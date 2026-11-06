"""沙盒：路径越界、命令白名单、资源上限、namespace 隔离。

这些测试是安全边界的证明——任何一条放宽都意味着 Agent 能碰到
工作目录以外的东西。
"""
import os
import sys

import pytest

from ai import sandbox


@pytest.fixture()
def root(tmp_path):
    return str(tmp_path)


# ---------- 路径 ----------

def test_relative_path_inside_workdir_is_ok(root):
    out = sandbox.write_file(root, "a/b.txt", "hello")
    assert out["written"] is True
    assert sandbox.read_file(root, "a/b.txt")["content"] == "hello"


def test_parent_traversal_is_blocked(root):
    for bad in ("../escape.txt", "../../etc/passwd", "a/../../escape.txt"):
        with pytest.raises(sandbox.SandboxError):
            sandbox.write_file(root, bad, "x")


def test_absolute_path_outside_is_blocked(root):
    with pytest.raises(sandbox.SandboxError):
        sandbox.write_file(root, "/etc/passwd", "x")


def test_absolute_path_inside_is_allowed(root):
    base = sandbox.workdir(root)
    target = os.path.join(base, "ok.txt")
    out = sandbox.write_file(root, target, "x")
    assert out["written"] is True


def test_symlink_escape_is_blocked(root):
    """符号链接是最容易被忽略的越界方式：字符串前缀比较挡不住它。"""
    base = sandbox.workdir(root)
    link = os.path.join(base, "evil")
    try:
        os.symlink("/etc", link)
    except OSError:
        pytest.skip("当前环境不支持创建符号链接")
    with pytest.raises(sandbox.SandboxError):
        sandbox.read_file(root, "evil/passwd")


def test_file_size_limit(root):
    with pytest.raises(sandbox.SandboxError):
        sandbox.write_file(root, "big.txt", "x" * (sandbox.MAX_FILE_BYTES + 10))


# ---------- 命令白名单 ----------

def test_allowed_command_runs(root):
    out = sandbox.run(root, "echo hello", isolate=False)
    assert out["ok"] is True
    assert "hello" in out["stdout"]


def test_unknown_command_is_rejected(root):
    for bad in ("rm -rf /", "curl http://x", "wget http://x", "ssh root@x"):
        # rm 在白名单里但 "/" 会被路径规则挡；curl/wget/ssh 直接不在白名单
        with pytest.raises(sandbox.SandboxError):
            sandbox.run(root, bad, isolate=False)


def test_shell_metacharacters_are_blocked(root):
    for bad in ("echo a; rm -rf /", "echo a && whoami", "echo $(id)",
                "echo `id`", "cat /etc/passwd"):
        with pytest.raises(sandbox.SandboxError):
            sandbox.run(root, bad, isolate=False)


def test_system_paths_are_blocked(root):
    for bad in ("cat /etc/hostname", "ls /proc/1", "cat /root/.ssh/id_rsa"):
        with pytest.raises(sandbox.SandboxError):
            sandbox.run(root, bad, isolate=False)


def test_privilege_escalation_is_blocked(root):
    for bad in ("sudo ls", "chmod 4755 x", "mount /dev/sda1 /mnt"):
        with pytest.raises(sandbox.SandboxError):
            sandbox.run(root, bad, isolate=False)


# ---------- 资源 ----------

def test_timeout_is_enforced(root):
    code = "python3 -c 'import time; time.sleep(5)'"
    out = sandbox.run(root, code, timeout=1, isolate=False)
    assert out["ok"] is False
    # 超时可能报 error，也可能被 kill 成非零退出码
    assert "超时" in (out.get("error") or "") or out.get("code") not in (0, None)


@pytest.mark.skipif(sys.platform == "win32",
                    reason="RLIMIT_AS 依赖 Unix resource，Windows 上沙盒按设计跳过内存上限")
def test_memory_limit_is_enforced(root):
    # 申请远超 512MB 的内存应被 RLIMIT_AS 挡住
    code = "python3 -c 'x = bytearray(1024*1024*1024)'"
    out = sandbox.run(root, code, timeout=20, mem_mb=64, isolate=False)
    assert out["ok"] is False, "超内存的分配应该失败"


def test_output_is_capped(root):
    code = "python3 -c 'print(\"x\" * 200000)'"
    out = sandbox.run(root, code, timeout=20, isolate=False)
    assert len(out["stdout"]) <= sandbox.MAX_OUTPUT


# ---------- 隔离 ----------

def test_readonly_root_when_isolation_available(root):
    """有 unshare 时，根文件系统必须只读：写 /tmp 之外的位置应失败。"""
    if not sys.platform.startswith("linux"):
        pytest.skip("仅 Linux")
    import shutil
    if not shutil.which("unshare"):
        pytest.skip("没有 unshare")
    out = sandbox.run(root, "touch /etc/zzz_should_fail", isolate=True)
    if not out.get("isolated"):
        pytest.skip("unshare 在本环境不可用（可能缺权限）")
    assert out["ok"] is False, "根文件系统应该只读"


def test_workdir_is_writable_under_isolation(root):
    if not sys.platform.startswith("linux"):
        pytest.skip("仅 Linux")
    import shutil
    if not shutil.which("unshare"):
        pytest.skip("没有 unshare")
    out = sandbox.run(root, "touch inside.txt", isolate=True)
    if not out.get("isolated"):
        pytest.skip("unshare 在本环境不可用")
    assert out["ok"] is True, out
    assert os.path.exists(os.path.join(sandbox.workdir(root), "inside.txt"))


# ---------- 绕过手法的回归测试 ----------

def test_rm_root_is_blocked():
    """`rm -rf /` 的第一个词在白名单里，必须靠参数检查拦住。"""
    with pytest.raises(sandbox.SandboxError):
        sandbox.check_command("rm -rf /")


def test_python_c_cannot_shell_out():
    """`python3 -c "import os; os.system(...)"` 是绕过命令白名单的常见手法。"""
    for bad in (
        'python3 -c "import os; os.system(\'rm -rf /\')"',
        'python3 -c "import subprocess; subprocess.run([\'ls\'])"',
        'python3 -c "import shutil; shutil.rmtree(\'/\')"',
        'python3 -c "__import__(\'os\').system(\'id\')"',
    ):
        with pytest.raises(sandbox.SandboxError):
            sandbox.check_command(bad)


def test_python_c_cannot_read_system_files():
    """代码体里出现 /etc/ 之类的路径也要拦。"""
    with pytest.raises(sandbox.SandboxError):
        sandbox.check_command('python3 -c "print(open(\'/etc/passwd\').read())"')


def test_normal_script_still_works():
    """合法脚本不能被误伤。"""
    argv = sandbox.check_command('python3 -c "print(sum(range(10)))"')
    assert argv[0] == "python3"
