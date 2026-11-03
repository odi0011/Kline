"""配置密钥加密存储（Fernet）：把 config.yaml 里的敏感字段落成 enc:v1:... 密文。

设计约束（与 tools/encrypt_config.py、main.py 接线共用）：

* 惰性依赖：cryptography 只在真的遇到加密值/真的要加密时才 import。
  缺依赖时抛 SecretsError，明文配置照样能启动（不能在 import 阶段失败）。
* 密钥优先级：环境变量 KLINE_SECRET_KEY（urlsafe base64 的 32 字节）
  → 配置文件同目录的 secrets.key 文件。
* 环境变量覆盖：decrypt_config 里 6 个 slot 对应的 KLINE_* 环境变量
  优先级最高，用于服务器上完全不落盘密钥。
* 不静默吞错：密文解不开（错 key / 密文损坏 / 缺依赖）一律抛
  SecretsError，由调用方决定是拒绝启动还是降级明文。

加密格式：ENC_PREFIX("enc:v1:") + Fernet token（URL-safe base64）。
"""
from __future__ import annotations

import base64
import copy
import os
import stat
from typing import Any

# 加密值前缀：区分"密文"与普通明文（v1 便于将来换算法）。
ENC_PREFIX = "enc:v1:"
# 前端/API 回传的掩码标记：含这些字符的值不是真实密钥，不能加密（会把掩码当密钥存）。
MASK_MARKERS = ("***", "\u2022")
# 主密钥环境变量与密钥文件名。
ENV_KEY = "KLINE_SECRET_KEY"
KEY_FILE_NAME = "secrets.key"

# 固定 slot：配置路径 -> 覆盖它的环境变量名。
_FIXED_SLOTS: tuple[tuple[tuple, str], ...] = (
    (("mysql", "password"), "KLINE_MYSQL_PASSWORD"),
    (("ai", "key"), "KLINE_AI_KEY"),
    (("telegram", "token"), "KLINE_TELEGRAM_TOKEN"),
    (("notify", "webhook", "url"), "KLINE_WEBHOOK_URL"),
    # GIPHY 贴纸 API key：配了就能联网找表情包，没配就只用内置几张。
    (("stickers", "giphy_key"), "KLINE_GIPHY_KEY"),
)
_ACCOUNT_SLOTS: tuple[tuple[str, str], ...] = (
    ("api_key", "KLINE_ACCOUNT_API_KEY"),
    ("api_secret", "KLINE_ACCOUNT_API_SECRET"),
)


class SecretsError(RuntimeError):
    """密钥相关错误：缺 cryptography、密钥无效、解密失败、配置文件不可读等。"""


# ---------------------------------------------------------------------------
# 惰性依赖
# ---------------------------------------------------------------------------

def _import_fernet():
    """只在需要加解密时导入 cryptography；缺依赖转成 SecretsError。"""
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:  # pragma: no cover - 通过 monkeypatch 覆盖
        raise SecretsError(
            "未安装 cryptography：无法加解密配置密钥，"
            "请先 pip install cryptography>=42（明文配置仍可正常使用）"
        ) from exc
    return Fernet


def cryptography_available() -> bool:
    """当前环境是否能 import cryptography（用于提示/降级判断，不抛异常）。"""
    try:
        _import_fernet()
    except SecretsError:
        return False
    return True


# ---------------------------------------------------------------------------
# slot 定义与路径读写
# ---------------------------------------------------------------------------

def secret_slots(cfg: dict) -> list[tuple[tuple, str]]:
    """返回配置里所有需要加密的字段：[(路径元组, 覆盖它的环境变量名), ...]。

    固定 4 个 + 账户第 0 个（单账户设计：只保护页面实际使用的那一个账户）。
    accounts 缺失/为空/非列表时不产生账户 slot；函数对脏配置容错，不抛异常。
    """
    slots: list[tuple[tuple, str]] = list(_FIXED_SLOTS)
    accounts = cfg.get("accounts") if isinstance(cfg, dict) else None
    if isinstance(accounts, list) and accounts:
        for field, env_name in _ACCOUNT_SLOTS:
            slots.append((("accounts", 0, field), env_name))
    return slots


def get_path(cfg: Any, path: tuple) -> Any:
    """按路径元组取值；路径不存在/类型不匹配时返回 None（容错，不抛异常）。"""
    current = cfg
    for key in path:
        if isinstance(key, int):
            if not isinstance(current, list) or not 0 <= key < len(current):
                return None
            current = current[key]
        else:
            if not isinstance(current, dict) or key not in current:
                return None
            current = current[key]
    return current


def set_path(cfg: Any, path: tuple, value: Any) -> bool:
    """按路径元组写值；缺的 dict 中间层自动补；返回是否写入成功（容错，不抛异常）。

    列表下标必须是已存在的元素（不自动扩容，避免把 accounts 之外的东西撑成列表）。
    """
    if not path:
        return False
    current = cfg
    for index, key in enumerate(path[:-1]):
        nxt = path[index + 1]
        if isinstance(key, int):
            if not isinstance(current, list) or not 0 <= key < len(current):
                return False
            child = current[key]
            if isinstance(nxt, int) and not isinstance(child, list):
                return False
            if not isinstance(nxt, int) and not isinstance(child, dict):
                return False
            current = child
        else:
            if not isinstance(current, dict):
                return False
            if key not in current or current[key] is None:
                current[key] = [] if isinstance(nxt, int) else {}
            child = current[key]
            if isinstance(nxt, int) and not isinstance(child, list):
                return False
            if not isinstance(nxt, int) and not isinstance(child, dict):
                return False
            current = child
    last = path[-1]
    if isinstance(last, int):
        if not isinstance(current, list) or not 0 <= last < len(current):
            return False
        current[last] = value
        return True
    if not isinstance(current, dict):
        return False
    current[last] = value
    return True


def _slot_label(path: tuple) -> str:
    """路径转可读 slot 名：mysql.password / accounts[0].api_key。"""
    label = ""
    for key in path:
        if isinstance(key, int):
            label += "[%d]" % key
        else:
            label = (label + "." + str(key)) if label else str(key)
    return label


# ---------------------------------------------------------------------------
# 密钥：环境变量 / 文件 / 生成
# ---------------------------------------------------------------------------

def _decode_key(value) -> bytes:
    """把用户提供的密钥规范成 Fernet 需要的 urlsafe base64 bytes；非法抛 SecretsError。"""
    if isinstance(value, (bytes, bytearray)):
        try:
            text = bytes(value).decode("ascii").strip()
        except UnicodeDecodeError as exc:
            raise SecretsError("密钥格式无效：需为 ASCII 的 urlsafe base64") from exc
    else:
        text = str(value or "").strip()
    if not text:
        raise SecretsError("密钥为空：需为 urlsafe base64 编码的 32 字节")
    # 允许省略 padding 的写法（Fernet 生成的 key 自带 =，手写容易漏）。
    padded = text + "=" * (-len(text) % 4)
    try:
        raw = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
    except (ValueError, TypeError, UnicodeEncodeError) as exc:
        raise SecretsError("密钥格式无效：需为 urlsafe base64 编码的 32 字节") from exc
    if len(raw) != 32:
        raise SecretsError(
            "密钥长度无效（解出 %d 字节）：需 urlsafe base64 编码的 32 字节" % len(raw))
    return base64.urlsafe_b64encode(raw)


def _new_key() -> bytes:
    """生成一把新的 Fernet 主密钥（不依赖 cryptography：os.urandom 32B + base64）。"""
    return base64.urlsafe_b64encode(os.urandom(32))


def _key_path(config_path) -> str:
    """默认密钥文件路径：与配置文件同目录的 secrets.key。"""
    directory = os.path.dirname(os.path.abspath(str(config_path))) or "."
    return os.path.join(directory, KEY_FILE_NAME)


def _read_key_file(path: str) -> bytes:
    """读密钥文件并校验格式；空/坏文件抛 SecretsError。"""
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        raise SecretsError("读取密钥文件失败：%s（%s）" % (path, exc)) from exc
    data = data.strip()
    if not data:
        raise SecretsError("密钥文件为空：%s" % path)
    return _decode_key(data)


def _write_key_file(path: str, key: bytes) -> bytes:
    """把主密钥写盘并收紧权限（POSIX 0600，Windows 尽力）；已存在会被覆盖。"""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    try:
        os.makedirs(directory, exist_ok=True)
        temporary = path + ".tmp"
        with open(temporary, "wb") as handle:
            handle.write(bytes(key).strip() + b"\n")
        harden_permissions(temporary)
        os.replace(temporary, path)
        harden_permissions(path)
    except OSError as exc:
        raise SecretsError("写入密钥文件失败：%s（%s）" % (path, exc)) from exc
    return bytes(key).strip()


def load_key(config_path, create: bool = False) -> bytes | None:
    """取主密钥：环境变量 KLINE_SECRET_KEY 优先，其次同目录 secrets.key。

    * 环境变量存在但非法 → SecretsError（不能悄悄退回文件，否则会解密失败得更隐蔽）。
    * 文件不存在且 create=False → None。
    * create=True → 生成新密钥写 secrets.key（0600）并返回。
    """
    env = os.environ.get(ENV_KEY)
    if env is not None:
        return _decode_key(env)
    path = _key_path(config_path)
    if os.path.exists(path):
        return _read_key_file(path)
    if not create:
        return None
    return _write_key_file(path, _new_key())


def key_available(config_path) -> bool:
    """是否已有可用主密钥（env 或文件）；非法 env/坏文件按"不可用"返回 False。"""
    try:
        return load_key(config_path, create=False) is not None
    except SecretsError:
        return False


def harden_permissions(path) -> None:
    """文件权限收紧到 0600：POSIX 严格 chmod；Windows 只能尽力（chmod 语义有限）。"""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        # 不阻断主流程：权限是加固手段，不是功能前提。
        return


# ---------------------------------------------------------------------------
# 单值加解密
# ---------------------------------------------------------------------------

def _looks_masked(value) -> bool:
    return isinstance(value, str) and any(marker in value for marker in MASK_MARKERS)


def _should_encrypt(value) -> bool:
    """只有"非空明文、且不是 enc/掩码"的值才加密。"""
    return (
        isinstance(value, str)
        and bool(value.strip())
        and not is_encrypted(value)
        and not _looks_masked(value)
    )


def is_encrypted(value) -> bool:
    """是否为 enc:v1: 加密值（非字符串返回 False）。"""
    return isinstance(value, str) and value.startswith(ENC_PREFIX)


def _fernet(key):
    """构造 Fernet 实例；缺依赖/缺密钥/坏密钥统一转 SecretsError。"""
    Fernet = _import_fernet()
    if isinstance(key, (bytes, bytearray)):
        material = bytes(key).strip()
    elif isinstance(key, str):
        material = _decode_key(key)
    else:
        raise SecretsError("缺少加密密钥：请设置 KLINE_SECRET_KEY 或提供 secrets.key")
    if not material:
        raise SecretsError("缺少加密密钥：请设置 KLINE_SECRET_KEY 或提供 secrets.key")
    try:
        return Fernet(material)
    except (ValueError, TypeError) as exc:
        raise SecretsError("加密密钥格式无效：需 urlsafe base64 编码的 32 字节") from exc


def encrypt_value(plain, key):
    """加密单个值：非字符串/空串/已是密文的值原样返回；其余返回 enc:v1:...。"""
    if not isinstance(plain, str) or not plain or is_encrypted(plain):
        return plain
    fernet = _fernet(key)
    try:
        token = fernet.encrypt(plain.encode("utf-8"))
    except Exception as exc:  # cryptography 的异常类型不对外暴露
        raise SecretsError("加密失败：%s" % type(exc).__name__) from exc
    return ENC_PREFIX + token.decode("ascii")


def decrypt_value(token, key):
    """解密单个值：非 enc:v1: 值原样返回；缺 key/错 key/坏密文抛 SecretsError。"""
    if not isinstance(token, str) or not is_encrypted(token):
        return token
    if key is None:
        raise SecretsError("配置含加密值但缺少密钥：请设置 KLINE_SECRET_KEY 或提供 secrets.key")
    fernet = _fernet(key)
    body = token[len(ENC_PREFIX):]
    try:
        plain = fernet.decrypt(body.encode("ascii"))
    except Exception as exc:  # InvalidToken / 编码错误都按"解不开"处理
        raise SecretsError(
            "解密失败：密钥不匹配或密文损坏（请检查 KLINE_SECRET_KEY/secrets.key）"
        ) from exc
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SecretsError("解密结果不是有效 UTF-8 文本，配置可能已损坏") from exc


# ---------------------------------------------------------------------------
# 整份配置：解密 / 加密 / 审计 / 脱敏
# ---------------------------------------------------------------------------

def decrypt_config(cfg: dict, config_path) -> dict:
    """返回解密后的新配置（深拷贝，不修改入参）。

    顺序：环境变量覆盖优先（不落盘主密钥的部署方式）→ 密文值解密。
    只在遇到密文时才需要 cryptography；明文配置不 import、不要求密钥。
    """
    out = copy.deepcopy(cfg) if isinstance(cfg, dict) else {}
    for path, env_name in secret_slots(out):
        override = os.environ.get(env_name)
        if override is not None:
            set_path(out, path, override)
            continue
        value = get_path(out, path)
        if not is_encrypted(value):
            continue
        key = load_key(config_path, create=False)
        if key is None:
            raise SecretsError(
                "配置项 %s 已加密，但找不到密钥：请设置 KLINE_SECRET_KEY"
                " 或准备 %s（可运行 python tools/encrypt_config.py --decrypt）"
                % (_slot_label(path), _key_path(config_path)))
        set_path(out, path, decrypt_value(value, key))
    return out


def encrypt_config(cfg: dict, config_path, create_key: bool = False) -> dict:
    """返回加密后的新配置（深拷贝，不修改入参）。

    * 没有可用主密钥且 create_key=False → 原样返回副本（调用方可用
      audit_plaintext 提示"仍是明文"）。
    * 只加密明文 slot；空值/掩码/已是密文的值保持不动；账户只处理第 0 个。
    """
    out = copy.deepcopy(cfg) if isinstance(cfg, dict) else {}
    key = load_key(config_path, create=create_key)
    if key is None:
        return out
    for path, _env_name in secret_slots(out):
        value = get_path(out, path)
        if not _should_encrypt(value):
            continue
        set_path(out, path, encrypt_value(value, key))
    return out


def audit_plaintext(cfg: dict) -> list[str]:
    """列出仍是明文且非空的 slot 名（供启动日志/设置保存后的安全提示）。"""
    out: list[str] = []
    if not isinstance(cfg, dict):
        return out
    for path, _env_name in secret_slots(cfg):
        value = get_path(cfg, path)
        if _should_encrypt(value):
            out.append(_slot_label(path))
    return out


def redact_config(cfg: dict) -> dict:
    """返回脱敏副本：所有非空 slot 替换成 ***（日志/接口输出用，不修改入参）。"""
    out = copy.deepcopy(cfg) if isinstance(cfg, dict) else {}
    for path, _env_name in secret_slots(out):
        value = get_path(out, path)
        if isinstance(value, str) and value:
            set_path(out, path, MASK_MARKERS[0])
    return out
