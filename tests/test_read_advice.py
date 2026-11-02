"""read_advice：Agent 必须能读到胶囊那份建议，两边口径一致。

曾经的 bug：胶囊显示做空、Agent 说做多，因为 Agent 只看原始指标
自己判断，从没读过系统结论。
"""
import asyncio
import copy
import json

from tests.test_agent import make_manager, tool_call, tool_result, wait_for


ADVICE = {
    "symbol": "btcusdt", "action": "short", "side": "short", "score": 61.5,
    "gap": 12.0, "leverage": 20, "sl": 79500.0, "tp": 77800.0, "slPct": 0.012,
    "holdMin": 55, "reason": "评分偏空",
}


def test_read_advice_returns_the_same_object(tmp_path, monkeypatch):
    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        manager.advice_fn = lambda sym: ADVICE if sym == "btcusdt" else {}
        out = manager._read_advice({"symbol": "BTCUSDT"})
        assert out["available"] is True
        assert out["advice"] == ADVICE, "必须原样返回系统建议，不能自己算一遍"
        await manager.stop()
    asyncio.run(scenario())


def test_read_advice_is_empty_for_unwatched_symbol(tmp_path, monkeypatch):
    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        manager.advice_fn = lambda sym: {}
        out = manager._read_advice({"symbol": "dogeusdt"})
        assert out["available"] is False
        assert "暂无建议" in out["reason"]
        await manager.stop()
    asyncio.run(scenario())


def test_read_advice_rejects_unknown_arguments(tmp_path, monkeypatch):
    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        manager.advice_fn = lambda sym: ADVICE
        try:
            manager._read_advice({"symbol": "btcusdt", "leverage": 100})
        except ValueError as exc:
            assert "不支持的参数" in str(exc)
        else:
            raise AssertionError("多余的参数必须被拒绝")
        await manager.stop()
    asyncio.run(scenario())


def test_read_advice_without_backend_says_so(tmp_path, monkeypatch):
    """服务端没注入 advice_fn 时要明确说明，不能静默返回空对象。"""
    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        out = manager._read_advice({"symbol": "btcusdt"})
        assert out["available"] is False
        assert "尚未接入" in out["reason"]
        await manager.stop()
    asyncio.run(scenario())


def test_agent_is_told_to_read_advice_first(tmp_path, monkeypatch):
    """系统提示词要明确要求先读系统建议，并交代分歧。"""
    from ai.agent import SYSTEM_PROMPT
    assert "read_advice" in SYSTEM_PROMPT
    assert "复述它的方向与理由" in SYSTEM_PROMPT
    assert "分歧" in SYSTEM_PROMPT


def test_tool_is_exposed_and_dispatched(tmp_path, monkeypatch):
    """read_advice 必须在工具清单里，且执行时走的是自己的分支。"""
    from ai.agent_tools import TOOL_NAMES
    assert "read_advice" in TOOL_NAMES

    captured = []

    def provider(*args, **kwargs):
        captured.append(copy.deepcopy(args[2]))
        if kwargs.get("tool_choice") == "required":
            return tool_result(tool_call("read_advice", {"symbol": "btcusdt"}, "c1"))
        return {"ok": True, "reply": "系统建议做空，理由是评分偏空。",
                "usage": {"total_tokens": 10}, "model": "test-model"}

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch, provider=provider)
        manager.advice_fn = lambda sym: ADVICE
        await manager.ask("BTC 该做多还是做空", "test-model")
        assert await wait_for(lambda: not manager.chat_runners)
        await manager.stop()
    asyncio.run(scenario())

    # 工具结果里要有系统建议，模型才能复述
    tool_msgs = [m for m in captured[-1] if m.get("role") == "tool"]
    assert tool_msgs, "第二轮请求里应该有工具结果"
    payload = json.loads(tool_msgs[0]["content"])
    assert payload["data"]["advice"]["action"] == "short"
