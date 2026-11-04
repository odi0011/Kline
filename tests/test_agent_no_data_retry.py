"""模型"没查就说查不到"必须被打回重查一次。

线上实测：问"这周赚了多少"，模型一次工具都没调，回了句
"数据线断了，读不到本周成交和权益"——read_equity / read_trades
明明都在。首轮 tool_choice=required，出现这种情况说明它擅自
跳过了查数据这一步；直接采信等于把"没查"当成"查不到"。
"""
import asyncio

from tests.test_agent import make_manager, tool_call, tool_result, wait_for


def _provider(replies):
    """按顺序返回预设回复的 provider；记录每次调用的 tool_choice。"""
    calls = []

    def provider(config, model, messages, **kwargs):
        calls.append({"tool_choice": kwargs.get("tool_choice"),
                      "messages": list(messages)})
        reply = replies[min(len(calls) - 1, len(replies) - 1)]
        return {"ok": True, "reply": reply, "usage": {"total_tokens": 10},
                "model": "test-model"}

    return provider, calls


def test_no_data_claim_without_tools_is_retried(tmp_path, monkeypatch):
    """第一轮空手说"读不到" → 打回重查；第二轮正常调用工具。"""
    calls = []

    def provider(config, model, messages, **kwargs):
        calls.append({"tool_choice": kwargs.get("tool_choice"),
                      "messages": list(messages)})
        if len(calls) == 1:
            return {"ok": True, "reply": "数据线断了，读不到本周成交和权益。",
                    "usage": {"total_tokens": 10}, "model": "test-model"}
        if kwargs.get("tool_choice") == "required":
            return tool_result(tool_call("read_equity", {"days": 7}))
        return {"ok": True, "reply": "这周亏了 11.16。",
                "usage": {"total_tokens": 10}, "model": "test-model"}

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch, provider=provider)
        await manager.ask("这周赚了多少", "test-model")
        assert await wait_for(lambda: not manager.chat_runners)
        answer = [m for m in manager.chat if m.get("role") == "assistant"
                  and m.get("content")][-1]
        assert "11.16" in answer["content"], answer
        assert "数据线断了" not in answer["content"]
        # 必须真的重问了一轮，且重问时把可用工具列给了模型
        assert len(calls) >= 2, calls
        retry_msgs = " ".join(str(m.get("content")) for m in calls[1]["messages"])
        assert "read_equity" in retry_msgs and "read_trades" in retry_msgs
        assert "还没有调用任何工具" in retry_msgs
        await manager.stop()
    asyncio.run(scenario())


def test_no_data_retry_only_once(tmp_path, monkeypatch):
    """重试只有一次：模型第二次还这么说就采信，不能无限打回。"""
    def provider(config, model, messages, **kwargs):
        provider.calls.append(1)
        return {"ok": True, "reply": "读不到数据。", "usage": {"total_tokens": 5},
                "model": "test-model"}
    provider.calls = []

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch, provider=provider)
        await manager.ask("这周赚了多少", "test-model")
        assert await wait_for(lambda: not manager.chat_runners)
        assert len(provider.calls) == 2, f"应该只重试一次，实际 {len(provider.calls)} 次"
        await manager.stop()
    asyncio.run(scenario())


def test_normal_short_answer_is_not_retried(tmp_path, monkeypatch):
    """普通短答（不含"读不到"）不能被这条规则误伤。"""
    def provider(config, model, messages, **kwargs):
        provider.calls.append(1)
        return {"ok": True, "reply": "空。", "usage": {"total_tokens": 5},
                "model": "test-model"}
    provider.calls = []

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch, provider=provider)
        await manager.ask("BTC能做吗", "test-model")
        assert await wait_for(lambda: not manager.chat_runners)
        assert len(provider.calls) == 1, provider.calls
        await manager.stop()
    asyncio.run(scenario())


def test_no_data_detector_patterns():
    from ai.agent import _says_no_data
    for text in ("数据线断了，读不到本周成交和权益", "这个我查不到",
                 "没有这个数据源", "接口挂了", "无法获取权益",
                 "no access to such data"):
        assert _says_no_data(text), text
    for text in ("空。79500 止损。", "BTC 现在 78486，多头。", "这周亏了 11.16。"):
        assert not _says_no_data(text), text
