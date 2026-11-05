"""Agent 聊天记录统计：表情包到底发了多少、在什么场景下没发。

只读。用法：python scripts/chatreport.py [条数]
"""
import sys

import pymysql
import yaml

import secrets_store

CFG = "/opt/kline-monitor/config.yaml" if len(sys.argv) > 2 else "config.yaml"
LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 80


def connect(path: str):
    cfg = yaml.safe_load(open(path, encoding="utf-8"))
    try:
        cfg = secrets_store.decrypt_config(cfg, path)
    except Exception:
        pass
    my = cfg.get("mysql") or {}
    return pymysql.connect(
        host=my.get("host", "127.0.0.1"), port=int(my.get("port", 3306)),
        user=my.get("user"), password=my.get("password"),
        database=my.get("database"), charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor)


def main() -> None:
    conn = connect(CFG)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) c FROM agent_chat")
    print("总消息:", cur.fetchone()["c"])
    cur.execute(
        "SELECT msg_id, ts, seq, role, content, data FROM agent_chat "
        "ORDER BY ts DESC, seq DESC LIMIT %s", (LIMIT,))
    rows = list(reversed(cur.fetchall()))
    with_sticker = sum(
        1 for r in rows
        if "[sticker:" in (r.get("content") or "") + str(r.get("data") or ""))
    replies = [r for r in rows if r.get("role") == "assistant"]
    print(f"最近 {len(rows)} 条消息，其中 {len(replies)} 条是 Agent 回复，"
          f"带表情包的 {with_sticker} 条 "
          f"({with_sticker / max(1, len(replies)) * 100:.0f}%)")
    print("--- 明细 ---")
    for r in rows:
        text = (r.get("content") or "").replace("\n", " / ")
        mark = "[图]" if "[sticker:" in text else "    "
        print(mark, r["role"][:4], text[:90])


if __name__ == "__main__":
    main()
