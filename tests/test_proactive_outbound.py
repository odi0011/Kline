"""主动发言必须推到外部渠道（Telegram / 微信）。

线上问题："自动发的消息，为啥没推到微信和飞机？"
根因：`_run_event_chat` 只把消息写进 self.chat 并广播给网页端，
Telegram 与 OpenClaw 两条链路只实现了「收」，没有「主动发」。
"""
import asyncio
import os
import pathlib

import pytest

from feed.openclaw_push import TARGET_RE, OpenClawPush

ROOT = pathlib.Path(__file__).resolve().parents[1]


# ---------- OpenClaw 推送 ----------

def test_target_is_learned_from_gateway_log(tmp_path):
    """收件人只能从网关日志里学（OpenClaw 不给主动推送提供名单）。"""
    log = tmp_path / "openclaw-2026-11-06.log"
    log.write_text(
        '{"1":"outbound: to=o9cq806izatz2G-o7tES1mk_uwKM@im.wechat textLen=5"}\n'
        '{"1":"inbound: from=o9cq806izatz2G-o7tES1mk_uwKM@im.wechat bodyLen=1"}\n',
        encoding="utf-8")
    push = OpenClawPush("/nonexistent/openclaw", "/tmp", "/usr/bin",
                        log_dir=str(tmp_path))
    assert push.targets() == ["o9cq806izatz2G-o7tES1mk_uwKM@im.wechat"]


def test_no_target_means_push_refuses(tmp_path):
    """没聊过就推不了——要说清楚原因，不能静默失败。"""
    push = OpenClawPush("/nonexistent/openclaw", "/tmp", "/usr/bin",
                        log_dir=str(tmp_path))
    res = push.send("测试")
    assert res["ok"] is False
    assert "微信" in res["error"]


def test_send_builds_expected_cli_args(tmp_path, monkeypatch):
    log = tmp_path / "log"
    log.write_text("to=abc@im.wechat\n", encoding="utf-8")
    push = OpenClawPush("/opt/openclaw/node_modules/.bin/openclaw",
                        "/opt/openclaw-home", "/opt/node26/bin",
                        log_dir=str(tmp_path))
    seen = {}

    def fake_run(args):
        seen["args"] = args
        return 0, "sent"

    monkeypatch.setattr(push, "_run", fake_run)
    res = push.send("形态命中", media_path="/tmp/x.gif")
    assert res["ok"] is True
    args = seen["args"]
    assert "--channel" in args and "openclaw-weixin" in args
    assert "--target" in args and "abc@im.wechat" in args
    assert "--media" in args and "/tmp/x.gif" in args
    assert "--message" in args and "形态命中" in args


def test_failure_is_recorded_not_raised(tmp_path, monkeypatch):
    (tmp_path / "log").write_text("to=abc@im.wechat\n", encoding="utf-8")
    push = OpenClawPush("/opt/x/openclaw", "/tmp", "/usr/bin", log_dir=str(tmp_path))
    monkeypatch.setattr(push, "_run", lambda args: (1, "boom"))
    res = push.send("hi")
    assert res["ok"] is False
    assert push.describe()["errors"] == 1
    assert "boom" in push.describe()["lastError"]


def test_sticker_is_sent_as_media_not_text(tmp_path, monkeypatch):
    """表情包要以图片发，不能把 [sticker:xxx] 留在正文里。"""
    from ai.stickers_media import split_stickers
    (tmp_path / "log").write_text("to=abc@im.wechat\n", encoding="utf-8")
    push = OpenClawPush("/opt/x/openclaw", "/tmp", "/usr/bin", log_dir=str(tmp_path))
    seen = {}

    def fake_run(args):
        seen["args"] = args
        return 0, "ok"

    monkeypatch.setattr(push, "_run", fake_run)
    body, refs = split_stickers("[sticker:崩溃]",
                                str(ROOT / "web" / "public" / "stickers"),
                                str(ROOT / "agent-attachments"))
    res = push.send_sticker(body, refs[0],
                            public_dir=str(ROOT / "web" / "public" / "stickers"),
                            dist_dir=str(ROOT / "web" / "dist" / "stickers"))
    assert res["ok"] is True
    args = seen["args"]
    assert "--media" in args
    media = args[args.index("--media") + 1]
    assert media.endswith("breakdown.png")
    assert not any("[sticker:" in a for a in args)


def test_target_regex_only_matches_wechat_ids():
    assert TARGET_RE.search("to=abc123@im.wechat").group(1) == "abc123@im.wechat"
    assert TARGET_RE.search("to=+15555550123") is None


# ---------- Agent 出站钩子 ----------

def test_agent_pushes_to_all_outbound_channels(tmp_path, monkeypatch):
    """一轮主动发言结束 → 每个注册渠道都收到同一条内容。"""
    from tests.test_agent import make_manager

    got = []

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        manager.outbound_fns = [
            lambda text: got.append(("tg", text)),
            lambda text: got.append(("wx", text)),
        ]
        await manager._push_outbound(["行，收工。", "明天见。"])
        await manager.stop()

    asyncio.run(scenario())
    assert [c for c, _ in got] == ["tg", "wx"]
    assert all("行，收工。" in t and "明天见。" in t for _, t in got)


def test_failing_channel_does_not_block_others(tmp_path, monkeypatch):
    """一个渠道挂了，另一个仍要收到——不能让 Telegram 故障拖累微信。"""
    from tests.test_agent import make_manager

    got = []

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)

        def boom(text):
            raise RuntimeError("telegram down")

        manager.outbound_fns = [boom, lambda text: got.append(text)]
        await manager._push_outbound(["测试"])
        await manager.stop()

    asyncio.run(scenario())
    assert got == ["测试"]


def test_empty_reply_pushes_nothing(tmp_path, monkeypatch):
    from tests.test_agent import make_manager

    got = []

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        manager.outbound_fns = [lambda text: got.append(text)]
        await manager._push_outbound(["", "   "])
        await manager.stop()

    asyncio.run(scenario())
    assert got == []


def test_main_wires_both_channels():
    """main 里必须真的注册两个出站，否则等于没做。"""
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "agent.outbound_fns = [_push_telegram, _push_weixin]" in src
    # 微信侧表情包要当媒体发
    assert "push.send_sticker(" in src


# ---------- Telegram 主动推送的收件人 ----------

def test_telegram_chat_is_persisted_across_restart(tmp_path):
    """"谁聊过"必须落盘：重启后还要能主动推送。

    线上实测：服务重启后 last_chats 是空的、白名单也为空，
    主动消息哪里都推不出去。
    """
    from feed.telegram import TelegramBot

    path = tmp_path / "tg-chats.json"
    bot = TelegramBot("tok", [], None, None, chats_path=str(path))
    bot.remember_chat(123456)
    assert path.exists()

    # 模拟重启：新实例从同一个文件恢复
    again = TelegramBot("tok", [], None, None, chats_path=str(path))
    assert again.last_chats == ["123456"]


def test_telegram_push_targets_learned_chats(tmp_path, monkeypatch):
    from feed.telegram import TelegramBot

    path = tmp_path / "tg-chats.json"
    bot = TelegramBot("tok", [], None, None, chats_path=str(path))
    bot.remember_chat("111")
    sent = []
    monkeypatch.setattr(bot, "send_reply",
                        lambda chat_id, text: sent.append((chat_id, str(text))) or {"ok": True})
    res = bot.push("形态命中")
    assert res["ok"] is True
    assert sent and sent[0][0] == "111"


def test_telegram_push_without_history_explains(tmp_path):
    from feed.telegram import TelegramBot

    bot = TelegramBot("tok", [], None, None, chats_path=str(tmp_path / "x.json"))
    res = bot.push("hi")
    assert res["ok"] is False and "会话" in res["error"]
