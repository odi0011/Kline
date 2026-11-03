"""配置密钥加密（secrets_store + tools/encrypt_config.py）测试。

覆盖规格：
* 单值加解密 roundtrip / 非 enc 原样 / 错 key 抛 / 缺 cryptography（monkeypatch）抛；
* load_key env 优先 / 文件 / create 生成 0600（Windows 跳过权限断言） / 非法 env；
* decrypt_config 明文不改 / enc 解密 / env 覆盖优先 / 深拷贝；
* encrypt_config 全 slot 加密 / 掩码跳过 / 幂等 / 无 key 原样；
* secret_slots 边界 / audit_plaintext / redact_config / get_path、set_path 容错；
* CLI run：临时目录加密后 yaml 带 enc:v1: + 备份存在 + mode 0600（Windows 跳过）、
  decrypt 恢复明文、--check 返回状态 dict 且不写文件。

运行：python -m pytest tests/test_secrets.py -q -p no:cacheprovider
"""
import base64
import os
import stat
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import secrets_store as ss  # noqa: E402
import tools.encrypt_config as cli  # noqa: E402

SLOT_ENVS = (
    "KLINE_MYSQL_PASSWORD",
    "KLINE_AI_KEY",
    "KLINE_TELEGRAM_TOKEN",
    "KLINE_WEBHOOK_URL",
    "KLINE_ACCOUNT_API_KEY",
    "KLINE_ACCOUNT_API_SECRET",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """隔离宿主机环境变量，避免真实部署的 KLINE_* 污染测试。"""
    for name in (*SLOT_ENVS, ss.ENV_KEY):
        monkeypatch.delenv(name, raising=False)


def new_key() -> bytes:
    """生成一把测试用 Fernet 主密钥（32 字节随机 + urlsafe base64）。"""
    return base64.urlsafe_b64encode(os.urandom(32))


def full_cfg() -> dict:
    """覆盖全部 6 个 slot 的配置样例（全部用假值，不含任何真实密钥）。"""
    return {
        "web_port": 8000,
        "mysql": {"host": "127.0.0.1", "password": "mysql-pass-1"},
        "ai": {"key": "sk-ai-test"},
        "telegram": {"token": "123:telegram-token", "enabled": False},
        "notify": {"webhook": {"enabled": False, "url": "https://example.invalid/hook"}},
        "accounts": [
            {"name": "acc0", "api_key": "api-key-0", "api_secret": "api-secret-0"},
            {"name": "acc1", "api_key": "api-key-1", "api_secret": "api-secret-1"},
        ],
    }


def write_yaml(path: Path, cfg: dict) -> Path:
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")
    return path


def hide_cryptography(monkeypatch) -> None:
    """模拟"没装 cryptography"：让 import cryptography 抛 ImportError。"""
    monkeypatch.setitem(sys.modules, "cryptography", None)
    monkeypatch.setitem(sys.modules, "cryptography.fernet", None)


# ---------------------------------------------------------------------------
# 单值加解密
# ---------------------------------------------------------------------------

def test_encrypt_decrypt_roundtrip():
    key = new_key()
    token = ss.encrypt_value("我的密钥-abc", key)
    assert isinstance(token, str) and token.startswith(ss.ENC_PREFIX)
    assert token != "我的密钥-abc"
    assert ss.decrypt_value(token, key) == "我的密钥-abc"


def test_encrypt_value_passthrough():
    key = new_key()
    assert ss.encrypt_value(None, key) is None
    assert ss.encrypt_value("", key) == ""
    assert ss.encrypt_value(123, key) == 123
    token = ss.encrypt_value("x", key)
    assert ss.encrypt_value(token, key) == token  # 已加密不二次加密


def test_decrypt_value_passthrough():
    key = new_key()
    assert ss.decrypt_value("明文", key) == "明文"
    assert ss.decrypt_value(None, key) is None
    assert ss.decrypt_value(123, key) == 123


def test_decrypt_wrong_key_raises():
    token = ss.encrypt_value("secret", new_key())
    with pytest.raises(ss.SecretsError):
        ss.decrypt_value(token, new_key())


def test_decrypt_missing_key_raises():
    token = ss.encrypt_value("secret", new_key())
    with pytest.raises(ss.SecretsError):
        ss.decrypt_value(token, None)


def test_bad_key_raises():
    with pytest.raises(ss.SecretsError):
        ss.encrypt_value("x", b"not-a-valid-key")
    with pytest.raises(ss.SecretsError):
        ss.encrypt_value("x", None)


def test_missing_cryptography_raises(monkeypatch):
    hide_cryptography(monkeypatch)
    key = new_key()
    assert ss.cryptography_available() is False
    with pytest.raises(ss.SecretsError):
        ss.encrypt_value("x", key)
    # 非加密值不需要依赖，明文配置路径不受影响。
    assert ss.decrypt_value("plain", key) == "plain"


# ---------------------------------------------------------------------------
# 主密钥：环境变量 / 文件 / 生成
# ---------------------------------------------------------------------------

def test_load_key_env_priority(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("{}", encoding="utf-8")
    file_key = new_key()
    (tmp_path / "secrets.key").write_bytes(file_key + b"\n")
    env_key = new_key()
    monkeypatch.setenv(ss.ENV_KEY, env_key.decode("ascii"))
    assert ss.load_key(str(config)) == env_key


def test_load_key_from_file(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("{}", encoding="utf-8")
    file_key = new_key()
    key_path = tmp_path / "secrets.key"
    key_path.write_bytes(file_key + b"\n")
    assert ss.key_available(str(config)) is True
    assert ss.load_key(str(config)) == file_key


def test_load_key_create_and_mode(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("{}", encoding="utf-8")
    assert ss.load_key(str(config)) is None
    key = ss.load_key(str(config), create=True)
    assert isinstance(key, bytes) and len(base64.urlsafe_b64decode(key)) == 32
    key_path = tmp_path / "secrets.key"
    assert key_path.exists()
    if os.name != "nt":  # Windows 上 chmod 语义有限，跳过权限断言
        assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert ss.load_key(str(config)) == key  # 二次读取同一把


def test_load_key_invalid_env(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("{}", encoding="utf-8")
    monkeypatch.setenv(ss.ENV_KEY, "not base64 !!")
    with pytest.raises(ss.SecretsError):
        ss.load_key(str(config))
    monkeypatch.setenv(ss.ENV_KEY, base64.urlsafe_b64encode(b"short").decode("ascii"))
    with pytest.raises(ss.SecretsError):
        ss.load_key(str(config))


def test_key_available(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("{}", encoding="utf-8")
    assert ss.key_available(str(config)) is False
    (tmp_path / "secrets.key").write_bytes(new_key() + b"\n")
    assert ss.key_available(str(config)) is True
    monkeypatch.setenv(ss.ENV_KEY, "bad!")
    assert ss.key_available(str(config)) is False


def test_harden_permissions(tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text("x", encoding="utf-8")
    if os.name != "nt":
        target.chmod(0o644)
        ss.harden_permissions(target)
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    ss.harden_permissions(tmp_path / "missing.file")  # 不存在也不抛


# ---------------------------------------------------------------------------
# slot / 路径 / 审计 / 脱敏
# ---------------------------------------------------------------------------

def test_secret_slots_boundaries():
    fixed = list(ss._FIXED_SLOTS)
    assert ss.secret_slots({}) == fixed
    assert len(ss.secret_slots(None)) == 4
    assert len(ss.secret_slots({"accounts": []})) == 4
    assert len(ss.secret_slots({"accounts": "oops"})) == 4
    slots = ss.secret_slots({"accounts": [{"api_key": "a"}, {"api_key": "b"}]})
    assert len(slots) == 6
    assert slots[4] == (("accounts", 0, "api_key"), "KLINE_ACCOUNT_API_KEY")
    assert slots[5] == (("accounts", 0, "api_secret"), "KLINE_ACCOUNT_API_SECRET")
    assert all(isinstance(path, tuple) and isinstance(env, str) for path, env in slots)
    assert [env for _path, env in fixed] == [
        "KLINE_MYSQL_PASSWORD", "KLINE_AI_KEY",
        "KLINE_TELEGRAM_TOKEN", "KLINE_WEBHOOK_URL",
    ]


def test_get_set_path_tolerant():
    cfg = {"a": {"b": 1}, "lst": [{"x": 1}]}
    assert ss.get_path(cfg, ("a", "b")) == 1
    assert ss.get_path(cfg, ("a", "c")) is None
    assert ss.get_path(cfg, ("nope", "x")) is None
    assert ss.get_path(cfg, ("lst", 0, "x")) == 1
    assert ss.get_path({"lst": []}, ("lst", 0)) is None

    assert ss.set_path(cfg, ("c", "d"), 2) is True
    assert cfg["c"]["d"] == 2
    assert ss.set_path(cfg, ("lst", 0, "y"), 3) is True
    assert cfg["lst"][0]["y"] == 3
    assert ss.set_path(cfg, ("lst", 5, "y"), 3) is False
    assert ss.set_path(cfg, (), 1) is False
    assert ss.set_path({"a": 1}, ("a", "b"), 2) is False
    assert ss.set_path(None, ("a",), 1) is False


def test_audit_plaintext():
    assert ss.audit_plaintext(full_cfg()) == [
        "mysql.password", "ai.key", "telegram.token", "notify.webhook.url",
        "accounts[0].api_key", "accounts[0].api_secret",
    ]
    cfg = full_cfg()
    cfg["mysql"]["password"] = ss.encrypt_value("p", new_key())
    cfg["telegram"]["token"] = "\u2022\u2022\u2022\u2022"
    cfg["notify"]["webhook"]["url"] = ""
    cfg["ai"].pop("key")
    assert ss.audit_plaintext(cfg) == ["accounts[0].api_key", "accounts[0].api_secret"]
    assert ss.audit_plaintext(None) == []


def test_redact_config():
    cfg = full_cfg()
    red = ss.redact_config(cfg)
    assert red is not cfg
    assert red["mysql"]["password"] == "***"
    assert red["ai"]["key"] == "***"
    assert red["accounts"][0]["api_key"] == "***"
    assert red["accounts"][0]["api_secret"] == "***"
    assert red["accounts"][1]["api_key"] == "api-key-1"  # 只处理第 0 个账户
    assert red["web_port"] == 8000
    assert cfg["mysql"]["password"] == "mysql-pass-1"  # 入参不被修改


# ---------------------------------------------------------------------------
# encrypt_config / decrypt_config
# ---------------------------------------------------------------------------

def test_encrypt_config_all_slots(tmp_path):
    cfg = full_cfg()
    cfg["telegram"]["token"] = "***masked***"
    cfg["notify"]["webhook"]["url"] = ""
    config = tmp_path / "config.yaml"
    config.write_text("{}", encoding="utf-8")

    encrypted = ss.encrypt_config(cfg, str(config), create_key=True)
    assert encrypted is not cfg
    assert encrypted["mysql"]["password"].startswith(ss.ENC_PREFIX)
    assert encrypted["ai"]["key"].startswith(ss.ENC_PREFIX)
    assert encrypted["accounts"][0]["api_key"].startswith(ss.ENC_PREFIX)
    assert encrypted["accounts"][0]["api_secret"].startswith(ss.ENC_PREFIX)
    assert encrypted["telegram"]["token"] == "***masked***"  # 掩码跳过
    assert encrypted["notify"]["webhook"]["url"] == ""       # 空值跳过
    assert encrypted["accounts"][1]["api_key"] == "api-key-1"  # 非第 0 账户不动
    assert cfg["mysql"]["password"] == "mysql-pass-1"          # 入参不被修改
    assert (tmp_path / "secrets.key").exists()

    again = ss.encrypt_config(encrypted, str(config))
    assert again == encrypted  # 幂等：已加密的不再处理


def test_encrypt_config_no_key_returns_copy(tmp_path, monkeypatch):
    cfg = full_cfg()
    config = tmp_path / "config.yaml"
    config.write_text("{}", encoding="utf-8")
    out = ss.encrypt_config(cfg, str(config))
    assert out == cfg and out is not cfg
    assert not (tmp_path / "secrets.key").exists()

    # 没有 key 时连 cryptography 都不需要 import。
    hide_cryptography(monkeypatch)
    assert ss.encrypt_config(cfg, str(config)) == cfg


def test_decrypt_config_plain_untouched(tmp_path):
    cfg = full_cfg()
    config = tmp_path / "config.yaml"
    config.write_text("{}", encoding="utf-8")
    out = ss.decrypt_config(cfg, str(config))
    assert out == cfg
    assert out is not cfg
    assert out["mysql"] is not cfg["mysql"]  # 深拷贝


def test_decrypt_config_roundtrip(tmp_path):
    cfg = full_cfg()
    config = tmp_path / "config.yaml"
    config.write_text("{}", encoding="utf-8")
    encrypted = ss.encrypt_config(cfg, str(config), create_key=True)
    assert encrypted["mysql"]["password"].startswith(ss.ENC_PREFIX)
    assert ss.decrypt_config(encrypted, str(config)) == cfg


def test_decrypt_config_env_override(tmp_path, monkeypatch):
    cfg = full_cfg()
    config = tmp_path / "config.yaml"
    config.write_text("{}", encoding="utf-8")
    encrypted = ss.encrypt_config(cfg, str(config), create_key=True)
    monkeypatch.setenv("KLINE_AI_KEY", "env-ai-key")
    monkeypatch.setenv("KLINE_MYSQL_PASSWORD", "env-mysql-pass")
    out = ss.decrypt_config(encrypted, str(config))
    assert out["ai"]["key"] == "env-ai-key"
    assert out["mysql"]["password"] == "env-mysql-pass"
    assert out["accounts"][0]["api_key"] == "api-key-0"  # 未覆盖的走解密
    assert encrypted["ai"]["key"] != "env-ai-key"        # 入参不被修改


def test_decrypt_config_missing_key_raises(tmp_path):
    cfg = full_cfg()
    cfg["ai"]["key"] = ss.encrypt_value("secret", new_key())
    config = tmp_path / "config.yaml"
    config.write_text("{}", encoding="utf-8")
    with pytest.raises(ss.SecretsError):
        ss.decrypt_config(cfg, str(config))


def test_decrypt_config_missing_cryptography_raises(tmp_path, monkeypatch):
    key = new_key()
    cfg = full_cfg()
    cfg["ai"]["key"] = ss.encrypt_value("secret", key)
    config = tmp_path / "config.yaml"
    config.write_text("{}", encoding="utf-8")
    monkeypatch.setenv(ss.ENV_KEY, key.decode("ascii"))  # 有 key，但缺 cryptography
    hide_cryptography(monkeypatch)
    with pytest.raises(ss.SecretsError):
        ss.decrypt_config(cfg, str(config))


# ---------------------------------------------------------------------------
# CLI（tools/encrypt_config.py）
# ---------------------------------------------------------------------------

def test_cli_encrypt_then_decrypt(tmp_path):
    cfg = full_cfg()
    config = write_yaml(tmp_path / "config.yaml", cfg)

    info = cli.run(str(config))
    assert info["action"] == "encrypt"
    assert info["count"] == 6
    assert os.path.exists(info["backup"])
    assert Path(info["backup"]).name.startswith("config.yaml.bak.")
    key_path = tmp_path / "secrets.key"
    assert key_path.exists()
    assert info["keyPath"] == str(key_path)

    encrypted = yaml.safe_load(config.read_text(encoding="utf-8"))
    for path, _env in ss.secret_slots(cfg):
        assert ss.is_encrypted(ss.get_path(encrypted, path))
    assert "mysql-pass-1" in Path(info["backup"]).read_text(encoding="utf-8")  # 备份含明文
    if os.name != "nt":  # Windows 跳过权限断言
        assert stat.S_IMODE(config.stat().st_mode) == 0o600
        assert stat.S_IMODE(key_path.stat().st_mode) == 0o600

    dec = cli.run(str(config), decrypt=True)
    assert dec["action"] == "decrypt" and dec["count"] == 6
    assert yaml.safe_load(config.read_text(encoding="utf-8")) == cfg
    assert len(list(tmp_path.glob("config.yaml.bak.*"))) == 2


def test_cli_check_does_not_write(tmp_path):
    cfg = full_cfg()
    config = write_yaml(tmp_path / "config.yaml", cfg)
    info = cli.run(str(config), check=True)
    assert info["action"] == "check"
    assert info["key"] is False
    assert len(info["atRisk"]) == 6
    assert all(slot["state"] == "plaintext" for slot in info["slots"])
    assert not (tmp_path / "secrets.key").exists()
    assert not list(tmp_path.glob("config.yaml.bak.*"))
    assert yaml.safe_load(config.read_text(encoding="utf-8")) == cfg

    cli.run(str(config))  # 加密
    info2 = cli.run(str(config), check=True)
    assert info2["key"] is True
    assert info2["atRisk"] == []
    assert all(slot["state"] == "encrypted" for slot in info2["slots"])


def test_cli_missing_config(tmp_path, capsys):
    missing = tmp_path / "nope.yaml"
    with pytest.raises(ss.SecretsError):
        cli.run(str(missing), check=True)
    assert cli.main(["--config", str(missing), "--check"]) == 1
    assert "配置文件不存在" in capsys.readouterr().err


def test_cli_decrypt_without_key_keeps_config(tmp_path):
    cfg = full_cfg()
    config = write_yaml(tmp_path / "config.yaml", cfg)
    cli.run(str(config))
    (tmp_path / "secrets.key").unlink()
    before = config.read_text(encoding="utf-8")
    with pytest.raises(ss.SecretsError):
        cli.run(str(config), decrypt=True)
    assert config.read_text(encoding="utf-8") == before
    assert len(list(tmp_path.glob("config.yaml.bak.*"))) == 1  # 失败不留新备份


def test_cli_custom_key_file(tmp_path):
    cfg = full_cfg()
    config = write_yaml(tmp_path / "config.yaml", cfg)
    custom = tmp_path / "custom.key"
    info = cli.run(str(config), key_file=str(custom))
    assert info["keySource"] == "created"
    assert Path(info["keyPath"]) == custom
    assert custom.exists()
    assert not (tmp_path / "secrets.key").exists()
    dec = cli.run(str(config), decrypt=True, key_file=str(custom))
    assert dec["count"] == 6
    assert yaml.safe_load(config.read_text(encoding="utf-8")) == cfg


def test_cli_env_key_avoids_key_file(tmp_path, monkeypatch):
    cfg = full_cfg()
    config = write_yaml(tmp_path / "config.yaml", cfg)
    monkeypatch.setenv(ss.ENV_KEY, new_key().decode("ascii"))
    info = cli.run(str(config))
    assert info["keySource"] == "env"
    assert not (tmp_path / "secrets.key").exists()
    assert yaml.safe_load(config.read_text(encoding="utf-8")) != cfg
    dec = cli.run(str(config), decrypt=True)
    assert dec["count"] == 6
    assert yaml.safe_load(config.read_text(encoding="utf-8")) == cfg


@pytest.mark.skipif(os.name == "nt", reason="Windows 无法可靠检查 POSIX 权限")
def test_cli_check_permission_warning(tmp_path):
    cfg = full_cfg()
    config = write_yaml(tmp_path / "config.yaml", cfg)
    config.chmod(0o644)
    info = cli.run(str(config), check=True)
    assert any("权限过宽" in warning for warning in info["warnings"])


def test_cli_main_encrypt_output(tmp_path, capsys):
    config = write_yaml(tmp_path / "config.yaml", full_cfg())
    assert cli.main(["--config", str(config)]) == 0
    out = capsys.readouterr().out
    assert "已加密 6 个配置项" in out
    assert "备份文件" in out
    assert "secrets.key" in out


def test_cli_refuses_new_key_when_config_already_encrypted(tmp_path):
    """已加密配置 + 密钥丢失：默认模式必须报错，不能悄悄生成新密钥把旧密文锁死。"""
    config = write_yaml(tmp_path / "config.yaml", full_cfg())
    cli.run(str(config))
    (tmp_path / "secrets.key").unlink()
    before = config.read_text(encoding="utf-8")
    with pytest.raises(ss.SecretsError):
        cli.run(str(config))
    assert config.read_text(encoding="utf-8") == before
    assert not (tmp_path / "secrets.key").exists()
