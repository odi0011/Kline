"""线上验证：Telegram 表情包上传格式是否被接受。

用一个不存在的 chat_id 试发：只关心 Telegram 报的是「chat not found」
（-1004）而不是文件/格式错误。前者说明 multipart 和文件本身没问题，
真正发给用户时只会因为 chat_id 不同而成功。
"""
import sys

sys.path.insert(0, "/opt/kline-monitor")

import secrets_store
import yaml

from ai.stickers_media import split_stickers
from feed.telegram import TelegramBot

CFG = "/opt/kline-monitor/config.yaml"
cfg = secrets_store.decrypt_config(
    yaml.safe_load(open(CFG, encoding="utf-8")), CFG)
tok = (cfg.get("telegram") or {}).get("token") or ""
bot = TelegramBot(tok, [], None, None,
                  sticker_dir="/opt/kline-monitor/web/public/stickers",
                  attach_dir="/opt/kline-monitor/agent-attachments")
info = bot.verify()
print("getMe:", (info.get("bot") or {}).get("username"),
      "ok=", info.get("ok"))

body, refs = split_stickers("测试\n[sticker:笑死]",
                            "/opt/kline-monitor/web/public/stickers",
                            "/opt/kline-monitor/agent-attachments")
print("parsed body:", repr(body))
print("ref:", refs[0].local_path, refs[0].kind, "available:", refs[0].available)

# 不存在的 chat_id：预期 -1004 "chat not found"
res = bot.send_sticker(999999999999, refs[0])
print("send result:", str(res)[:400])
