"""线上排查：ODDD 的 OpenAI 兼容接口为什么还是 404。"""
import sys

sys.path.insert(0, "/opt/kline-monitor")

import secrets_store
import yaml

CFG = "/opt/kline-monitor/config.yaml"
raw = yaml.safe_load(open(CFG, encoding="utf-8"))
try:
    cfg = secrets_store.decrypt_config(raw, CFG)
except Exception as exc:
    print("decrypt failed:", exc)
    cfg = raw

oc = cfg.get("openclaw")
print("openclaw in decrypted cfg:", oc)
print("raw keys:", [k for k in raw if k == "openclaw"])
print("raw openclaw:", raw.get("openclaw"))
