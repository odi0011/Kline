"""找出哪个 OpenClaw 版本还保留微信插件需要的 plugin-sdk 导出。

官方插件 @tencent-weixin/openclaw-weixin 依赖这些子路径：
  account-id / channel-config-schema / channel-message / command-auth /
  config-runtime / hook-runtime / infra-runtime / plugin-runtime
2026.11.1 里 infra-runtime 等已从 exports 移除，所以必须往下找。
"""
import json
import re
import urllib.request


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


WANT = ["account-id", "channel-config-schema", "channel-message", "command-auth",
        "config-runtime", "hook-runtime", "infra-runtime", "plugin-runtime"]

data = get("https://registry.npmjs.org/openclaw")
versions = data["versions"]
stable = [v for v in versions if v[0].isdigit() and "-" not in v]


def key(v):
    return [int(x) for x in re.findall(r"\d+", v)[:3]]


cands = sorted([v for v in stable if v.startswith(("2026.",))], key=key)
print("总版本数:", len(cands))

for v in reversed(cands[-40:]):
    meta = versions[v]
    exports = meta.get("exports")
    node_needed = (meta.get("engines") or {}).get("node")
    if not exports:
        print(f"{v:14s} node={node_needed} exports=none")
        continue
    keys = set(exports.keys())
    missing = [w for w in WANT if f"./plugin-sdk/{w}" not in keys]
    flag = "✓ 全都有" if not missing else f"缺 {missing}"
    print(f"{v:14s} node={node_needed} {flag}")
    if not missing:
        print("\n>>> 可用版本:", v)
        break
