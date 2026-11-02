"""多消息：界面上每句话都是独立气泡，模型侧才合并。

曾经的 bug：把两句拼成 "A（补充）B" 存进用户消息的 content，
界面上就显示成一个气泡里塞着「你是谁 /（补充）你好」。
"""
import asyncio

from ai.agent import _join_consecutive_users
from tests.test_agent import SlowHTTP, make_manager, wait_for


def test_consecutive_users_are_joined_for_the_model_only():
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "你是谁"},
        {"role": "user", "content": "你好"},
        {"role": "user", "content": "系统建议啥"},
        {"role": "assistant", "content": "x"},
    ]
    out = _join_consecutive_users(messages)
    assert [m["role"] for m in out] == ["system", "user", "assistant"]
    assert out[1]["content"] == "你是谁\n你好\n系统建议啥"


def test_no_extra_merge_when_alternating():
    """正常交替时不改动任何东西。"""
    messages = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
    ]
    assert _join_consecutive_users(messages) == messages


def test_multimodal_content_is_concatenated():
    """带图的用户消息后面接一条文字：两段都要留给模型。"""
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "看图"},
                                     {"type": "image_url", "image_url": {"url": "data:x"}}]},
        {"role": "user", "content": "这是什么形态"},
    ]
    out = _join_consecutive_users(messages)
    assert len(out) == 1
    parts = out[0]["content"]
    assert sum(1 for p in parts if p["type"] == "image_url") == 1
    assert any(p["type"] == "text" and "这是什么形态" in p["text"] for p in parts)


def test_ui_messages_stay_separate(tmp_path, monkeypatch):
    """端到端：连问三句，会话里就该有三条独立的用户消息。"""
    slow = SlowHTTP()

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch, provider=slow)
        try:
            assert (await manager.handle("chat", {"prompt": "你是谁"}))["ok"]
            assert await wait_for(lambda: slow.active == 1)
            assert (await manager.handle("chat", {"prompt": "你好"}))["ok"]
            assert await wait_for(lambda: slow.active == 1)
            assert (await manager.handle("chat", {"prompt": "系统建议啥"}))["ok"]
            assert await wait_for(lambda: slow.active == 1)
            slow.release.set()
            assert await wait_for(lambda: not manager.chat_runners)

            users = [m["content"] for m in manager.chat if m.get("role") == "user"]
            assert users == ["你是谁", "你好", "系统建议啥"], users
            # 任何一条都不能出现拼接痕迹
            assert not any("（补充）" in u for u in users), users
        finally:
            slow.release.set()
            await manager.stop()
    asyncio.run(scenario())


def test_model_sees_all_three_in_one_turn(tmp_path, monkeypatch):
    """端到端：模型那一侧要能看到全部三句，而不是只看到最后一句。"""
    slow = SlowHTTP()
    seen = []

    def provider(*args, **kwargs):
        messages = args[2] if len(args) >= 3 else kwargs.get("messages")
        users = [m for m in (messages or []) if m.get("role") == "user"]
        seen.append(users[-1]["content"] if users else "")
        return slow(*args, **kwargs)

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch, provider=provider)
        try:
            for text in ("你是谁", "你好", "系统建议啥"):
                assert (await manager.handle("chat", {"prompt": text}))["ok"]
                assert await wait_for(lambda: slow.active == 1)
            slow.release.set()
            assert await wait_for(lambda: not manager.chat_runners)
            last = seen[-1]
            for text in ("你是谁", "你好", "系统建议啥"):
                assert text in last, f"模型没看到 {text}: {last!r}"
        finally:
            slow.release.set()
            await manager.stop()
    asyncio.run(scenario())
