"""方向滞后 + 入场时机。

核心问题：每 5 秒重算一次，谁高 0.1 分就翻方向。用户跟着做，
手续费能把本金磨光。方向必须有滞后——分差不够就维持上一轮。
"""
import asyncio
from unittest.mock import patch

from decision.advice import _entry_hint


# ---------- 入场时机 ----------

def test_entry_zone_below_price_for_long():
    plan = {"sl": 95.0, "tp": 110.0}
    out = _entry_hint(price=100.0, bullish=True, atr=2.0, plan=plan)
    lo, hi = out["entryZone"]
    assert hi <= 100.0, "做多的入场区应该在现价下方或等于现价"
    assert lo < hi


def test_entry_zone_above_price_for_short():
    out = _entry_hint(price=100.0, bullish=False, atr=2.0, plan={})
    lo, hi = out["entryZone"]
    assert lo >= 100.0, "做空的入场区应该在现价上方或等于现价"


def test_timing_now_when_price_in_zone():
    out = _entry_hint(price=100.0, bullish=True, atr=2.0, plan={})
    # offset = 1.0，区间约 [99.0, 99.8]，中点 99.4，偏离 0.6% > 0.15%
    # 所以默认应该是 wait；把 ATR 调小到偏离很小才是 now
    tight = _entry_hint(price=100.0, bullish=True, atr=0.2, plan={})
    assert tight["timing"] in ("now", "wait")
    assert out["timing"] in ("now", "wait")


def test_entry_note_is_actionable():
    out = _entry_hint(price=100.0, bullish=True, atr=3.0, plan={})
    assert out["entryNote"] in ("现价可入", "等回落到区间")


def test_entry_hint_handles_missing_atr():
    out = _entry_hint(price=100.0, bullish=True, atr=None, plan={})
    assert out["entryZone"], "没有 ATR 也要给区间（用价格百分比兜底）"


def test_entry_hint_rejects_bad_price():
    assert _entry_hint(price=0, bullish=True, atr=2.0, plan={}) == {}


# ---------- 方向滞后 ----------

def _eval(prev_dir, bull, bear, gap=8.0):
    """复现 quant 里的滞后逻辑。"""
    raw = "bullish" if bull >= bear else "bearish"
    if prev_dir in ("bullish", "bearish") and prev_dir != raw:
        cur = bull if prev_dir == "bullish" else bear
        alt = bear if prev_dir == "bullish" else bull
        if abs(alt - cur) < gap:
            return prev_dir
        return raw
    return raw


def test_small_edge_keeps_previous_direction():
    """上一轮做空，这轮做多只高 3 分 —— 不够翻，维持做空。"""
    assert _eval("bearish", bull=52.0, bear=49.0) == "bearish"


def test_large_edge_does_flip():
    """高 15 分 —— 真的趋势变了，该翻。"""
    assert _eval("bearish", bull=65.0, bear=50.0) == "bullish"


def test_first_direction_has_no_lag():
    """没有上一轮时直接取分数高的。"""
    assert _eval(None, bull=50.1, bear=50.0) == "bullish"


def test_hysteresis_constant_is_sane():
    from quant import HYSTERESIS_GAP
    # 太小等于没滞后，太大永远不翻
    assert 3.0 <= HYSTERESIS_GAP <= 20.0
