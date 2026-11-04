"""Agent 的数据覆盖：页面上有的数据，它必须都能读到。

背景（线上实测）：
 · 问资金费率/多空比/OI → 答"没有这个数据源"，其实 hub.derivatives 一直在采
 · 问金叉 → 自己写 python 脚本拿 K 线算 MACD，而 liveFeatures 里现成的
 · 问止损挂上没 → 只看 positions 的 sl 字段就断言"已保护"，从没查过交易所
 · 问昨天权益/这周曲线 → 答"读不到历史权益"
 · 问历史开过多少单 → 只拿到最近 20 笔就说"只有这些"
"""
import json
import time

from ai.agent import SYSTEM_PROMPT
from ai.agent_tools import TOOL_NAMES, read_snapshot


# ---------- 工具存在性 ----------

def test_new_read_tools_are_registered():
    for name in ("read_orders", "read_derivatives", "read_equity", "read_trades"):
        assert name in TOOL_NAMES, f"缺少工具 {name}"


def test_prompt_points_every_data_class_to_a_tool():
    """每类页面数据都要在提示词里点名对应工具，否则模型会说"没有数据源"。"""
    for phrase in ("read_derivatives", "read_orders", "read_equity", "read_trades",
                   "没有这个数据源", "features", "gateLog", "dailyPnl"):
        assert phrase in SYSTEM_PROMPT, f"提示词缺少: {phrase}"


def test_prompt_forbids_guessing_protection_from_position_fields():
    assert "必须调 read_orders" in SYSTEM_PROMPT
    assert "裸奔" in SYSTEM_PROMPT


# ---------- read_derivatives ----------

DERIV = {
    "btcusdt": {"price": 78319.7, "changePct": 1.94, "fundingRate": 8.29e-05,
                "nextFundingMs": 1793808000000, "openInterest": 35163.7,
                "oiChangePct": 0.71, "accountLongShort": 1.96,
                "topPositionLongShort": 1.96, "takerBuySellRatio": 0.959,
                "topLongChange24h": 0.67, "venue": "okx"},
    "ethusdt": {"price": 3000.0, "fundingRate": 0.0001, "venue": "okx"},
}


def test_read_derivatives_returns_exchange_fields():
    out = read_snapshot("read_derivatives", {"symbol": "BTC"}, {"derivatives": DERIV})
    assert out["available"] is True
    row = out["rows"]["btcusdt"]
    assert row["fundingRate"] == 8.29e-05
    assert row["accountLongShort"] == 1.96
    assert row["venue"] == "okx"
    # 口径解释必须一起给：户数比 vs 持仓量比经常反向
    assert "户数" in out["fieldMeaning"]["accountLongShort"]
    assert "持仓量" in out["fieldMeaning"]["topPositionLongShort"]


def test_read_derivatives_symbol_filter_and_unknown():
    out = read_snapshot("read_derivatives", {"symbol": "ETH"}, {"derivatives": DERIV})
    assert list(out["rows"]) == ["ethusdt"]
    miss = read_snapshot("read_derivatives", {"symbol": "SOL"}, {"derivatives": DERIV})
    assert miss["available"] is False
    assert "btcusdt" in miss["available_symbols"], "要告诉模型哪些币有数据"


def test_read_derivatives_empty_is_not_crash():
    out = read_snapshot("read_derivatives", {}, {})
    assert out["available"] is False
    assert "还没采集到" in out["reason"]


# ---------- read_orders ----------

def test_read_orders_reports_gaps_and_algo_orders():
    seen = {}

    def fake_orders(symbol):
        seen["symbol"] = symbol
        return {"ts": 1,
                "symbols": {"btcusdt": [
                    {"symbol": "BTCUSDT", "side": "SELL", "type": "STOP_MARKET",
                     "triggerPrice": "77000", "reduceOnly": "true", "algo": True}]},
                "gaps": [], "errors": []}

    out = read_snapshot("read_orders", {"symbol": "btc"}, {}, fns={"orders": fake_orders})
    assert seen["symbol"] == "btcusdt", "币种写法要归一"
    assert out["available"] is True
    assert out["count"] == 1
    assert out["orders"]["btcusdt"][0]["algo"] is True
    assert out["gaps"] == []
    assert "算法条件单" in out["note"]


def test_read_orders_surfaces_naked_positions():
    def fake_orders(symbol):
        return {"symbols": {"btcusdt": []}, "gaps": ["btcusdt"], "errors": []}

    out = read_snapshot("read_orders", {}, {}, fns={"orders": fake_orders})
    assert out["gaps"] == ["btcusdt"], "有仓没保护必须能被模型看见"


def test_read_orders_without_fn_says_so():
    out = read_snapshot("read_orders", {}, {}, fns={})
    assert out["available"] is False
    assert "未接入" in out["reason"]


def test_read_orders_failure_is_reported_not_hidden():
    def boom(symbol):
        raise RuntimeError("HTTP 400: -1021 timestamp")

    out = read_snapshot("read_orders", {}, {}, fns={"orders": boom})
    assert out["available"] is False
    assert "-1021" in out["reason"]


# ---------- read_equity ----------

def test_read_equity_computes_daily_pnl():
    daily = [{"date": "2026-11-01", "open": 7300, "close": 7350, "high": 7360, "low": 7290},
             {"date": "2026-11-02", "open": 7350, "close": 7300, "high": 7360, "low": 7280},
             {"date": "2026-11-03", "open": 7300, "close": 7352.95, "high": 7360, "low": 7290}]
    series = [{"t": i, "bal": 7352.95, "upnl": 0} for i in range(5)]

    def fake_equity(days, hours):
        return {"account": "OODD", "daily": daily, "series": series}

    out = read_snapshot("read_equity", {"days": 7, "hours": 24}, {},
                        fns={"equity": fake_equity})
    assert out["available"] is True
    assert out["account"] == "OODD"
    pnl = out["dailyPnl"]
    assert [p["pnl"] for p in pnl] == [-50.0, 52.95]
    assert pnl[0]["date"] == "2026-11-02"
    assert out["seriesPoints"] == 5


def test_read_equity_without_data_explains_why():
    def empty(days, hours):
        return {"account": None, "daily": [], "series": []}

    out = read_snapshot("read_equity", {}, {}, fns={"equity": empty})
    assert out["available"] is False


def test_read_equity_rejects_bad_range():
    for bad in ({"days": 0}, {"days": 400}, {"hours": 0}, {"hours": 9999}):
        try:
            read_snapshot("read_equity", bad, {}, fns={"equity": lambda d, h: {}})
        except ValueError:
            continue
        raise AssertionError(f"{bad} 应该被拒绝")


# ---------- read_trades ----------

def test_read_trades_returns_all_time_stats():
    rows = [{"symbol": "btcusdt", "pnl": 0.75}, {"symbol": "btcusdt", "pnl": -0.14}]

    def fake_trades(days, symbol, limit):
        return {"account": "OODD", "trades": rows, "total": 260,
                "stats": {"count": 1, "wins": 0, "losses": 0, "pnl": 0.0},
                "allTime": {"count": 260, "wins": 120, "losses": 90, "pnl": 110.12},
                "source": "mysql"}

    out = read_snapshot("read_trades", {"days": 0, "limit": 10}, {},
                        fns={"trades": fake_trades})
    assert out["available"] is True
    assert out["allTime"]["count"] == 260
    assert out["total"] == 260, "要能说出全历史共多少笔"
    assert "不能当全历史" in out["note"]


def test_read_trades_symbol_normalized():
    seen = {}

    def fake_trades(days, symbol, limit):
        seen["symbol"] = symbol
        return {"trades": [], "stats": {}, "allTime": {}, "total": 0}

    read_snapshot("read_trades", {"symbol": "BTC", "limit": 5}, {},
                  fns={"trades": fake_trades})
    assert seen["symbol"] == "btcusdt"


def test_read_trades_rejects_bad_limit():
    for bad in ({"limit": 0}, {"limit": 201}, {"limit": "5"}):
        try:
            read_snapshot("read_trades", bad, {}, fns={"trades": lambda *a: {}})
        except ValueError:
            continue
        raise AssertionError(f"{bad} 应该被拒绝")


# ---------- read_market 带 features ----------

def test_read_market_exposes_live_features():
    """MACD/挤压/CVD/OBI/HTF 这些算好的值必须直接给，别让模型自己算。"""
    feats = {"btcusdt": {"macd": {"hist": -19.4, "rising": True},
                         "squeeze": {"on": False, "release": False},
                         "obi": {"valid": True, "ratio": 1.33},
                         "htf": {"direction": "neutral"},
                         "liquidation": {"active": False}}}
    snap = {"market": {"btcusdt": {"price": 78319.7}},
            "liveFeatures": feats,
            "symbols": ["btcusdt"]}
    out = read_snapshot("read_market", {"symbol": "BTC"}, snap)
    data = out["data"]
    assert data["features"]["btcusdt"]["macd"]["hist"] == -19.4
    assert "金叉" in data["featureNote"]
    assert "不要自己重算指标" in data["featureNote"]


def test_read_market_without_features_still_works():
    out = read_snapshot("read_market", {}, {"market": {"btcusdt": {"price": 1}}})
    assert out["data"]["market"]["btcusdt"]["price"] == 1
    assert "features" not in out["data"]


def test_read_market_symbol_filter_applies_to_features():
    """按币查时要连特征一起筛，别把十个币的特征全塞回去。"""
    feats = {f"{s}usdt": {"macd": {"hist": 0.0}} for s in ("btc", "eth", "sol")}
    snap = {"market": dict(feats), "liveFeatures": feats}
    out = read_snapshot("read_market", {"symbol": "ETH"}, snap)
    assert list(out["data"]["features"]) == ["ethusdt"]


# ---------- read_risk 带 gateLog ----------

def test_read_risk_exposes_gate_log_and_traces():
    """问"刚才为什么没开单"要有拦截记录与决策轨迹。"""
    snap = {"drawdown": {"current": -0.07, "paused": False},
            "params": {"minScore": 55},
            "gateLog": [{"ts": 1, "symbol": "tslausdt", "reason": "评分未通过"}],
            "decisionTraces": [{"symbol": "btcusdt", "status": "blocked"}],
            "health": {"mode": "binance"}}
    out = read_snapshot("read_risk", {}, snap)
    data = out["data"]
    assert data["gateLog"][0]["reason"] == "评分未通过"
    assert data["decisionTraces"][0]["status"] == "blocked"
    assert "gateLog" in data["note"]
    assert "只读" in data["note"]


def test_read_risk_omits_empty_gate_log():
    out = read_snapshot("read_risk", {}, {"drawdown": {}, "params": {}})
    assert "gateLog" not in out["data"]
