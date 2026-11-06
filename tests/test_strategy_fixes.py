"""数据驱动的策略修复回归：默认开关、rr 下界、方向过滤、回放判定口径。

依据 scripts/param_sweep.py 的 7 天 2809 笔 / 30 天 4094 笔线上回放：
只做空为正期望、rr 3.0 是扫描最优档、55/15 硬门槛在制造负 alpha。
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from decision.config import DecisionConfig  # noqa: E402
from decision.paper import PaperBook  # noqa: E402
from decision.scoring import score_signal  # noqa: E402
from param_sweep import replay  # noqa: E402
from quant import QuantEngine  # noqa: E402


def test_defaults_follow_replay_data():
    cfg = DecisionConfig()
    assert cfg.allow_long is False
    assert cfg.score_gate_enabled is False
    assert cfg.watch_tp_atr_mult == 4.5


def test_switches_can_be_reenabled_from_config():
    cfg = DecisionConfig.from_dict({
        "allow_long": True, "score_gate_enabled": True,
        "watch_tp_atr_mult": 2.5,
    })
    assert cfg.allow_long is True
    assert cfg.score_gate_enabled is True
    assert cfg.watch_tp_atr_mult == 2.5


def _state():
    return {"regime": "range", "adx": 10, "rsi": 50,
            "plusDI": 20, "minusDI": 20}


def _features():
    return {"close": 100.0, "emaFast": 100.0, "emaSlow": 100.0,
            "emaMacro": 100.0}


def test_low_score_is_allowed_when_gate_disabled():
    off = score_signal("bullish", _state(), _features(),
                       DecisionConfig(score_gate_enabled=False))
    assert off.allowed
    assert not any("共鸣分" in v for v in off.vetoes)


def test_gate_still_vetoes_when_explicitly_enabled():
    on = score_signal("bullish", _state(), _features(),
                      DecisionConfig(score_gate_enabled=True,
                                     minimum_score=999.0))
    assert not on.allowed
    assert any("共鸣分" in v for v in on.vetoes)


def test_long_signal_is_filtered_by_default():
    eng = object.__new__(QuantEngine)
    eng.enabled = True
    eng._op_lock = threading.Lock()
    eng.decision_cfg = DecisionConfig()
    eng.symbol_filter = None
    eng.feed_is_live_fn = None
    eng.stats = {"signals": 0}
    eng._primary_account = lambda: "acct"
    eng._event = MagicMock()
    eng._trace_start = MagicMock()
    eng.price_fn = lambda s: 100.0

    eng.on_signal({"symbol": "btcusdt", "direction": "bullish",
                   "pattern": "hammer", "price": 100.0})
    eng._event.assert_called_once()
    assert "只做空" in eng._event.call_args.args[1]
    eng._trace_start.assert_not_called()


def test_short_signal_is_not_filtered():
    eng = object.__new__(QuantEngine)
    eng.enabled = True
    eng._op_lock = threading.Lock()
    eng.decision_cfg = DecisionConfig()
    eng.symbol_filter = None
    eng.feed_is_live_fn = None
    eng.bars_fn = None
    eng.stats = {"signals": 0, "errors": 0}
    eng._primary_account = lambda: "acct"
    eng._event = MagicMock()
    eng._trace_start = MagicMock(return_value={})
    eng.price_fn = lambda s: 100.0
    eng._decide_open = MagicMock()

    # 只验证方向门没拦做空；后续决策链需要完整运行时上下文，异常由
    # on_signal 自己兜住，这里不关心。
    try:
        eng.on_signal({"symbol": "btcusdt", "direction": "bearish",
                       "pattern": "hammer", "price": 100.0})
    except Exception:
        pass
    reasons = [str(c.args[1]) for c in eng._event.call_args_list
               if len(c.args) > 1]
    assert not any("只做空" in r for r in reasons)


def test_paper_skips_long_when_disabled():
    book = PaperBook()
    ctx = {"direction": "bullish", "atr": 1.0, "score": 70.0, "gap": 20.0,
           "grade": "medium", "pattern": "hammer", "patternStats": {},
           "maxLeverage": 20, "allowLong": False, "price": 100.0}
    assert book._open("btcusdt", ctx, 100.0) is None
    assert not book.positions


def test_paper_default_rr_is_three():
    book = PaperBook()
    ctx = {"direction": "bearish", "atr": 1.0, "score": 70.0, "gap": 20.0,
           "grade": "medium", "pattern": "hammer", "patternStats": {},
           "maxLeverage": 20, "allowLong": False, "price": 100.0}
    book._open("btcusdt", ctx, 100.0)
    assert book.positions
    pos = book.positions["btcusdt"]
    assert pos.rr == 3.0


def _bars(*rows):
    return [(0, 100.0, 100.0, 100.0, 100.0)] + list(rows)


def test_replay_same_bar_double_touch_is_loss():
    bars = _bars((60_000, 100.0, 102.0, 99.0, 100.0),
                 (120_000, 100.0, 100.1, 99.9, 100.0),
                 (180_000, 100.0, 100.1, 99.9, 100.0))
    r = replay(bars, 0, 100.0, "bullish", 0.004, 3.0, 10, 0.1)
    assert r == pytest.approx(-0.5)


def test_replay_target_only_is_win():
    bars = _bars((60_000, 100.0, 101.3, 99.9, 101.2),
                 (120_000, 100.0, 101.4, 100.0, 101.3),
                 (180_000, 100.0, 101.4, 100.0, 101.3))
    r = replay(bars, 0, 100.0, "bullish", 0.004, 3.0, 10, 0.1)
    assert r == pytest.approx(1.1)


def test_replay_short_window_returns_none():
    assert replay(_bars((60_000, 100.0, 100.1, 99.9, 100.0)),
                  0, 100.0, "bullish", 0.004, 3.0, 10, 0.1) is None
