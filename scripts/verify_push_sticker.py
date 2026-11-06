"""线上验证：通过 OpenClaw 把表情包作为**图片**推到微信。"""
import sys

sys.path.insert(0, "/opt/kline-monitor")

from ai.stickers_media import split_stickers
from feed.openclaw_push import OpenClawPush

PUB = "/opt/kline-monitor/web/public/stickers"
DIST = "/opt/kline-monitor/web/dist/stickers"
ATT = "/opt/kline-monitor/agent-attachments"

push = OpenClawPush("/opt/openclaw/node_modules/.bin/openclaw",
                    "/opt/openclaw-home", "/opt/node26/bin")
body, refs = split_stickers("形态命中测试\n[sticker:崩溃]", PUB, ATT)
print("body:", repr(body), "| refs:", len(refs))
if refs:
    print("file:", refs[0].local_path, "exists:", refs[0].available)
    res = push.send_sticker(body, refs[0], public_dir=PUB, dist_dir=DIST)
    print("sticker push ok:", res.get("ok"))
    for row in res.get("results") or []:
        print("  target:", row.get("target"), "code:", row.get("code"))
        print("  out:", str(row.get("out"))[:200])
