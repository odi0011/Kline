"""纯附件消息：不能凭空多出一条"（见附件）"文字气泡。

曾经的问题：前端在 prompt 为空时塞了占位文字 "（见附件）"，
它变成一条真的用户消息，气泡里就多出一行没意义的文字。
"""
import asyncio
import base64

import pytest

from tests.test_agent import make_manager, wait_for

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 40).decode()


def _attachment():
    return [{"kind": "image", "name": "chart.png", "mime": "image/png",
             "data": f"data:image/png;base64,{PNG}"}]


def test_attachment_only_message_has_empty_content(tmp_path, monkeypatch):
    """只发附件时，会话里那条 user 消息的 content 必须是空串。"""
    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        await manager.ask("", "", _attachment())
        assert await wait_for(lambda: not manager.chat_runners)
        users = [m for m in manager.chat if m.get("role") == "user"]
        assert len(users) == 1
        assert users[0]["content"] == "", f"不该出现占位文字: {users[0]['content']!r}"
        assert users[0]["attachments"], "附件引用要保留"
        await manager.stop()
    asyncio.run(scenario())


def test_attachment_only_still_reaches_the_model(tmp_path, monkeypatch):
    """会话里 content 为空，但给模型的那份要有内容（带图的多模态块）。"""
    captured = []

    def provider(*args, **kwargs):
        from tests.test_agent import completion
        captured.append(args[2] if len(args) >= 3 else kwargs.get("messages"))
        return completion(*args, **kwargs)

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch, provider=provider)
        await manager.ask("", "", _attachment())
        assert await wait_for(lambda: not manager.chat_runners)
        await manager.stop()
    asyncio.run(scenario())

    user_msg = [m for m in captured[0] if m["role"] == "user"][-1]
    content = user_msg["content"]
    assert isinstance(content, list), "带图片时应为多模态 content"
    text_part = next(p for p in content if p.get("type") == "text")
    assert text_part["text"].strip(), "给模型的文本不能是空串，否则部分服务商会拒绝"
    assert any(p.get("type") == "image_url" for p in content)


def test_empty_message_without_attachments_is_rejected(tmp_path, monkeypatch):
    """既没文字也没附件：仍然要报错，不能发一条空消息。"""
    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        with pytest.raises(ValueError):
            await manager.ask("", "", None)
        assert manager.chat == []
        await manager.stop()
    asyncio.run(scenario())


def test_merge_accepts_previous_message_without_text(tmp_path, monkeypatch):
    """上一条只有附件（content 为空）时，追加一句仍然能正常合并。"""
    from tests.test_agent import SlowHTTP, tool_call, tool_result

    slow = SlowHTTP()
    prompts = []

    def provider(*args, **kwargs):
        messages = args[2] if len(args) >= 3 else kwargs.get("messages")
        for item in reversed(messages or []):
            if item.get("role") == "user":
                content = item.get("content")
                if isinstance(content, list):
                    content = next((p.get("text") for p in content if p.get("type") == "text"), "")
                prompts.append(str(content or ""))
                break
        return slow(*args, **kwargs)

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch, provider=provider)
        try:
            assert (await manager.handle("chat", {"prompt": "", "attachments": _attachment()}))["ok"]
            assert await wait_for(lambda: slow.active == 1)
            assert (await manager.handle("chat", {"prompt": "这是什么形态"}))["ok"]
            assert await wait_for(lambda: slow.active == 1)
            slow.release.set()
            assert await wait_for(lambda: not manager.chat_runners)
            joined = prompts[-1]
            assert "这是什么形态" in joined, f"补充的话没进上下文: {joined!r}"
            users = [m for m in manager.chat if m.get("role") == "user"]
            assert len(users) == 1, f"合并后不该残留两条: {users!r}"
            assert users[0]["content"] == "这是什么形态"
            assert users[0].get("attachments"), "上一条的附件要带到合并后的消息里"
        finally:
            slow.release.set()
            await manager.stop()
    asyncio.run(scenario())
