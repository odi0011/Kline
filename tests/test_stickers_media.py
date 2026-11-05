"""表情包在外部渠道（Telegram / OpenClaw 微信）的落地。

线上具体问题：Telegram 里收到的是 `[sticker:崩溃]` 这串代码，不是图。
根因：`TelegramBot.send()` 只调 sendMessage，把标记当普通文本发出去。
"""
import asyncio
import pathlib

import pytest

from ai.stickers_media import STICKER_FILES, split_stickers, to_markdown
from feed.telegram import TelegramBot

ROOT = pathlib.Path(__file__).resolve().parents[1]
PUB = str(ROOT / "web" / "public" / "stickers")
ATT = str(ROOT / "agent-attachments")


def test_split_strips_marker_from_text():
    """标记必须从正文里剥掉，否则聊天里就是那串代码。"""
    body, refs = split_stickers("行，收工。\n[sticker:点赞]", PUB, ATT)
    assert body == "行，收工。"
    assert len(refs) == 1 and refs[0].inner == "点赞"


def test_sticker_only_reply_has_empty_body():
    body, refs = split_stickers("[sticker:无语]", PUB, ATT)
    assert body == ""
    assert len(refs) == 1


def test_builtin_sticker_file_resolves():
    _, refs = split_stickers("[sticker:崩溃]", PUB, ATT)
    assert refs[0].available
    assert refs[0].local_path.endswith("breakdown.png")
    assert refs[0].kind == "photo"


def test_gif_uses_animation_kind():
    """GIF 必须走 sendAnimation：sendPhoto 只会发首帧，图不动。"""
    _, refs = split_stickers("[sticker:笑死]", PUB, ATT)
    assert refs[0].kind == "animation"


def test_unknown_name_is_not_available():
    _, refs = split_stickers("[sticker:不存在的名字]", PUB, ATT)
    assert not refs[0].available


def test_marker_payload_cannot_escape_attach_dir():
    """标记是模型写的，URL 里塞 ../.. 不能读到任意文件。"""
    payload = "[sticker:url=%2Fagent-attachments%2F..%2F..%2Fconfig.yaml&w=1&h=1]"
    _, refs = split_stickers(payload, PUB, ATT)
    assert not refs[0].available


def test_every_declared_sticker_file_exists():
    """名字→文件名映射里每一条都要真的存在于 web/public/stickers。"""
    base = ROOT / "web" / "public" / "stickers"
    for name, filename in STICKER_FILES.items():
        assert (base / filename).is_file(), f"{name} → {filename} 不存在"


def test_frontend_names_match_media_map():
    """前后端名字必须一一对应（两边手工维护，最容易漏）。"""
    path = ROOT / "web/src/components/arc/chat-dock/stickers.jsx"
    if not path.exists():
        return
    import re
    src = path.read_text(encoding="utf-8")
    block = src.split("export const STICKERS")[1].split("export const STICKER_NAMES")[0]
    front = set(re.findall(r"\n  ([^\s:/]+)\s*:", block))
    assert front == set(STICKER_FILES), (
        f"前端多出 {front - set(STICKER_FILES)}，后端多出 {set(STICKER_FILES) - front}")


def test_to_markdown_renders_public_url():
    """给只能收文本的渠道（微信）：标记换成真图片链接，不是代码。"""
    out = to_markdown("收工。\n[sticker:点赞]", "https://od1.site", PUB, ATT)
    assert "[sticker:" not in out
    assert "https://od1.site/stickers/thumbsup.gif" in out
    assert out.startswith("收工。")


def test_to_markdown_handles_no_sticker():
    assert to_markdown("就一句话。", "https://od1.site", PUB, ATT) == "就一句话。"


# ---------- Telegram 出站 ----------

def _bot(calls):
    bot = TelegramBot("tok", [], None, None, sticker_dir=PUB, attach_dir=ATT)

    def fake_request(method, params=None):
        calls.append((method, params or {}))
        return {"ok": True, "result": {}}

    def fake_upload(method, field, chat_id, path, mime):
        calls.append((method, {"file": path}))
        return {"ok": True, "result": {}}

    bot._request = fake_request
    bot._upload = fake_upload
    return bot


def test_telegram_sends_text_then_sticker():
    """正文和表情分开发：先 sendMessage，再 sendAnimation。"""
    calls = []
    bot = _bot(calls)
    bot.send_reply(123, "行，收工。\n[sticker:点赞]")
    assert calls[0][0] == "sendMessage"
    assert calls[0][1]["text"] == "行，收工。"
    assert calls[1][0] == "sendAnimation"
    assert calls[1][1]["file"].endswith("thumbsup.gif")
    assert bot.media_sent == 1


def test_telegram_png_uses_send_photo():
    calls = []
    bot = _bot(calls)
    bot.send_reply(1, "崩了\n[sticker:崩溃]")
    assert calls[1][0] == "sendPhoto"


def test_telegram_sticker_only_sends_no_empty_text():
    """只发表情时不能多发一条"（空回复）"。"""
    calls = []
    bot = _bot(calls)
    bot.send_reply(1, "[sticker:无语]")
    methods = [c[0] for c in calls]
    assert "sendMessage" not in methods
    assert methods == ["sendAnimation"]


def test_telegram_remote_giphy_uses_url_not_upload():
    calls = []
    bot = _bot(calls)
    bot.send_reply(1, "[sticker:url=https%3A%2F%2Fmedia.giphy.com%2Fa.gif&w=1&h=1]")
    assert calls[0][0] == "sendAnimation"
    assert calls[0][1].get("animation", "").startswith("https://media.giphy.com/")


def test_telegram_text_unaffected_without_sticker():
    calls = []
    bot = _bot(calls)
    bot.send_reply(1, "就是一句话。")
    assert calls == [("sendMessage", {"chat_id": 1, "text": "就是一句话。",
                                      "disable_web_page_preview": True})]
