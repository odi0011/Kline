"""Agent 要能读到某个币在交易所的**全部数据**，不限监控列表与周期。

用户原话：
  "agent能读到某个币种的全部数据，包括不限于多空压力实时数据，
   1m，3m，15m，1h，3h，1日等等所有数据…不要只停留在系统层面，
   而是所有的一切的，能被拿到的"

之前的限制：
  · read_candles 只读本地聚合器 → 只有监控中的币、只有已订阅周期
  · 没有"一次拿全"的入口 → 模型得一个个试工具，还容易漏
"""
import asyncio

import pytest

from ai.agent import AgentManager
from ai.agent_tools import TOOL_NAMES


def test_everything_tool_exists():
    assert "read_symbol_everything" in TOOL_NAMES


def test_candles_tool_documents_any_interval_and_symbol():
    from ai.agent_tools import TOOLS
    spec = next(t for t in TOOLS if t["function"]["name"] == "read_candles")
    desc = spec["function"]["description"]
    assert "任何周期" in desc
    assert "任何币种" in desc
    assert "不在监控列表" in desc
    # limit 上限放宽到 500（原来 120，读日线不够看）
    assert spec["function"]["parameters"]["properties"]["limit"]["maximum"] == 500


def test_everything_tool_schema():
    from ai.agent_tools import TOOLS
    spec = next(t for t in TOOLS
                if t["function"]["name"] == "read_symbol_everything")
    props = spec["function"]["parameters"]["properties"]
    assert set(props) == {"symbol", "interval", "limit", "trades"}
    assert spec["function"]["parameters"]["required"] == ["symbol"]


# ---------- read_symbol_everything 的行为 ----------

class _FakeFetch:
    """模拟 main 注入的交易所取数入口。"""

    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail or set()

    def __call__(self, what, symbol, params=None):
        self.calls.append((what, symbol, dict(params or {})))
        if what in self.fail:
            raise RuntimeError(f"{what} boom")
        if what == "ticker":
            return {"symbol": symbol, "last": 78300.0, "changePct": 1.2,
                    "high": 79000.0, "low": 77000.0, "quoteVolume": 8.0e9,
                    "trades": 123456}
        if what == "candles":
            return [{"t": i, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10,
                     "buy": 6, "sell": 4} for i in range(int((params or {}).get("limit") or 60))]
        if what == "derivatives":
            return {"fundingRate": 8e-05, "oiChangePct": 0.7,
                    "accountLongShort": 1.96, "venue": "okx"}
        if what == "depth":
            return {"bids": [[78300.0, 2.0], [78290.0, 3.0]],
                    "asks": [[78310.0, 1.0], [78320.0, 1.5]],
                    "ts": 1}
        if what == "trades":
            return [
                {"ts": 1, "price": 78300, "qty": 0.5, "notional": 39150, "side": "buy"},
                {"ts": 2, "price": 78301, "qty": 0.2, "notional": 15660, "side": "sell"},
                {"ts": 3, "price": 78302, "qty": 0.3, "notional": 23490, "side": "buy"},
            ]
        raise RuntimeError(f"unknown {what}")


def _manager(fetch, context=None):
    """构造一个只测 _read_symbol_everything 的最小 manager。

    不走 make_manager（那要 monkeypatch 一堆 provider），这里只验证
    这个方法本身的取数/汇总/容错逻辑。
    """
    class _M(AgentManager):
        def __init__(self):
            self.symbol_fetch_fn = fetch
            self.context_fn = context or (lambda: {})
            self.symbols_fn = lambda: ["btcusdt"]
    return _M()


def test_everything_merges_exchange_and_system():
    fetch = _FakeFetch()
    ctx = lambda: {
        "market": {"btcusdt": {"price": 78300, "rsi": 55, "adx": 25}},
        "evaluations": {"btcusdt": {"direction": "bullish", "score": 62}},
        "liveFeatures": {"btcusdt": {"macd": {"hist": 1.0}}},
        "derivatives": {"btcusdt": {"fundingRate": 8e-05}},
        "positions": [{"symbol": "btcusdt", "qty": 0.001}],
    }
    mgr = _manager(fetch, ctx)
    out = asyncio.run(mgr._read_symbol_everything({"symbol": "BTC", "interval": "1d", "limit": 30}))
    assert out["available"] is True
    assert out["symbol"] == "btcusdt", "币种写法要归一"
    assert out["ticker"]["last"] == 78300.0
    assert len(out["candles"]) == 30
    assert out["derivatives"]["fundingRate"] == 8e-05
    # 盘口压力按前 20 档买卖金额算
    assert out["depthPressure"]["bidPct"] > 0
    assert out["depthPressure"]["bestBid"] == 78300.0
    assert out["depthPressure"]["bestAsk"] == 78310.0
    # 逐笔成交 → 主动买卖比
    assert out["tradeFlow"]["buyNotional"] == pytest.approx(39150 + 23490)
    assert out["tradeFlow"]["buyPct"] > 50
    # 系统评估单独一块，并且标了有没有持仓
    assert out["system"]["evaluation"]["score"] == 62
    assert out["system"]["hasPosition"] is True


def test_everything_works_for_unmonitored_symbol():
    """监控外的币也要能读——这是这次需求的核心。"""
    fetch = _FakeFetch()
    mgr = _manager(fetch, lambda: {"market": {}, "evaluations": {}})
    out = asyncio.run(mgr._read_symbol_everything({"symbol": "SOL", "interval": "4h"}))
    assert out["available"] is True
    assert out["ticker"] is not None
    assert out["candles"], "监控外的币也必须拿得到K线"
    # 系统没有这个币的评估 → 留空，不编
    assert out["system"].get("state") is None
    assert out["system"].get("evaluation") is None


def test_everything_reports_partial_failures():
    """某个数据源挂了要如实标出来，不能让整次读取失败。"""
    fetch = _FakeFetch(fail={"derivatives"})
    mgr = _manager(fetch)
    out = asyncio.run(mgr._read_symbol_everything({"symbol": "BTC"}))
    assert out["available"] is True
    assert out["derivatives"] is None
    assert "derivatives" in out["errors"]


def test_everything_without_fetch_fn():
    mgr = _manager(None)
    out = asyncio.run(mgr._read_symbol_everything({"symbol": "BTC"}))
    assert out["available"] is False
    assert "未接入" in out["reason"]


def test_everything_validates_arguments():
    fetch = _FakeFetch()
    mgr = _manager(fetch)
    for bad in ({"symbol": "BTC", "limit": 5},
                {"symbol": "BTC", "limit": 999},
                {"symbol": "BTC", "trades": -1},
                {"symbol": "BTC", "trades": 9999},
                {"symbol": "BTC", "bogus": 1}):
        with pytest.raises(ValueError):
            asyncio.run(mgr._read_symbol_everything(bad))


def test_everything_default_interval_is_15m():
    fetch = _FakeFetch()
    mgr = _manager(fetch)
    asyncio.run(mgr._read_symbol_everything({"symbol": "BTC"}))
    candle_call = next(c for c in fetch.calls if c[0] == "candles")
    assert candle_call[2]["interval"] == "15m"


# ---------- read_candles 走交易所 ----------

def test_read_candles_prefers_exchange():
    """指定了周期就优先问交易所——这样监控外的币/未订阅周期也能读。"""
    calls = []

    class _Fetch:
        def __call__(self, what, symbol, params=None):
            calls.append((what, symbol, params))
            return [{"t": i, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10,
                     "buy": 6, "sell": 4} for i in range(12)]

    class _M(AgentManager):
        def __init__(self):
            self.symbol_fetch_fn = _Fetch()
            self.bars_fn = lambda s, iv: (_ for _ in ()).throw(
                AssertionError("有交易所就不该用本地"))
            self.symbols_fn = lambda: ["btcusdt"]

    mgr = _M()
    out = mgr._read_candles({"symbol": "SOL", "interval": "3h", "limit": 12})
    assert out["available"] is True
    assert out["source"] == "exchange"
    assert calls and calls[0][2]["interval"] == "3h"


def test_read_candles_falls_back_to_local():
    class _M(AgentManager):
        def __init__(self):
            self.symbol_fetch_fn = lambda w, s, p=None: []
            self.bars_fn = lambda s, iv: [type("C", (), {
                "open_time": i, "open": 1, "high": 2, "low": 0.5,
                "close": 1.5, "volume": 10, "buy_vol": 6, "sell_vol": 4})()
                for i in range(12)]
            self.symbols_fn = lambda: ["btcusdt"]

    out = _M()._read_candles({"symbol": "BTC", "interval": "", "limit": 12})
    assert out["available"] is True
    assert out["source"] == "local"


def test_read_candles_unmonitored_message_is_not_misleading():
    """取不到时要说明是"取不到"，不能说"不在监控范围"（那是两件事）。"""
    class _M(AgentManager):
        def __init__(self):
            self.symbol_fetch_fn = lambda w, s, p=None: []
            self.bars_fn = lambda s, iv: []
            self.symbols_fn = lambda: []

    out = _M()._read_candles({"symbol": "FOOUSDT", "interval": "1d", "limit": 20})
    assert out["available"] is False
    assert "取不到" in out["reason"]
    assert "不在当前监控列表" not in out["reason"]


def test_prompt_teaches_full_data_access():
    """提示词必须明确：任何币任何周期都能读，一次拿全用哪个工具。"""
    from ai.agent import SYSTEM_PROMPT
    for phrase in ("任何币种、任何周期", "不受监控列表限制",
                   "read_symbol_everything", "一次拿全",
                   "别答\"这个币没在监控里\"",
                   "只有交易所本身没有的东西"):
        assert phrase in SYSTEM_PROMPT, f"缺少: {phrase}"
