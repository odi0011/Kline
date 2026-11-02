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
        manager.symbols_fn = lambda: ["btcusdt"]
        out = manager._read_advice({"symbol": "dogeusdt"})
        assert out["available"] is False
        assert out["monitored"] is False
        assert "不在当前监控列表" in out["reason"]
        await manager.stop()
    asyncio.run(scenario())


def test_read_advice_distinguishes_not_ready_from_not_watched(tmp_path, monkeypatch):
    """在监控列表但还没算出来 ≠ 不在监控范围。

    混成一句话会让模型说"BTC 已不在监控范围"（实际只是回补没跟上）。
    """
    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        manager.advice_fn = lambda sym: {}
        manager.symbols_fn = lambda: ["btcusdt"]
        out = manager._read_advice({"symbol": "btcusdt"})
        assert out["available"] is False
        assert out["monitored"] is True
        assert "还没算出来" in out["reason"]
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
    assert "方向和关键价位" in SYSTEM_PROMPT
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


def test_read_advice_reports_age(tmp_path, monkeypatch):
    """建议要带上数据年龄：评分每 5 秒重算，模型必须知道是不是最新。"""
    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        stale = {**ADVICE, "ts": 1000}          # 很久以前的快照
        manager.advice_fn = lambda sym: stale
        manager.symbols_fn = lambda: ["btcusdt"]
        out = manager._read_advice({"symbol": "btcusdt"})
        assert out["available"] is True
        assert out["ageSeconds"] is not None
        assert out["ageSeconds"] > 0
        assert "每 5 秒重算" in out["note"]
        await manager.stop()
    asyncio.run(scenario())


def test_read_candles_distinguishes_not_ready_from_not_watched(tmp_path, monkeypatch):
    """K线的空结果同样要能区分两种情况。"""
    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        manager.bars_fn = lambda sym, iv="": []
        manager.symbols_fn = lambda: ["btcusdt"]

        watched = manager._read_candles({"symbol": "btcusdt"})
        assert watched["available"] is False and watched["monitored"] is True
        assert "还没攒够" in watched["reason"]

        other = manager._read_candles({"symbol": "dogeusdt"})
        assert other["available"] is False and other["monitored"] is False
        assert "不在当前监控列表" in other["reason"]
        await manager.stop()
    asyncio.run(scenario())


def test_symbol_normalization():
    """BTC / btc / BTCUSDT 都要归一到 btcusdt。

    这是"BTC 已不在监控范围"的直接原因：模型只写 BTC，系统 key 是 btcusdt，
    不归一则全部匹配失败。
    """
    from ai.agent import _symbol
    pool = ["btcusdt", "ethusdt"]
    for raw in ("BTC", "btc", "BTCUSDT", "btcusdt", "Btc"):
        assert _symbol(raw, pool) == "btcusdt", raw
    # 不在池子里的也要补全，便于给出准确的"不在监控"提示
    assert _symbol("DOGE", pool) == "dogeusdt"
    assert _symbol("dogeusdt", pool) == "dogeusdt"


def test_read_candles_accepts_short_symbol(tmp_path, monkeypatch):
    """模型传 'BTC' 时应该真的能读到 btcusdt 的数据。"""
    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        manager.symbols_fn = lambda: ["btcusdt"]

        class Candle:
            open_time = 1; open = 2.0; high = 3.0; low = 1.5; close = 2.5
            volume = 10.0; buy_vol = 6.0; sell_vol = 4.0

        manager.bars_fn = lambda sym, iv="": [Candle()] * 20
        out = manager._read_candles({"symbol": "BTC"})
        assert out["available"] is True
        assert out["symbol"] == "btcusdt"
        await manager.stop()
    asyncio.run(scenario())


def test_read_advice_accepts_short_symbol(tmp_path, monkeypatch):
    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        manager.symbols_fn = lambda: ["btcusdt"]
        manager.advice_fn = lambda sym: ADVICE if sym == "btcusdt" else {}
        out = manager._read_advice({"symbol": "BTC"})
        assert out["available"] is True
        assert out["advice"]["symbol"] == "btcusdt"
        await manager.stop()
    asyncio.run(scenario())
