"""配置密钥加解密 CLI（Fernet）。

用法（仓库根目录执行）：
  python tools/encrypt_config.py              # 加密所有密钥项（先备份，再写回，权限 0600）
  python tools/encrypt_config.py --decrypt    # 解密 enc:v1: 值写回明文（先备份）
  python tools/encrypt_config.py --check      # 只检查状态/权限，不写文件
  python tools/encrypt_config.py --config config.yaml --key-file /path/secrets.key

主密钥优先级：环境变量 KLINE_SECRET_KEY > --key-file > 配置文件同目录 secrets.key。
默认模式会自动生成 secrets.key（0600）；备份文件 config.yaml.bak.<时间戳>
可能包含明文密钥，确认配置可用后请安全删除。
"""
from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
import time
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import secrets_store  # noqa: E402
from secrets_store import (  # noqa: E402
    MASK_MARKERS, SecretsError, _new_key, _read_key_file, _slot_label,
    _write_key_file, get_path, harden_permissions, is_encrypted, secret_slots,
    set_path,
)

STATE_LABELS = {
    "encrypted": "已加密",
    "plaintext": "明文",
    "masked": "掩码（跳过）",
    "empty": "空",
    "missing": "缺失",
    "non-string": "非文本",
}


def _slot_state(value) -> str:
    """一个密钥项的状态：encrypted / plaintext / masked / empty / missing / non-string。"""
    if value is None:
        return "missing"
    if not isinstance(value, str):
        return "non-string"
    if is_encrypted(value):
        return "encrypted"
    if any(marker in value for marker in MASK_MARKERS):
        return "masked"
    if not value.strip():
        return "empty"
    return "plaintext"


def _mode_text(path: str) -> str:
    """权限文本（0o600）或 missing/unknown；Windows 上 chmod 语义有限，仅用于展示。"""
    if not path or not os.path.exists(path):
        return "missing"
    try:
        return "0o%o" % stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return "unknown"


def _too_wide(path: str) -> bool:
    """POSIX 下检查权限是否过宽（组/其他用户有权限）；Windows 无法可靠判断，返回 False。"""
    if os.name == "nt" or not path or not os.path.exists(path):
        return False
    try:
        return bool(stat.S_IMODE(os.stat(path).st_mode) & 0o077)
    except OSError:
        return False


def _resolve_key(config_path: str, key_file, create: bool) -> tuple[bytes | None, str, str]:
    """解析主密钥：返回 (key, 密钥文件路径, 来源 env/file/created/missing)。"""
    default_path = secrets_store._key_path(config_path)
    path = os.path.abspath(str(key_file)) if key_file else default_path
    if os.environ.get(secrets_store.ENV_KEY) is not None:
        return secrets_store.load_key(config_path, create=False), path, "env"
    if os.path.exists(path):
        return _read_key_file(path), path, "file"
    if not create:
        return None, path, "missing"
    return _write_key_file(path, _new_key()), path, "created"


def _read_yaml(config_path: str) -> dict:
    try:
        with open(config_path, encoding="utf-8") as handle:
            cfg = yaml.safe_load(handle) or {}
    except OSError as exc:
        raise SecretsError("读取配置失败：%s（%s）" % (config_path, exc)) from exc
    except yaml.YAMLError as exc:
        raise SecretsError("YAML 解析失败：%s" % exc) from exc
    if not isinstance(cfg, dict):
        raise SecretsError("配置格式无效：顶层需为映射（dict）")
    return cfg


def _write_yaml(config_path: str, cfg: dict) -> None:
    temporary = config_path + ".tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            yaml.safe_dump(cfg, handle, allow_unicode=True, sort_keys=False)
        harden_permissions(temporary)
        os.replace(temporary, config_path)
    except OSError as exc:
        raise SecretsError("写入配置失败：%s（%s）" % (config_path, exc)) from exc
    harden_permissions(config_path)


def _backup(config_path: str) -> str:
    """复制一份带时间戳的备份（保留原权限并尽量收紧）；返回备份路径。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    candidate = "%s.bak.%s" % (config_path, stamp)
    index = 1
    while os.path.exists(candidate):
        candidate = "%s.bak.%s-%d" % (config_path, stamp, index)
        index += 1
    try:
        shutil.copy2(config_path, candidate)
        harden_permissions(candidate)
    except OSError as exc:
        raise SecretsError("创建配置备份失败：%s（%s）" % (candidate, exc)) from exc
    return candidate


def _apply(cfg: dict, key, decrypt: bool) -> list[str]:
    """在内存里加密或解密所有 slot，返回变更的 slot 名；失败不落盘、不留备份。"""
    changed: list[str] = []
    for path, _env_name in secret_slots(cfg):
        value = get_path(cfg, path)
        if decrypt:
            if not is_encrypted(value):
                continue
            if key is None:
                raise SecretsError(
                    "配置项 %s 已加密但缺少主密钥：请设置 KLINE_SECRET_KEY 或提供密钥文件"
                    % _slot_label(path))
            set_path(cfg, path, secrets_store.decrypt_value(value, key))
            changed.append(_slot_label(path))
        else:
            if (not isinstance(value, str) or not value.strip()
                    or is_encrypted(value)
                    or any(marker in value for marker in MASK_MARKERS)):
                continue
            if key is None:
                raise SecretsError(
                    "没有可用主密钥：默认模式本应生成，请检查文件权限或设置 KLINE_SECRET_KEY")
            set_path(cfg, path, secrets_store.encrypt_value(value, key))
            changed.append(_slot_label(path))
    return changed


def _check(config_path: str, cfg: dict, key_file) -> dict:
    """只读检查：返回每个 slot 的状态、主密钥是否存在、权限是否过宽。不写文件。"""
    key, key_path, key_source = _resolve_key(config_path, key_file, create=False)
    slots = [
        {"name": _slot_label(path), "state": _slot_state(get_path(cfg, path))}
        for path, _env_name in secret_slots(cfg)
    ]
    at_risk = [slot["name"] for slot in slots if slot["state"] == "plaintext"]
    warnings: list[str] = []
    if key is None:
        warnings.append("缺少主密钥：加密/解密不可用（运行本脚本默认模式会自动生成）")
    if at_risk:
        warnings.append("以下配置项仍是明文：" + ", ".join(at_risk))
    if _too_wide(config_path):
        warnings.append("%s 权限过宽，建议 0600（本脚本写入时会自动收紧）" % config_path)
    if os.path.exists(key_path) and _too_wide(key_path):
        warnings.append("%s 权限过宽，建议 0600" % key_path)
    return {
        "action": "check",
        "config": config_path,
        "key": key is not None,
        "keyPath": key_path,
        "keySource": key_source,
        "slots": slots,
        "atRisk": at_risk,
        "configMode": _mode_text(config_path),
        "keyMode": _mode_text(key_path),
        "warnings": warnings,
    }


def run(config_path="config.yaml", decrypt: bool = False,
        check: bool = False, key_file=None) -> dict:
    """加密/解密/检查配置。返回可直接打印或断言的结果字典；出错抛 SecretsError。"""
    if decrypt and check:
        raise SecretsError("--decrypt 与 --check 不能同时使用")
    path = os.path.abspath(str(config_path or "config.yaml"))
    if not os.path.isfile(path):
        raise SecretsError("配置文件不存在：%s" % path)
    cfg = _read_yaml(path)
    if check:
        return _check(path, cfg, key_file)
    if not decrypt:
        # 安全闸：配置里已有加密值却找不到密钥时，绝不能生成新密钥——
        # 那会把旧密文永久锁死（新密钥解不开旧数据），宁可报错让人先恢复密钥。
        encrypted = [slot for slot, _env in secret_slots(cfg)
                     if is_encrypted(get_path(cfg, slot))]
        if encrypted:
            existing_key, _key_path, _source = _resolve_key(path, key_file, create=False)
            if existing_key is None:
                raise SecretsError(
                    "配置里已有 %d 个加密值但找不到主密钥：拒绝生成新密钥"
                    "（否则旧密文将永久无法解密）。请先恢复 KLINE_SECRET_KEY 或密钥文件。"
                    % len(encrypted))
    key, key_path, key_source = _resolve_key(path, key_file, create=not decrypt)
    # 先在内存里完成转换：解不开/加密失败时不写坏配置、也不留误导性备份。
    changed = _apply(cfg, key, decrypt=decrypt)
    backup = _backup(path)
    _write_yaml(path, cfg)
    return {
        "action": "decrypt" if decrypt else "encrypt",
        "config": path,
        "backup": backup,
        "keyPath": key_path,
        "keySource": key_source,
        "count": len(changed),
        "changed": changed,
    }


def _print_report(info: dict) -> None:
    if info.get("action") == "check":
        print("配置文件：%s" % info["config"])
        print("主密钥：%s（%s，来源 %s）"
              % ("存在" if info["key"] else "缺失", info["keyPath"], info["keySource"]))
        print("权限：config=%s key=%s" % (info["configMode"], info["keyMode"]))
        for slot in info["slots"]:
            print("  %s: %s" % (slot["name"], STATE_LABELS.get(slot["state"], slot["state"])))
        for warning in info["warnings"]:
            print("[提示] %s" % warning)
        return
    verb = "解密" if info["action"] == "decrypt" else "加密"
    print("已%s %d 个配置项：%s" % (verb, info["count"], info["config"]))
    print("主密钥：%s（来源 %s）" % (info["keyPath"], info["keySource"]))
    print("备份文件：%s" % info["backup"])
    if info["action"] == "encrypt":
        print("提醒：备份文件包含明文密钥，确认配置可用后请安全删除。")
        print("提醒：请单独备份 secrets.key；密钥丢失后配置里的加密值将无法解密。")
    else:
        print("提醒：备份文件是加密内容，可保留；确认无误后可删除。")


def main(argv=None) -> int:
    """CLI 入口：返回退出码（0 成功，1 失败并打印中文错误）。"""
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")
            except (AttributeError, OSError):
                pass
    parser = argparse.ArgumentParser(
        description="K线配置密钥加解密工具（Fernet）。默认把明文密钥加密后写回 config.yaml。")
    parser.add_argument("--config", default="config.yaml",
                        help="配置文件路径（默认 config.yaml）")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--decrypt", action="store_true",
                       help="解密 enc:v1: 值并写回明文（会先备份）")
    group.add_argument("--check", action="store_true",
                       help="只检查密钥项状态与权限，不写文件")
    parser.add_argument("--key-file", default=None,
                        help="主密钥文件（默认配置文件同目录的 secrets.key）")
    args = parser.parse_args(argv)
    try:
        info = run(args.config, decrypt=args.decrypt, check=args.check,
                   key_file=args.key_file)
    except SecretsError as exc:
        print("错误：%s" % exc, file=sys.stderr)
        return 1
    except OSError as exc:
        print("错误：文件操作失败：%s" % exc, file=sys.stderr)
        return 1
    _print_report(info)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
