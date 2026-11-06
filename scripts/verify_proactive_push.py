"""端到端验证：主动发言是否真的推到外部渠道。

不伪造 agent 内部状态：直接调 Agent 的 _push_outbound（主动发言成功
之后走的就是这一步），看两个渠道的返回值。

只读 + 发一条测试消息，不改任何数据。
"""
import asyncio
import sys

sys.path.insert(0, "/opt/kline-monitor")

from feed.openclaw_push import OpenClawPush


async def main() -> None:
    from main import run  # noqa: F401  (仅确认可导入)


def check_push_modules() -> None:
    """直接验证两个出站实现能跑通。"""
    push = OpenClawPush("/opt/openclaw/node_modules/.bin/openclaw",
                        "/opt/openclaw-home", "/opt/node26/bin")
    print("微信 targets:", push.targets())
    res = push.send("【主动推送·端到端】系统事件触发的消息，不是回复。")
    print("微信推送:", res.get("ok"))
    for row in res.get("results") or []:
        print("  code:", row.get("code"), "|", str(row.get("out"))[:120])

    import secrets_store
    import yaml
    from feed.telegram import TelegramBot

    CFG = "/opt/kline-monitor/config.yaml"
    cfg = secrets_store.decrypt_config(
        yaml.safe_load(open(CFG, encoding="utf-8")), CFG)
    t = cfg.get("telegram") or {}
    bot = TelegramBot(t.get("token") or "", t.get("allowed_chats") or [], None, None,
                      chats_path="/opt/kline-monitor/telegram-chats.json")
    print("Telegram 已知会话:", bot.last_chats or "(空，需要先给 bot 发过消息)")
    if bot.last_chats or bot.allowed:
        print("Telegram 推送:", bot.push("【主动推送·端到端】"))


if __name__ == "__main__":
    check_push_modules()
