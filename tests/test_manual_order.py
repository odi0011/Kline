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


def test_plan_rejects_only_when_margin_insufficient(tmp_path):
    """不设人工上限：只要账户真有钱就允许，超出可用保证金才拒绝。"""
    eng = _engine(tmp_path, available=100000)
    # 10x、可用 10 万 → 最多 100 万名义
    plan = eng.manual_order_plan("btcusdt", "long", 999999, 10)
    assert plan["ready"] is True, plan
    assert plan["notional"] > 900000, "大单不该被人为上限挡掉"
    # 再大就真的超出账户能力了
    over = eng.manual_order_plan("btcusdt", "long", 2000000, 10)
    assert over["ready"] is False
    assert "超出账户能支撑" in over["reason"]
    assert over["limits"]["maxNotional"] > 0


def test_plan_allows_full_margin(tmp_path):
    """满仓也能下：不做百分比打折。"""
    eng = _engine(tmp_path, available=100.0)
    # 100 USDT 保证金 × 10x = 1000 名义，正好用满
    plan = eng.manual_order_plan("btcusdt", "long", 1000, 10)
    assert plan["ready"] is True, plan
    assert plan["margin"] >= 99


def test_plan_clamps_leverage_to_exchange_max(tmp_path):
    """杠杆只受交易所上限约束——不是我们设的顶。"""
    eng = _engine(tmp_path)
    eng.max_leverage_fn = lambda s: 5
    plan = eng.manual_order_plan("btcusdt", "long", 100.0, 50)
    assert plan["ready"] is True
    assert plan["leverage"] == 5
    assert "交易所最高 5x" in plan["note"]


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


# ---------- 币种归一 & 取价兜底 ----------

def test_symbol_normalisation(tmp_path):
    """模型会写 BTC / btc / BTC-USDT / 币安BTC，都要归一到 btcusdt。

    线上实测：模型传 "BTC" → 内部键是 "btcusdt" → price_fn 查不到 →
    "取不到 BTC 最新价，连单都算不出来"。
    """
    eng = _engine(tmp_path)
    for raw in ("BTC", "btc", "BTC-USDT", "BTCUSDT", "btc_usdt", "币安BTC"):
        assert eng._normalize_symbol(raw) == "btcusdt", raw
    assert eng._normalize_symbol("") == ""
    assert eng._normalize_symbol("   ") == ""


def test_price_falls_back_to_ticker(tmp_path):
    """本地行情没有这个币时，要退回交易所 ticker，而不是直接说取不到价。"""
    eng = _engine(tmp_path)
    eng.price_fn = lambda s: None
    eng._mark_price = MagicMock(return_value=None)
    import quant as quant_mod
    orig = quant_mod.signed_request
    quant_mod.signed_request = MagicMock(return_value={"lastPrice": "123.45"})
    try:
        price, src = eng._manual_price({"api_key": "k", "api_secret": "s"}, "ethusdt")
    finally:
        quant_mod.signed_request = orig
    assert price == 123.45
    assert src == "ticker"


def test_plan_reports_real_limits_when_over(tmp_path):
    """超限时要返回真实边界（可用保证金 × 杠杆），模型才能给出可用建议。"""
    eng = _engine(tmp_path, available=7356.55)
    plan = eng.manual_order_plan("btcusdt", "long", 5000000, 10)
    assert plan["ready"] is False
    lim = plan["limits"]
    assert lim["available"] == 7356.55
    # 上限就是可用保证金 × 杠杆，不含任何人为比例
    assert abs(lim["maxNotional"] - 7356.55 * 10) < 1
    assert "最多可下" in plan["suggest"]


def test_high_leverage_allowed_up_to_exchange_max(tmp_path):
    """150x 只被交易所上限拦，不被我们自己砍。

    测试里 max_leverage_fn 返回 50，所以按 50x 算并说明；
    如果交易所支持 150x，就应该原样 150x。
    """
    eng = _engine(tmp_path)
    plan = eng.manual_order_plan("btcusdt", "long", 5000, 150)
    assert plan["ready"] is True, plan
    assert plan["leverage"] == 50
    assert "交易所最高 50x" in plan["note"]

    # 交易所支持 150x 时不该被砍到 125x
    eng.max_leverage_fn = lambda s: 150
    plan2 = eng.manual_order_plan("btcusdt", "long", 5000, 150)
    assert plan2["leverage"] == 150, plan2


def test_trade_short_with_chinese_symbol(tmp_path):
    """用户说"BTC开空 5000U 150倍"时，算单必须成功且方向是 SELL。"""
    eng = _engine(tmp_path)
    plan = eng.manual_order_plan("BTC", "short", 5000, 150)
    assert plan["ready"] is True, plan
    assert plan["symbol"] == "btcusdt"
    assert plan["orderSide"] == "SELL"
    assert plan["tp"] < plan["price"] < plan["sl"], "做空：止盈在下、止损在上"


def test_protective_order_uses_algo_endpoint(tmp_path, monkeypatch):
    """止损必须走 /fapi/v1/algoOrder。

    线上实测：用老接口 /fapi/v1/order + STOP_MARKET 会被拒
    （-4120 "Order type not supported for this endpoint, please use the
    Algo Order API endpoints instead"），下单成功但止损全失败，仓位裸奔。
    """
    from accounts import place_protective_order
    import accounts as acc_mod
    seen = {}

    def fake(key, secret, path, params=None, method="GET"):
        seen["path"] = path
        seen["params"] = params
        return {"algoId": 123}

    orig = acc_mod.signed_request
    acc_mod.signed_request = fake
    try:
        out = place_protective_order("k", "s", "btcusdt", "BUY", "STOP_MARKET",
                                     0.001, 78000.0)
    finally:
        acc_mod.signed_request = orig
    assert out["algoId"] == 123
    assert seen["path"] == "/fapi/v1/algoOrder"
    p = seen["params"]
    assert p["algoType"] == "CONDITIONAL"
    assert p["triggerPrice"] == "78000"     # 新接口叫 triggerPrice，不是 stopPrice
    assert p["reduceOnly"] == "true"
    assert p["workingType"] == "MARK_PRICE"


def test_sl_and_tp_fail_independently(tmp_path):
    """止盈挂了但止损成功，也要如实报错（不能一个失败连累另一个）。"""
    eng = _engine(tmp_path)
    calls = []

    def fake(key, secret, sym, side, otype, qty, stop):
        calls.append(otype)
        if otype == "TAKE_PROFIT_MARKET":
            raise RuntimeError("tp boom")
        return {"algoId": 1}

    import quant as quant_mod
    orig = quant_mod.place_protective_order
    orig_req = quant_mod.signed_request
    quant_mod.place_protective_order = fake
    quant_mod.signed_request = MagicMock(return_value={})
    try:
        plan = eng.manual_order_plan("btcusdt", "long", 100.0, 10)
        result = eng.manual_order_execute(plan)
    finally:
        quant_mod.place_protective_order = orig
        quant_mod.signed_request = orig_req
    assert "STOP_MARKET" in calls and "TAKE_PROFIT_MARKET" in calls
    assert result["slOrderId"] == 1, "止损成功就该记下 id"
    assert result["tpOrderId"] is None
    assert result["unprotected"] is True
    assert "止盈" in result["protectError"]


def test_unprotected_warning_is_explicit(tmp_path):
    """止损完全没挂上时，要返回醒目的 warning 字段供 Agent 转述。"""
    eng = _engine(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("-4120 not supported")

    import quant as quant_mod
    orig = quant_mod.place_protective_order
    orig_req = quant_mod.signed_request
    quant_mod.place_protective_order = boom
    quant_mod.signed_request = MagicMock(return_value={})
    try:
        plan = eng.manual_order_plan("btcusdt", "long", 100, 10)
        result = eng.manual_order_execute(plan)
    finally:
        quant_mod.place_protective_order = orig
        quant_mod.signed_request = orig_req
    assert result["unprotected"] is True
    assert "裸" in result["warning"]
    assert "手动补止损" in result["warning"]


# ---------- 孤儿算法单清理 ----------

def test_cancel_algo_orders_deletes_all(monkeypatch):
    """平仓后要撤掉 algo 条件单。

    只调 cancel_all_open_orders 撤不到它们——止损走的是 /fapi/v1/algoOrder，
    仓位平了单还挂着就会累积成孤儿（线上实测留了两张，一张 0.623 BTC）。
    """
    import accounts as acc_mod
    calls = []

    def fake(key, secret, path, params=None, method="GET"):
        calls.append((method, path, dict(params or {})))
        if path == "/fapi/v1/openAlgoOrders":
            return {"orders": [{"algoId": 11}, {"algoId": 22}]}
        return {"code": "200"}

    monkeypatch.setattr(acc_mod, "signed_request", fake)
    removed = acc_mod.cancel_algo_orders("k", "s", "btcusdt")
    assert removed == 2
    deletes = [c for c in calls if c[0] == "DELETE"]
    assert len(deletes) == 2
    assert all(c[1] == "/fapi/v1/algoOrder" for c in deletes)
    assert {c[2]["algoId"] for c in deletes} == {11, 22}


def test_cancel_algo_orders_survives_single_failure(monkeypatch):
    """单张撤失败不中断，其余继续撤，返回真实数量。"""
    import accounts as acc_mod

    def fake(key, secret, path, params=None, method="GET"):
        if path == "/fapi/v1/openAlgoOrders":
            return {"orders": [{"algoId": 11}, {"algoId": 22}]}
        if params and params.get("algoId") == 11:
            raise RuntimeError("boom")
        return {"code": "200"}

    monkeypatch.setattr(acc_mod, "signed_request", fake)
    assert acc_mod.cancel_algo_orders("k", "s", "btcusdt") == 1


def test_cancel_protection_also_clears_algo(tmp_path, monkeypatch):
    """引擎平仓路径必须同时撤普通单和算法单。"""
    eng = _engine(tmp_path)
    import quant as quant_mod
    hit = []
    monkeypatch.setattr(quant_mod, "cancel_all_open_orders",
                        lambda k, s, sym: hit.append(("normal", sym)))
    monkeypatch.setattr(quant_mod, "cancel_algo_orders",
                        lambda k, s, sym: hit.append(("algo", sym)))
    eng._cancel_protection({"api_key": "k", "api_secret": "s"}, "btcusdt")
    assert ("normal", "btcusdt") in hit
    assert ("algo", "btcusdt") in hit, "算法单没撤，会留孤儿"


def test_cancel_protection_runs_even_when_exchange_protection_off(tmp_path, monkeypatch):
    """开关只控制"引擎要不要挂保护单"，撤单永远要做。"""
    eng = _engine(tmp_path)
    import dataclasses
    eng.decision_cfg = dataclasses.replace(eng.decision_cfg, exchange_protection=False)
    import quant as quant_mod
    hit = []
    monkeypatch.setattr(quant_mod, "cancel_all_open_orders", lambda k, s, sym: hit.append("normal"))
    monkeypatch.setattr(quant_mod, "cancel_algo_orders", lambda k, s, sym: hit.append("algo"))
    eng._cancel_protection({"api_key": "k", "api_secret": "s"}, "btcusdt")
    assert hit == ["normal", "algo"]
