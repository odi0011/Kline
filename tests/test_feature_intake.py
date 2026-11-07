"""信号特征必须在所有提前 return 之前落库，且每个信号恰好一条。

实机证据：近 3 小时 161 条 hits 只落了 1 条 signal_features。原因是
_persist_signal_features 只在评分 veto / 质量门两处调用，而 on_signal
前面有演示行情 / 展示范围 / 只做空 / 影子模式 / 中性形态 / 无报价等
六个提前 return，被这些分支挡掉的信号全部没有特征快照。
"""
import threading
from unittest.mock import MagicMock

from core.candle import Candle
from decision.config import DecisionConfig
from quant import QuantEngine


def _bars(n: int = 40):
    return [Candle(open_time=i * 60_000, open=100.0, high=100.4, low=99.6,
                   close=100.0 + (i % 3) * 0.05, volume=10.0,
                   interval="3m", closed=True) for i in range(n)]


def _engine(cfg: DecisionConfig | None = None):
    eng = object.__new__(QuantEngine)
    eng.enabled = True
    eng._op_lock = threading.Lock()
    eng.decision_cfg = cfg or DecisionConfig()
    eng.feature_storage_fn = MagicMock()
    eng.bars_fn = lambda s: _bars()
    eng.indicators = {"btcusdt": {"regime": "range", "atrPct": 0.004,
                                  "adx": 18.0, "atr": 0.4, "rsi": 50.0,
                                  "z": 0.1, "volState": "normal",
                                  "squeeze": False}}
    eng.price_fn = lambda s: 100.0
    eng.live_evaluations = {"btcusdt": {"score": 60.0, "gap": 20.0,
                                        "grade": "medium", "confidence": 0.6}}
    eng.feed_is_live_fn = None
    eng.symbol_filter = None
    eng.stats = {"signals": 0, "errors": 0}
    eng._event = MagicMock()
    eng._primary_account = lambda: "acct"
    # 中性信号会走到决策流水，给最小可用的桩，避免测试依赖整条链
    eng._trace_seq = 0
    eng._trace_start = MagicMock(return_value={})
    eng._trace_stage = MagicMock()
    eng._finish_trace = MagicMock()
    return eng


def _hit(direction: str = "bearish", pattern: str = "rising_wedge"):
    return {"symbol": "btcusdt", "interval": "3m", "direction": direction,
            "pattern": pattern, "name_zh": "", "mode": "confirmed",
            "price": 100.0, "bar_open_time": 1_700_000_000_000}


def _rows(eng):
    return [c.args[0] for c in eng.feature_storage_fn.call_args_list]


def test_bullish_signal_blocked_by_short_only_still_persists_features():
    eng = _engine()
    eng.on_signal(_hit("bullish"))
    rows = _rows(eng)
    assert len(rows) == 1, "只做空分支提前 return，但仍必须留下特征快照"
    r = rows[0]
    assert r["symbol"] == "btcusdt" and r["pattern"] == "rising_wedge"
    assert r["direction"] == "bullish" and r["interval"] == "3m"
    assert r["regime"] == "range" and r["score"] == 60.0
    assert "quality_allowed" in r


def test_shadow_mode_signal_still_persists_features():
    cfg = DecisionConfig(auto_trade_enabled=False)
    eng = _engine(cfg)
    eng.on_signal(_hit("bearish"))
    assert len(_rows(eng)) == 1


def test_directionless_signal_still_persists_features():
    eng = _engine()
    eng.on_signal(_hit("neutral", "doji"))
    assert len(_rows(eng)) == 1


def test_symbol_out_of_scope_still_persists_features():
    eng = _engine()
    eng.symbol_filter = {"ethusdt"}
    eng.on_signal(_hit("bearish"))
    assert len(_rows(eng)) == 1


def test_no_duplicate_when_intake_succeeded():
    """入口写成功后不能再在质量门处重复 INSERT。"""
    eng = _engine()
    state = {"done": False}
    eng._persist_intake_features(_hit("bearish"), "btcusdt", "bearish", state)
    assert state["done"] is True
    assert eng.feature_storage_fn.call_count == 1
    # 模拟上游按 state 决定是否兜底
    if not state["done"]:
        eng._persist_signal_features(_hit("bearish"), "btcusdt", "bearish",
                                     100.0, {}, {}, None, _bars(), None)
    assert eng.feature_storage_fn.call_count == 1


def test_persist_returns_success_flag_and_survives_sink_failure():
    eng = _engine()
    assert eng._persist_signal_features(_hit("bearish"), "btcusdt", "bearish",
                                        100.0, {}, {}, None, _bars(), None) is True
    eng.feature_storage_fn.side_effect = RuntimeError("db down")
    assert eng._persist_signal_features(_hit("bearish"), "btcusdt", "bearish",
                                        100.0, {}, {}, None, _bars(), None) is False
