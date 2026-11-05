"""把线上 agent_chat 导出成 JSON，供 scripts/sticker_research.py 离线分析。"""
import json
import sys

import pymysql
import yaml

import secrets_store

CFG = "/opt/kline-monitor/config.yaml"
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/transcript.json"
LIMIT = int(sys.argv[2]) if len(sys.argv) > 2 else 300

cfg = yaml.safe_load(open(CFG, encoding="utf-8"))
try:
    cfg = secrets_store.decrypt_config(cfg, CFG)
except Exception:
    pass
my = cfg.get("mysql") or {}
conn = pymysql.connect(host=my.get("host", "127.0.0.1"),
                       port=int(my.get("port", 3306)),
                       user=my.get("user"), password=my.get("password"),
                       database=my.get("database"), charset="utf8mb4",
                       cursorclass=pymysql.cursors.DictCursor)
cur = conn.cursor()
cur.execute("SELECT ts, seq, role, content FROM agent_chat "
            "ORDER BY ts DESC, seq DESC LIMIT %s", (LIMIT,))
rows = list(reversed(cur.fetchall()))
out = [{"ts": r["ts"], "role": r["role"], "content": r.get("content") or ""}
       for r in rows if r["role"] in ("user", "assistant")]
json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"wrote {OUT}: {len(out)} messages "
      f"({sum(1 for r in out if r['role'] == 'user')} user / "
      f"{sum(1 for r in out if r['role'] == 'assistant')} assistant)")
