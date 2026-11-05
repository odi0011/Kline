"""Agent 手动下单：必须两步（算单 → 确认 → 执行），且风控闸是硬的。

用户要求"人能做的它都能做"，但真实资金操作不能一句话就下单：
模型可能把做空理解成做多、把 50 倍理解成 50 USDT。所以按最稳的方式做——
先算出完整的单给用户看，确认后原样执行，中途不许重算。
"""
import asyncio
import dataclasses
from unittest.mock import MagicMock

import pytest

from decision.config import DecisionConfig
from quant import QuantEngine


@pytest.fixture(autouse=True)
def _isolate_exchange(monkeypatch):
    """每个用例都把交易所调用隔开，结束后自动还原。

    为什么要 autouse：分档查询/精度查询/下单全走 accounts.signed_request，
    不拦住就会真的打币安（实测一个用例从 0.1 秒涨到 20 秒，还可能拿到
    真实账户数据）。用 monkeypatch 而不是直接赋值，是为了不把假接口
    漏给后面的模块。
    """
    import accounts
    import quant as quant_mod
    monkeypatch.setattr(accounts, "signed_request", lambda *a, **k: [])
    monkeypatch.setattr(quant_mod, "signed_request", lambda *a, **k: {})
    accounts.clear_bracket_cache()
    yield
    accounts.clear_bracket_cache()


def _engine(tmp_path, price=100.0, available=1000.0):
    accts = {"list": [{"name": "acc", "api_key": "k", "api_secret": "s", "icon": ""}]}
    # 分档查询带 TTL 缓存；每个用例换一份 mock，缓存必须清掉
    import accounts
    accounts.clear_bracket_cache()
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


# ---------- 止损止盈由用户说了算 ----------
#
# 线上原话："止损价我改不了：系统固定按 0.4% 算" "把止损挂到强平线旁边这件事
# 我设不了"。系统的默认值（ATR×1.5，夹在 0.4%~2%）是**建议**，不是规则：
# 用户报一个价，就必须按那个价挂。这里锁住这条，防止以后又被"收窄"回来。


def test_user_stop_loss_price_is_honored(tmp_path):
    """用户给 3% 的宽止损：旧实现会夹到 2%，现在必须原样。"""
    eng = _engine(tmp_path)
    plan = eng.manual_order_plan("btcusdt", "long", 100.0, 10, stop_loss=97.0)
    assert plan["ready"] is True, plan
    assert plan["sl"] == 97.0
    assert abs(plan["slPct"] - 3.0) < 1e-6
    assert "止损按你给的" in plan["note"]


def test_user_stop_loss_near_liquidation_is_accepted(tmp_path):
    """止损贴着强平线：交易所收这种单，系统就不许拒，只提醒风险。"""
    eng = _engine(tmp_path)
    eng.max_leverage_fn = lambda s: 150
    # 150x 多单强平约在 100 × (1 - 0.95/150) ≈ 99.367
    near = eng.manual_order_plan("btcusdt", "long", 100.0, 150, stop_loss=99.4)
    assert near["ready"] is True, near
    assert near["sl"] == 99.4, "用户给的价不能被改成默认值"
    # 越过强平线也照挂，只是提醒"大概率先爆仓"
    beyond = eng.manual_order_plan("btcusdt", "long", 100.0, 150, stop_loss=99.2)
    assert beyond["ready"] is True, beyond
    assert beyond["sl"] == 99.2
    assert beyond["warning"] and "强平" in beyond["warning"]


def test_user_stop_loss_beyond_liquidation_is_still_accepted(tmp_path):
    """止损在强平价之外（先爆仓、止损永不触发）也是用户的自由，只提示。"""
    eng = _engine(tmp_path)
    eng.max_leverage_fn = lambda s: 150
    plan = eng.manual_order_plan("btcusdt", "long", 100.0, 150, stop_loss=95.0)
    assert plan["ready"] is True, plan
    assert plan["sl"] == 95.0
    assert plan.get("warning")


def test_user_stop_loss_percent_in_points_and_fraction(tmp_path):
    """stopLossPct：0.5 和 5 都要按百分数理解，不是 50% 也不是 500%。"""
    eng = _engine(tmp_path)
    a = eng.manual_order_plan("btcusdt", "long", 100.0, 10, stop_loss_pct=0.5)
    assert a["ready"] is True and abs(a["sl"] - 99.5) < 1e-9, a
    b = eng.manual_order_plan("btcusdt", "long", 100.0, 10, stop_loss_pct=5)
    assert b["ready"] is True and abs(b["sl"] - 95.0) < 1e-9, b


def test_user_take_profit_on_short_side(tmp_path):
    """空单用户给止盈：必须是现价下方的那个价。"""
    eng = _engine(tmp_path)
    plan = eng.manual_order_plan("btcusdt", "short", 100.0, 10, take_profit=92.0)
    assert plan["ready"] is True, plan
    assert plan["tp"] == 92.0 and plan["tp"] < plan["price"]


def test_wrong_side_stop_is_rejected_with_reason(tmp_path):
    """方向搞反（多单止损在现价上方）是物理错误，必须说清楚。"""
    eng = _engine(tmp_path)
    plan = eng.manual_order_plan("btcusdt", "long", 100.0, 10, stop_loss=101.0)
    assert plan["ready"] is False
    assert "下方" in plan["reason"]


def test_high_leverage_default_stop_stays_inside_liquidation(tmp_path):
    """用户没给止损时，默认值要收进强平线内——否则挂上去永远不触发。"""
    eng = _engine(tmp_path)
    eng.max_leverage_fn = lambda s: 150
    plan = eng.manual_order_plan("btcusdt", "long", 100.0, 150)
    assert plan["ready"] is True, plan
    assert plan["sl"] > plan["liquidation"], "默认止损落在强平之外等于没挂"
    assert "强平太近" in plan["note"]


def test_stop_at_liquidation_gets_a_concrete_price(tmp_path):
    """用户说"止损挂强平线旁边"：系统给出贴着强平的具体价，不许落到 0.4%。"""
    eng = _engine(tmp_path)
    eng.max_leverage_fn = lambda s: 150
    plan = eng.manual_order_plan("btcusdt", "long", 100.0, 150,
                                 stop_at_liquidation=True)
    assert plan["ready"] is True, plan
    liq = plan["liquidation"]
    # 贴在强平线内侧：距强平只有千分之二的价差，远小于默认的 0.4%
    assert plan["sl"] > liq
    assert abs(plan["sl"] - liq) / plan["price"] < 0.002
    assert plan["sl"] < plan["price"]
    assert "强平线旁边" in plan["note"]
    # 这种情况下不该再报"止损在强平之外"
    assert not plan["warning"]


def test_stop_at_liquidation_on_short_side(tmp_path):
    eng = _engine(tmp_path)
    eng.max_leverage_fn = lambda s: 50
    plan = eng.manual_order_plan("btcusdt", "short", 100.0, 50,
                                 stop_at_liquidation=True)
    assert plan["ready"] is True, plan
    # 空单：现价 < 止损 < 强平（止损在强平内侧，先于爆仓触发）
    assert plan["price"] < plan["sl"] < plan["liquidation"]


def test_tool_forwards_stop_at_liquidation(tmp_path, monkeypatch):
    """Agent 工具层要把 stopAtLiquidation 透传（放在第 9 个位置）。"""
    from tests.test_agent import make_manager

    seen = []

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        manager.order_plan_fn = lambda *a: seen.append(a) or {
            "ready": True, "symbol": a[0]}
        out = await manager._place_order({
            "symbol": "btcusdt", "side": "long", "stopAtLiquidation": True})
        assert out["ready"] is True
        assert seen[0][8] is True
        await manager.stop()

    asyncio.run(scenario())


def test_tool_forwards_user_stops_to_plan(tmp_path, monkeypatch):
    """Agent 工具层要把 stopLoss 等四个参数透传给算单函数。"""
    from tests.test_agent import make_manager

    seen = []

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)

        def plan_fn(*args):
            seen.append(args)
            return {"ready": True, "symbol": args[0]}
        manager.order_plan_fn = plan_fn
        out = await manager._place_order({
            "symbol": "btcusdt", "side": "long",
            "stopLoss": 97.5, "takeProfitPct": 5})
        assert out["ready"] is True
        # 位置：symbol, side, notional, leverage, sl, slPct, tp, tpPct
        assert seen[0][4] == 97.5 and seen[0][7] == 5.0 and seen[0][5] is None
        await manager.stop()

    asyncio.run(scenario())


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
    assert "最高 5x" in plan["note"]


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
    assert "最高 50x" in plan["note"]

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
    assert result["slPlaced"] is True and result["tpPlaced"] is False
    # **止损成功就不算裸奔**：止盈缺了只是少个目标位，仓位仍有止损保护。
    # 线上事故：这里以前是 unprotected=True + "这个仓现在是裸的"，
    # 模型信了，连着 5 轮 read_orders/read_accounts 想补救，撞上 8 轮上限，
    # 用户只看到"已达到 8 轮上限"，根本不知道自己那笔单其实已经成交了。
    assert result["unprotected"] is False
    assert "止盈" in result["protectError"]
    assert "止损已挂上" in result["warning"]
    assert "裸" not in result["warning"], "止损在，不能说裸奔"


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


# ---------- 线上事故：止盈失败被报成裸奔 ----------

def test_tp_only_failure_is_not_reported_as_naked(tmp_path):
    """止损成功、只有止盈失败 → unprotected 必须是 false。

    线上真实事故链条：
      1. 下单成功，止损挂上，止盈报 -1111 精度错误
      2. 旧代码 unprotected=True 并写"这个仓现在是裸的"
      3. 模型信了，连开 5 轮 read_orders/read_accounts/read_events 想补救
      4. 撞上 8 轮上限
      5. 用户看到「已达到 8 轮上限，任务未得到最终结论」
      → 用户根本不知道那笔单已经成交了，也不知道其实有止损保护。
    """
    eng = _engine(tmp_path)

    def fake(key, secret, sym, side, otype, qty, stop):
        if otype == "TAKE_PROFIT_MARKET":
            raise RuntimeError("Precision is over the maximum defined for this asset")
        return {"algoId": 7}

    import quant as quant_mod
    orig, orig_req = quant_mod.place_protective_order, quant_mod.signed_request
    quant_mod.place_protective_order = fake
    quant_mod.signed_request = MagicMock(return_value={})
    try:
        plan = eng.manual_order_plan("btcusdt", "long", 100.0, 10)
        result = eng.manual_order_execute(plan)
    finally:
        quant_mod.place_protective_order = orig
        quant_mod.signed_request = orig_req

    assert result["ok"] is True
    assert result["unprotected"] is False, "有止损就不算裸奔"
    assert result["done"] is True
    # 明确告诉模型别再反复核对——这是 8 轮死循环的根源
    assert "不要再调用" in result["next"]
    assert "止损已挂上" in result["warning"]


def test_both_fail_is_naked(tmp_path):
    """止损也失败才是真裸奔，warning 必须第一句就警告。"""
    eng = _engine(tmp_path)

    def fake(*_a, **_k):
        raise RuntimeError("all protection failed")

    import quant as quant_mod
    orig, orig_req = quant_mod.place_protective_order, quant_mod.signed_request
    quant_mod.place_protective_order = fake
    quant_mod.signed_request = MagicMock(return_value={})
    try:
        plan = eng.manual_order_plan("btcusdt", "long", 100.0, 10)
        result = eng.manual_order_execute(plan)
    finally:
        quant_mod.place_protective_order = orig
        quant_mod.signed_request = orig_req

    assert result["unprotected"] is True
    assert result["slPlaced"] is False
    assert "止损没挂上" in result["warning"] and "裸" in result["warning"]


def test_account_leverage_bracket_is_used(monkeypatch):
    """杠杆上限必须查账户自己的分档，不能用公开分档。

    线上实测：公开分档说 BTCUSDT 可以 150x，这个账户实际只有 125x。
    按 150 去设 → -4028 "Leverage 150 is not valid"，整张单下不出去。
    """
    import accounts

    accounts.clear_bracket_cache()
    monkeypatch.setattr(accounts, "signed_request", lambda *a, **k: [
        {"symbol": "BTCUSDT", "maxLeverage": 125,
         "brackets": [{"initialLeverage": 125, "notionalCap": 50000},
                      {"initialLeverage": 100, "notionalCap": 250000}]},
        {"symbol": "ETHUSDT", "maxLeverage": 75, "brackets": []},
    ])
    assert accounts.account_max_leverage("k", "s", "btcusdt") == 125
    assert accounts.account_max_leverage("k", "s", "BTCUSDT") == 125
    assert accounts.account_max_leverage("k", "s", "ethusdt") == 75
    assert accounts.account_max_leverage("k", "s", "solusdt") == 0


def test_plan_takes_min_of_account_and_public_leverage(tmp_path):
    """账户分档和公开分档都给时取更小的那个（宁可少开也不被拒）。"""
    eng = _engine(tmp_path)
    eng.max_leverage_fn = lambda s: 150        # 公开分档说 150
    import accounts, quant as quant_mod
    orig = accounts.signed_request
    accounts.signed_request = lambda *a, **k: [
        {"symbol": "BTCUSDT", "maxLeverage": 125, "brackets": []}]
    try:
        plan = quant_mod.QuantEngine.manual_order_plan.__wrapped__ if False else eng.manual_order_plan(
            "btcusdt", "long", 100.0, 150)
    finally:
        accounts.signed_request = orig
    assert plan["ready"] is True
    assert plan["leverage"] == 125, plan


# ---------- 浮点精度：止损单靠运气挂上的根因 ----------

def test_format_price_avoids_float_noise():
    """f"{x:.12f}" 会把 77998.3 写成 77998.300000000003，交易所 -1111。

    线上实测（同一段代码、同一个币、同样是 0.001 张）：
      78784.5 → "78784.5"                  → OK
      77998.3 → "77998.300000000003"        → Precision 超限
    也就是说止损单能不能挂上，取决于价格的小数位碰巧干不干净。
    """
    import accounts
    assert accounts.format_price(77998.3, "btcusdt") == "77998.3"
    assert accounts.format_price(78784.5, "btcusdt") == "78784.5"
    assert accounts.format_price(78100.0 * 0.996, "btcusdt") == "77787.6"
    # 原始写法确实会产出脏字符串（证明这条测试不是空转）
    assert f"{77998.3:.12f}".rstrip("0").rstrip(".") == "77998.300000000003"


def test_format_qty_respects_step_size(monkeypatch):
    import accounts
    # 离线测试不能依赖网络：显式给交易所约束
    monkeypatch.setattr(accounts, "symbol_filters",
                        lambda *a, **k: {"stepSize": "0.00010",
                                         "quantityPrecision": 4})
    assert accounts.format_qty(0.001, "btcusdt") == "0.001"
    assert accounts.format_qty(0.0127, "btcusdt") == "0.0127"
    # 向下取整到步长，不能向上（会超出可用数量）
    assert accounts.format_qty(0.00129, "btcusdt") == "0.0012"


def test_format_qty_falls_back_to_quantity_precision(monkeypatch):
    """拿不到 stepSize 时用 quantityPrecision，绝不要原样传 float。"""
    import accounts
    monkeypatch.setattr(accounts, "symbol_filters",
                        lambda *a, **k: {"quantityPrecision": 3})
    assert accounts.format_qty(0.00129, "btcusdt") == "0.001"


def test_protective_order_sends_formatted_values(monkeypatch):
    """place_protective_order 必须用格式化后的字符串，不能走 f-string。"""
    import accounts
    sent = {}
    monkeypatch.setattr(accounts, "signed_request",
                        lambda *a, **k: sent.update(k.get("params") or a[3] or {}) or {})
    monkeypatch.setattr(accounts, "symbol_filters",
                        lambda *a, **k: {"tickSize": "0.10", "stepSize": "0.0010"})
    accounts.place_protective_order("k", "s", "BTCUSDT", "SELL", "STOP_MARKET",
                                    0.001, 77998.3)
    assert sent["quantity"] == "0.001"
    assert sent["triggerPrice"] == "77998.3"
    assert "000000000" not in sent["triggerPrice"]


def test_decimals_of_step():
    import accounts
    assert accounts._decimals_of("0.00010") == 4
    assert accounts._decimals_of("0.10") == 1
    assert accounts._decimals_of("1") == 0
    assert accounts._decimals_of(None) == 0


# ---------- 改已有仓位 / 平已有仓位 ----------
#
# 线上原话："止损价我改不了：系统固定按 0.4% 算"。
# 根因不是"策略限制"，是根本没有这个工具：place_order 只能开新仓，
# 改不了已经挂着的条件单。下面锁住这两个新能力。

def test_position_adjust_moves_stop_loss(tmp_path, monkeypatch):
    """用户说"止损拉到 X"：撤旧 STOP_MARKET → 挂新价，且不动止盈。"""
    import accounts
    import quant as quant_mod

    eng = _engine(tmp_path)
    monkeypatch.setattr(quant_mod, "fetch_positions", lambda *a, **k: [{
        "symbol": "btcusdt", "amt": 0.5, "entry": 100.0, "mark": 100.0,
        "leverage": 10, "liquidation": 90.5}])
    calls = []
    monkeypatch.setattr(accounts, "cancel_algo_orders",
                        lambda *a, **k: calls.append(("cancel", a[2], k.get("types"))) or 1)
    monkeypatch.setattr(quant_mod, "cancel_algo_orders",
                        lambda *a, **k: calls.append(("cancel", a[2], k.get("types"))) or 1)
    placed = []
    monkeypatch.setattr(quant_mod, "place_protective_order",
                        lambda *a, **k: placed.append(a) or {"algoId": 7})

    out = eng.position_adjust("BTC", stop_loss=96.0)
    assert out["ok"] is True, out
    assert out["stopLoss"] == 96.0
    # 只撤止损类条件单，止盈不能被顺手撤掉
    assert calls == [("cancel", "btcusdt", ("STOP_MARKET",))]
    assert len(placed) == 1 and placed[0][4] == "STOP_MARKET"
    assert placed[0][-1] == 96.0


def test_position_adjust_stop_beyond_liquidation_is_honored(tmp_path, monkeypatch):
    """改到强平线外面也照挂，只给 warning——不拒绝、不改价。"""
    import accounts
    import quant as quant_mod

    eng = _engine(tmp_path)
    monkeypatch.setattr(quant_mod, "fetch_positions", lambda *a, **k: [{
        "symbol": "btcusdt", "amt": 0.5, "entry": 100.0, "mark": 100.0,
        "leverage": 100, "liquidation": 99.05}])
    monkeypatch.setattr(accounts, "cancel_algo_orders", lambda *a, **k: 1)
    monkeypatch.setattr(quant_mod, "cancel_algo_orders", lambda *a, **k: 1)
    monkeypatch.setattr(quant_mod, "place_protective_order",
                        lambda *a, **k: {"algoId": 8})
    out = eng.position_adjust("btcusdt", stop_loss=98.0)
    assert out["ok"] is True, out
    assert out["stopLoss"] == 98.0
    assert out["warning"] and "强平" in out["warning"]


def test_position_adjust_can_clear_stop_only(tmp_path, monkeypatch):
    """用户要把仓裸着手动管理：撤止损，不动止盈。"""
    import accounts
    import quant as quant_mod

    eng = _engine(tmp_path)
    monkeypatch.setattr(quant_mod, "fetch_positions", lambda *a, **k: [{
        "symbol": "btcusdt", "amt": -0.5, "entry": 100.0, "mark": 100.0,
        "leverage": 10, "liquidation": 109.0}])
    cancelled = []
    monkeypatch.setattr(quant_mod, "cancel_algo_orders",
                        lambda *a, **k: cancelled.append(k.get("types")) or 1)
    placed = []
    monkeypatch.setattr(quant_mod, "place_protective_order",
                        lambda *a, **k: placed.append(a) or {"algoId": 9})
    out = eng.position_adjust("btcusdt", clear_stop=True)
    assert out["ok"] is True, out
    assert cancelled == [("STOP_MARKET",)]
    assert placed == [] and out["naked"] is True


def test_position_adjust_rejects_when_no_position(tmp_path, monkeypatch):
    import quant as quant_mod

    eng = _engine(tmp_path)
    monkeypatch.setattr(quant_mod, "fetch_positions", lambda *a, **k: [])
    out = eng.position_adjust("btcusdt", stop_loss=96.0)
    assert out["ok"] is False and "没有" in out["error"]


def test_position_close_full_cancels_protection(tmp_path, monkeypatch):
    """全平：市价 reduceOnly 平掉，并撤掉条件单（否则留孤儿单）。"""
    import quant as quant_mod

    eng = _engine(tmp_path)
    monkeypatch.setattr(quant_mod, "fetch_positions", lambda *a, **k: [{
        "symbol": "btcusdt", "amt": 0.5, "entry": 100.0, "mark": 101.0,
        "leverage": 10, "liquidation": 90.5}])
    eng._place_order = MagicMock(return_value={"avgPrice": "101.0"})
    eng._broadcast_positions = MagicMock()
    cancelled = []
    monkeypatch.setattr(quant_mod, "cancel_algo_orders",
                        lambda *a, **k: cancelled.append(a[2]) or 2)
    out = eng.position_close("btcusdt")
    assert out["ok"] is True, out
    assert out["qty"] == 0.5 and out["full"] is True
    assert out["fill"] == 101.0 and out["cancelledProtection"] == 2
    assert cancelled == ["btcusdt"]
    assert eng._place_order.call_args.kwargs.get("reduce_only") is True


def test_position_close_partial_keeps_protection(tmp_path, monkeypatch):
    """部分平仓不能撤条件单——剩下的仓位还需要保护。"""
    import quant as quant_mod

    eng = _engine(tmp_path)
    monkeypatch.setattr(quant_mod, "fetch_positions", lambda *a, **k: [{
        "symbol": "btcusdt", "amt": 1.0, "entry": 100.0, "mark": 100.0,
        "leverage": 10, "liquidation": 90.5}])
    eng._place_order = MagicMock(return_value={"avgPrice": "100.0"})
    eng._broadcast_positions = MagicMock()
    cancelled = []
    monkeypatch.setattr(quant_mod, "cancel_algo_orders",
                        lambda *a, **k: cancelled.append(a) or 2)
    out = eng.position_close("btcusdt", qty=0.4)
    assert out["ok"] is True, out
    assert out["qty"] == 0.4 and out["full"] is False
    assert abs(out["remaining"] - 0.6) < 1e-9
    assert cancelled == []


def test_adjust_tool_is_exposed_and_forwards(tmp_path, monkeypatch):
    """Agent 工具层：adjust_position 必须接到 position_adjust 上。"""
    from tests.test_agent import make_manager

    seen = []

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        manager.position_adjust_fn = lambda *a: seen.append(a) or {"ok": True}
        out = await manager._adjust_position({
            "symbol": "btcusdt", "stopLoss": 96.5, "clearTakeProfit": True})
        assert out["ok"] is True
        assert seen[0][1] == 96.5 and seen[0][7] is True
        await manager.stop()

    asyncio.run(scenario())


def test_close_tool_is_exposed_and_forwards(tmp_path, monkeypatch):
    from tests.test_agent import make_manager

    seen = []

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        manager.position_close_fn = lambda *a: seen.append(a) or {"ok": True}
        out = await manager._close_position({"symbol": "btcusdt", "qty": 0.2})
        assert out["ok"] is True
        assert seen[0] == ("btcusdt", 0.2)
        await manager.stop()

    asyncio.run(scenario())


def test_cancel_algo_orders_matches_order_type_field(monkeypatch):
    """交易所返回的字段叫 orderType，不是 type。

    线上真实事故：按 `type` 匹配 → 一条都没匹配上 → 撤 0 条 → 改止损后
    旧止损（2624.5）和新止损（2450）两张同时挂着，价格先碰哪张就按哪张平。
    """
    import accounts

    rows = [
        {"algoId": 1, "orderType": "STOP_MARKET", "triggerPrice": "2450.0"},
        {"algoId": 2, "orderType": "TAKE_PROFIT_MARKET", "triggerPrice": "2402.3"},
        {"algoId": 3, "orderType": "STOP_MARKET", "triggerPrice": "2624.5"},
    ]
    deleted = []

    def fake(api_key, api_secret, path, params=None, method="GET"):
        if path.endswith("/openAlgoOrders"):
            return {"orders": rows}
        if path.endswith("/algoOrder") and method == "DELETE":
            deleted.append(params["algoId"])
            return {}
        raise AssertionError("unexpected call " + path)

    monkeypatch.setattr(accounts, "signed_request", fake)
    removed = accounts.cancel_algo_orders("k", "s", "ethusdt",
                                          types=("STOP_MARKET",))
    assert removed == 2
    assert sorted(deleted) == [1, 3], "只撤止损，止盈那条必须留着"


def test_adjust_position_sweeps_duplicate_stops(tmp_path, monkeypatch):
    """撤单接口没撤干净时，收尾必须把旧止损扫掉，只留新挂的那张。"""
    import accounts
    import quant as quant_mod

    eng = _engine(tmp_path)
    monkeypatch.setattr(quant_mod, "fetch_positions", lambda *a, **k: [{
        "symbol": "ethusdt", "amt": -0.01, "entry": 2418.0, "mark": 2418.0,
        "leverage": 10, "liquidation": 2647.4}])
    # 撤单"失败"：换以前就是两张止损并存
    monkeypatch.setattr(quant_mod, "cancel_algo_orders", lambda *a, **k: 0)
    monkeypatch.setattr(quant_mod, "place_protective_order",
                        lambda *a, **k: {"algoId": 999})
    live = [
        {"algoId": 999, "orderType": "STOP_MARKET", "triggerPrice": "2450.0"},
        {"algoId": 111, "orderType": "STOP_MARKET", "triggerPrice": "2624.5"},
        {"algoId": 222, "orderType": "TAKE_PROFIT_MARKET", "triggerPrice": "2402.3"},
    ]
    monkeypatch.setattr(quant_mod, "algo_orders", lambda *a, **k: live)
    deleted = []
    monkeypatch.setattr(quant_mod, "signed_request",
                        lambda k, s, path, params=None, method="GET":
                        deleted.append(params.get("algoId")) or {})
    out = eng.position_adjust("ethusdt", stop_loss=2450.0)
    assert out["ok"] is True, out
    assert deleted == [111], "旧止损必须被扫掉，止盈不能动"
    assert out["sweptStale"] == 1


# ---------- 精度表必须来自下单那个主机 ----------
#
# 线上真实事故（东京部署）：symbol_filters 只问 fapi.binance.com，
# 该主机从东京返回 451 → tickSize 缺失 → 价格按 8 位小数格式化 →
# ETH 止损 2624.825 被 -1111 拒掉 → 审计判定"保护单缺失" → 把用户的仓平掉。

def test_symbol_filters_ask_order_host_first(monkeypatch):
    """exchangeInfo 必须先问 accounts.BASE（下单的主机）。"""
    import accounts

    asked = []

    class FakeResp:
        def __init__(self, payload):
            self.payload = payload

        def read(self):
            return self.payload.encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    payload = ('{"symbols":[{"symbol":"ETHUSDT","quantityPrecision":3,'
               '"pricePrecision":2,"filters":[{"filterType":"PRICE_FILTER",'
               '"tickSize":"0.01"},{"filterType":"LOT_SIZE",'
               '"stepSize":"0.001"}]}]}')

    class FakeOpener:
        def open(self, req, timeout=0):
            # 只记录 base 主机那一次；退回公共主机的分支不该被走到
            asked.append(req.full_url)
            return FakeResp(payload)

    monkeypatch.setattr(accounts.urllib.request, "build_opener",
                        lambda *a, **k: FakeOpener())
    monkeypatch.setattr(accounts, "_FILTER_CACHE", {})
    monkeypatch.setattr(accounts, "_FILTER_CACHE_TS", {"ts": 0})
    filt = accounts.symbol_filters("ethusdt")
    assert asked and asked[0].startswith(accounts.BASE), asked
    assert filt["tickSize"] == "0.01"
    assert accounts.format_price(2624.8249999999994, "ethusdt") == "2624.82"


def test_manual_order_is_registered_as_exempt(tmp_path, monkeypatch):
    """手动开的仓必须登记成引擎豁免，否则审计会用默认止损覆盖/平掉。"""
    eng = _engine(tmp_path)
    monkeypatch.setattr(eng, "_place_protection", lambda *a, **k: (11, 22))
    plan = eng.manual_order_plan("btcusdt", "long", 100.0, 10)
    assert plan["ready"] is True, plan
    eng.manual_order_execute(plan)
    assert ("acc", "btcusdt") in eng._manual, "手动仓没登记豁免"


def test_audit_protection_sees_algo_orders(tmp_path, monkeypatch):
    """审计必须查算法条件单：止损不在 openOrders 里。

    以前只查 openOrders → 永远看不到已挂的止损 → 每轮判定缺失 → 补挂 →
    精度失败 → 保命平仓。用户的手动单被系统自己平掉就是这么来的。
    """
    eng = _engine(tmp_path)
    eng.decision_cfg = dataclasses.replace(eng.decision_cfg,
                                           exchange_protection=True)
    monkeypatch.setattr(__import__("quant"), "open_orders", lambda *a, **k: [])
    monkeypatch.setattr(__import__("quant"), "algo_orders",
                        lambda *a, **k: [{"algoId": 1, "orderType": "STOP_MARKET",
                                          "triggerPrice": "95.0"}])
    placed = []
    monkeypatch.setattr(eng, "_place_protection", lambda *a, **k: placed.append(a) or (1, 2))
    pos = {"symbol": "btcusdt", "side": "SELL", "qty": 1.0, "entry": 100.0,
           "sl": 95.0, "tp": 105.0, "atrAtEntry": 1.0}
    a = {"api_key": "k", "api_secret": "s", "name": "acc"}
    assert eng._audit_protection(a, pos) is True
    assert placed == [], "止损已经挂着，不该重复补挂"
    assert pos["protectionOk"] is True
