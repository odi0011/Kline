"""受限沙盒：让 Agent 能写脚本、跑命令，但**只能在指定目录里动**。

设计取舍（为什么不是 Docker）：
  · 服务器上没有 bwrap/firejail，但有 `unshare`（util-linux）和 systemd 252。
  · Docker 要拉镜像、占空间、启动慢，这个场景不值得。
  · 用 Linux namespace + 资源限制就够了：mount 隔离挡住越界访问，
    cgroup/ulimit 挡住内存与进程爆炸。

三层防护：
  1. **路径**：所有路径先规范化，再强制落在工作目录内（realpath 比较，
     挡掉 `../`、符号链接、绝对路径）。
  2. **namespace**：`unshare --mount` 把根挂成只读（bind remount ro），
     只把工作目录 bind 成可写；`--pid --fork` 隔离进程，
     `--net` 断网（脚本默认不需要外网）。
  3. **资源**：内存上限（默认 512MB）、CPU 时间、进程数、单次墙钟超时。

一切命令走白名单：只允许解释器/构建类命令，禁 shell 元字符（; | & $() 反引号）。
不是"尽力而为"的黑名单——黑名单永远漏。
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

# 工作目录：部署目录下的 sandbox/，与代码、配置、数据完全分开
WORKDIR_NAME = "sandbox"
# 资源上限
DEFAULT_TIMEOUT = 30          # 秒，单次命令
MAX_TIMEOUT = 120
DEFAULT_MEM_MB = 512
MAX_MEM_MB = 2048
MAX_OUTPUT = 60_000           # 返回给模型的字符上限
MAX_FILE_BYTES = 512_000      # 单次写入上限
MAX_PROC = 64                 # 最多 fork 多少进程

# 命令白名单：只要解释器和常用只读工具。**不做通配**。
ALLOWED_COMMANDS = {
    "python", "python3", "node", "bash", "sh",
    "cat", "head", "tail", "wc", "grep", "sort", "uniq",
    "ls", "find", "stat", "file", "du", "df",
    "awk", "sed", "tr", "cut", "paste", "column",
    "jq", "bc", "date", "echo", "printf", "true", "false",
    "mkdir", "rmdir", "touch", "cp", "mv", "rm",
    "tar", "gzip", "gunzip", "zip", "unzip",
}

# 这些即使在工作目录内也不允许：能把文件写到目录外或提权
FORBIDDEN_PATTERNS = (
    r"/etc/", r"/proc/", r"/sys/", r"/dev/", r"/root/", r"/var/",
    r"\.\./", r"~/", r"\$HOME",
    r"sudo", r"su\s", r"chmod\s+[0-7]*[sS]", r"chown", r"mount", r"umount",
    r"curl", r"wget", r"nc\s", r"netcat", r"ssh", r"scp",
    r"docker", r"systemctl", r"crontab", r"modprobe", r"insmod",
)

# shell 元字符：一旦出现就说明这是想拼接命令而不是跑单个程序。
# 命令是直接 exec 的（不经过 shell），但参数里带这些说明调用方意图可疑，
# 干脆整条拒绝——正常情况下没有理由需要它们。
SHELL_META = (";", "&&", "||", "$(", "`", ">", "<", "|", "\n", "\r")


class SandboxError(ValueError):
    pass


def workdir(root: str) -> str:
    """沙盒工作目录（不存在就建）。"""
    path = os.path.join(root, WORKDIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def _inside(base: str, target: str) -> str:
    """把 target 规范化并确认它在 base 内；返回绝对路径。

    用 realpath 而不是字符串前缀比较：符号链接可以绕过前缀检查，
    只比字符串等于没做。
    """
    base_real = os.path.realpath(base)
    # 相对路径按 workdir 解析，绝对路径也接受（但必须落在 base 内）
    candidate = target if os.path.isabs(target) else os.path.join(base_real, target)
    real = os.path.realpath(candidate)
    # 允许目标就是 base 本身，或 base 之下的路径
    if real != base_real and not real.startswith(base_real + os.sep):
        raise SandboxError(f"路径越界：{target} 不在工作目录内")
    return real


def check_command(command: str) -> list[str]:
    """校验命令并拆成 argv。只允许白名单里的第一个词，禁 shell 元字符。

    除了拦危险模式，还要单独盯住**破坏性参数**：
    `rm -rf /` 的第一个词在白名单里、`/` 又太短不触发路径正则，
    光靠黑名单会漏——这就是为什么下面有独立的目录参数检查。
    """
    text = str(command or "").strip()
    if not text:
        raise SandboxError("命令为空")
    if len(text) > 4000:
        raise SandboxError("命令过长")
    # shell 元字符只在**命令外层**禁止：`python3 -c "import time; ..."` 里的
    # 分号是 Python 语法，不是 shell 拼接。
    code_match = re.search(r"""(-c|-e|--eval)\s+(['"])(.*?)\2""", text, re.S)
    outer = text
    if code_match:
        outer = text[:code_match.start()] + " CODE " + text[code_match.end():]
        # 代码体本身也要查：`python3 -c "import os; os.system('...')"` 是绕过
        # 命令白名单的常见手法。这里查的是同一套危险模式。
        body = code_match.group(3)
        for pattern in FORBIDDEN_PATTERNS:
            if re.search(pattern, body, re.I):
                raise SandboxError(f"命令脚本包含不允许的内容：{pattern}")
        for bad in ("os.system", "subprocess", "os.exec", "os.spawn", "pty.spawn",
                    "shutil.rmtree", "__import__"):
            if bad in body:
                raise SandboxError(f"命令脚本包含不允许的调用：{bad}")
    for meta in SHELL_META:
        if meta in outer:
            raise SandboxError(f"命令包含 shell 元字符 {meta!r}，不允许拼接命令")
    for pattern in FORBIDDEN_PATTERNS:
        if re.search(pattern, text, re.I):
            raise SandboxError(f"命令包含不允许的内容：{pattern}")
    # 裸的路径参数：/ 或 /xxx 或 .. 一律拒绝（沙盒里只能用相对路径）
    for token in text.replace(";", " ").split():
        if token in ("/", "/*", "~", "..", "../"):
            raise SandboxError(f"不允许直接操作 {token}")
    try:
        argv = shlex.split(text)
    except ValueError as exc:
        raise SandboxError(f"命令解析失败：{exc}") from None
    if not argv:
        raise SandboxError("命令为空")
    name = os.path.basename(argv[0])
    if name not in ALLOWED_COMMANDS:
        raise SandboxError(f"命令 {name} 不在允许列表内")
    # 逐个参数检查：任何看起来像根路径/父目录的都要拒绝
    for arg in argv[1:]:
        if arg.startswith("-"):
            continue            # 选项交给调用方的白名单语义
        if arg in ("/", "..") or arg.startswith("/") or ".." in arg:
            raise SandboxError(f"参数 {arg} 越界：沙盒内只能用相对路径")
    return argv


def _unshare_argv(root: str, argv: list[str]) -> list[str] | None:
    """构造 unshare 调用：mount/pid/net/ipc/uts 五个 namespace。

    根文件系统在子进程里 remount 成只读，只把工作目录 bind 回可写。
    实测（Debian 12 / kernel 6.1）：
        touch /etc/x  →  "Read-only file system"      被挡住
        echo x > 工作目录/文件 → 正常                    可用
    返回 None 表示这台机器不支持（非 Linux / 无权限），调用方降级。
    """
    if not sys.platform.startswith("linux") or not shutil.which("unshare"):
        return None
    base = workdir(root)
    script = (
        "mount --make-rprivate / 2>/dev/null; "
        "mount --bind / / 2>/dev/null; "
        "mount -o remount,bind,ro / 2>/dev/null; "
        f"mount --bind {shlex.quote(base)} {shlex.quote(base)} 2>/dev/null; "
        'cd "$1" && shift && exec "$@"'
    )
    return ["unshare", "--mount", "--pid", "--fork", "--net", "--ipc", "--uts",
            "sh", "-c", script, "_", base, *argv]


def run(root: str, command: str, timeout: int = DEFAULT_TIMEOUT,
        mem_mb: int = DEFAULT_MEM_MB, isolate: bool = True) -> dict:
    """在沙盒里执行一条命令，返回 {ok, stdout, stderr, code, ms, isolated}。

    isolate=True 时走 unshare（根只读 + 独立 pid/net）；
    不可用则降级为"仅路径校验 + ulimit"，并在返回值里标 isolated=False，
    让调用方知道这次没有 namespace 保护。
    """
    argv = check_command(command)
    base = workdir(root)
    timeout = max(1, min(int(timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT))
    mem_mb = max(64, min(int(mem_mb or DEFAULT_MEM_MB), MAX_MEM_MB))

    import time

    # resource 是 Unix 专有：Windows 上没有，跳过 ulimit（生产环境是 Linux）
    try:
        import resource
    except ImportError:      # pragma: no cover - 仅 Windows 本地开发
        resource = None

    def limits():
        if resource is None:
            return
        resource.setrlimit(resource.RLIMIT_AS, (mem_mb * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (timeout, timeout))
        resource.setrlimit(resource.RLIMIT_NPROC, (MAX_PROC, MAX_PROC))
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FILE_BYTES, MAX_FILE_BYTES))

    final = _unshare_argv(root, argv) if isolate else None
    isolated = final is not None
    if final is None:
        final = argv

    started = time.monotonic()
    extra = {"preexec_fn": limits} if resource is not None else {}
    try:
        proc = subprocess.run(
            final, cwd=base, timeout=timeout,
            capture_output=True, text=True, errors="replace",
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": base,
                 "TMPDIR": base, "LANG": "C.UTF-8"},
            **extra,
        )
        code = proc.returncode
        out = proc.stdout or ""
        err = proc.stderr or ""
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"执行超时（{timeout}s）", "code": -1, "isolated": isolated}
    except Exception as exc:
        return {"ok": False, "error": f"执行失败：{type(exc).__name__}: {exc}",
                "code": -1, "isolated": isolated}
    ms = int((time.monotonic() - started) * 1000)
    return {
        "ok": code == 0,
        "code": code,
        "ms": ms,
        "isolated": isolated,
        "stdout": out[:MAX_OUTPUT],
        "stderr": err[:MAX_OUTPUT],
        "truncated": len(out) > MAX_OUTPUT or len(err) > MAX_OUTPUT,
    }


def write_file(root: str, path: str, content: str) -> dict:
    """在沙盒里写文件。路径必须在工作目录内，大小有上限。"""
    base = workdir(root)
    target = _inside(base, path)
    data = str(content or "")
    if len(data.encode("utf-8")) > MAX_FILE_BYTES:
        raise SandboxError(f"文件超过 {MAX_FILE_BYTES // 1000}KB 上限")
    os.makedirs(os.path.dirname(target) or base, exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(data)
    return {"written": True, "path": os.path.relpath(target, base), "bytes": len(data.encode('utf-8'))}


def read_file(root: str, path: str, limit: int = 40_000) -> dict:
    """读沙盒里的文件。同样受路径约束。"""
    base = workdir(root)
    target = _inside(base, path)
    if not os.path.isfile(target):
        raise SandboxError(f"文件不存在：{path}")
    with open(target, encoding="utf-8", errors="replace") as handle:
        data = handle.read(limit + 1)
    return {"path": os.path.relpath(target, base),
            "content": data[:limit], "truncated": len(data) > limit}


def list_files(root: str, path: str = ".") -> dict:
    base = workdir(root)
    target = _inside(base, path)
    if not os.path.isdir(target):
        raise SandboxError(f"目录不存在：{path}")
    items = []
    for name in sorted(os.listdir(target))[:200]:
        full = os.path.join(target, name)
        try:
            st = os.stat(full)
            items.append({"name": name,
                          "dir": os.path.isdir(full),
                          "size": 0 if os.path.isdir(full) else st.st_size})
        except OSError:
            continue
    return {"path": os.path.relpath(target, base), "items": items}
