"""Agent 手动下单：必须两步（算单 → 确认 → 执行），且风控闸是硬的。

用户要求"人能做的它都能做"，但真实资金操作不能一句话就下单：
模型可能把做空理解成做多、把 50 倍理解成 50 USDT。所以按最稳的方式做——
先算出完整的单给用户看，确认后原样执行，中途不许重算。
"""
import asyncio
from unittest.mock import MagicMock

import pytest

from decision.config import DecisionConfig
from quant import QuantEngine


def _engine(tmp_path, price=100.0, available=1000.0):
    accts = {"list": [{"name": "acc", "api_key": "k", "api_secret": "s", "icon": ""}]}
    eng = QuantEngine(
        accts, str(tmp_path / "quant.yaml"), price_fn=lambda s: price,
        min_pattern_winrate=0.35, min_pattern_samples=5,
        decision_config=DecisionConfig(scoring_enabled=False, watch_enabled=False,
                                       exchange_protection=False),
    )
    eng._step_size = MagicMock(return_value=0.001)
    eng._available = MagicMock(return_value=available)
    eng._balance_snapshot = MagicMock(return_value=(available, available))
    eng._place_order = MagicMock(return_value={"orderId": 1, "avgPrice": str(price)})
    eng._broadcast_positions = MagicMock()
    eng.max_leverage_fn = lambda s: 50
    eng.indicators["btcusdt"] = {"price": price, "atr": price * 0.01}
    # 设置杠杆会打真实交易所接口，测试里一律拦掉
    import quant as quant_mod
    quant_mod.signed_request = MagicMock(return_value={})
    return eng


def test_plan_long_full_fields(tmp_path):
    eng = _engine(tmp_path)
    plan = eng.manual_order_plan("btcusdt", "long", 100.0, 10)
    assert plan["ready"] is True, plan
    assert plan["orderSide"] == "BUY" and plan["closeSide"] == "SELL"
    assert plan["direction"] == "long"
    for key in ("qty", "notional", "leverage", "margin", "sl", "tp", "slPct", "tpPct"):
        assert plan.get(key) is not None, key
    # 止损必须在入场价下方、止盈在上方
    assert plan["sl"] < plan["price"] < plan["tp"]


def test_plan_short_direction_is_reversed(tmp_path):
    """做空：下单方向 SELL、平仓方向 BUY、止损在上方。"""
    eng = _engine(tmp_path)
    plan = eng.manual_order_plan("btcusdt", "short", 100.0, 10)
    assert plan["ready"] is True, plan
    assert plan["orderSide"] == "SELL" and plan["closeSide"] == "BUY"
    assert plan["tp"] < plan["price"] < plan["sl"]


def test_plan_rejects_over_notional_cap(tmp_path):
    """超过单笔名义上限要拒绝，不能说"帮你下了"。"""
    eng = _engine(tmp_path, available=100000)
    plan = eng.manual_order_plan("btcusdt", "long", 999999, 10)
    assert plan["ready"] is False
    assert "超过上限" in plan["reason"]


def test_plan_rejects_when_margin_insufficient(tmp_path):
    """可用保证金太小 → 按比例算出的上限低于请求金额，拒绝。"""
    eng = _engine(tmp_path, available=100.0)      # 25% = 25 USDT 保证金
    plan = eng.manual_order_plan("btcusdt", "long", 1000, 10)
    assert plan["ready"] is False
    assert "超过上限" in plan["reason"]


def test_plan_clamps_leverage_to_exchange_max(tmp_path):
    eng = _engine(tmp_path)
    eng.max_leverage_fn = lambda s: 5
    plan = eng.manual_order_plan("btcusdt", "long", 100.0, 50)
    assert plan["ready"] is True
    assert plan["leverage"] == 5
    assert "上限" in plan["note"]


def test_plan_rejects_bad_direction(tmp_path):
    eng = _engine(tmp_path)
    assert eng.manual_order_plan("btcusdt", "sideways", 100, 10)["ready"] is False


def test_plan_rejects_unknown_price(tmp_path):
    eng = _engine(tmp_path)
    eng.price_fn = lambda s: None
    eng._mark_price = MagicMock(return_value=None)
    plan = eng.manual_order_plan("nosuchcoin", "long", 100, 10)
    assert plan["ready"] is False
    assert "最新价" in plan["reason"]


# ---------- 两步执行 ----------

def test_tool_requires_plan_to_execute(tmp_path, monkeypatch):
    """没有 plan 就执行 → 必须拒绝，不能自己重新算一遍下单。"""
    from tests.test_agent import make_manager

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        manager.order_exec_fn = lambda plan: {"ok": True}
        out = await manager._place_order({"symbol": "btcusdt", "side": "long",
                                          "dryRun": False})
        assert out["ok"] is False
        assert "plan" in out["error"].lower()
        await manager.stop()

    asyncio.run(scenario())


def test_tool_dry_run_returns_plan_without_executing(tmp_path, monkeypatch):
    from tests.test_agent import make_manager

    executed = []

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        manager.order_plan_fn = lambda sym, side, n, lev: {
            "ready": True, "symbol": sym, "side": side, "qty": 1.0}
        manager.order_exec_fn = lambda plan: executed.append(plan) or {"ok": True}
        out = await manager._place_order({"symbol": "btcusdt", "side": "long"})
        assert out["ready"] is True
        assert "复述给用户" in out["next"]
        assert executed == [], "dryRun 绝不能真的下单"
        await manager.stop()

    asyncio.run(scenario())


def test_tool_executes_only_with_explicit_plan(tmp_path, monkeypatch):
    from tests.test_agent import make_manager

    got = []

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        manager.order_plan_fn = lambda *a: {"ready": True, "symbol": "btcusdt",
                                            "qty": 2.0, "leverage": 10}
        manager.order_exec_fn = lambda plan: got.append(plan) or {"ok": True, "fill": 100}
        plan = await manager._place_order({"symbol": "btcusdt", "side": "long"})
        out = await manager._place_order({"symbol": "btcusdt", "side": "long",
                                          "dryRun": False, "plan": plan})
        assert out["ok"] is True
        assert got and got[0]["qty"] == 2.0, "必须原样传回 plan，不能重算"
        await manager.stop()

    asyncio.run(scenario())


def test_execute_places_order_and_protective_orders(tmp_path):
    """执行时：真下单 + 立刻挂止损止盈（交易所侧，进程挂了也有效）。"""
    eng = _engine(tmp_path)
    calls = []

    def fake_protective(key, secret, sym, side, otype, qty, stop):
        calls.append((otype, side, qty, stop))
        return {"orderId": len(calls)}

    import quant as quant_mod
    orig = quant_mod.place_protective_order
    orig_req = quant_mod.signed_request
    quant_mod.place_protective_order = fake_protective
    quant_mod.signed_request = MagicMock(return_value={})
    try:
        plan = eng.manual_order_plan("btcusdt", "long", 100.0, 10)
        result = eng.manual_order_execute(plan)
    finally:
        quant_mod.place_protective_order = orig
        quant_mod.signed_request = orig_req
    assert result["ok"] is True, result
    types = [c[0] for c in calls]
    assert "STOP_MARKET" in types and "TAKE_PROFIT_MARKET" in types
    # 止损止盈都是平仓方向（多头 → SELL）
    assert all(c[1] == "SELL" for c in calls)


def test_execute_rejects_invalid_plan(tmp_path):
    eng = _engine(tmp_path)
    assert eng.manual_order_execute({})["ok"] is False
    assert eng.manual_order_execute({"ready": False})["ok"] is False
