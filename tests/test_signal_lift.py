"""signal_lift 纯函数回归：判定口径必须和 storage.eval_outcomes 完全一致。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from signal_lift import SCORE_BINS, bin_label, judge, summarize  # noqa: E402


def test_bullish_touches_up_first_is_win():
    window = [(100.05, 99.95, 100.04), (100.30, 100.00, 100.20),
              (100.10, 100.00, 100.05)]
    assert judge(100.0, "bullish", window, 0.0015)[0] == "win"


def test_bearish_touches_down_first_is_win():
    window = [(100.05, 99.95, 100.00), (100.10, 99.70, 99.80),
              (100.05, 99.90, 100.00)]
    assert judge(100.0, "bearish", window, 0.0015)[0] == "win"


def test_same_bar_both_touched_is_loss():
    window = [(100.30, 99.70, 100.00), (100.10, 99.90, 100.00),
              (100.10, 99.90, 100.00)]
    assert judge(100.0, "bullish", window, 0.0015)[0] == "loss"


def test_no_touch_is_flat():
    window = [(100.05, 99.95, 100.00)] * 3
    assert judge(100.0, "bullish", window, 0.0015)[0] == "flat"


def test_short_window_is_skipped():
    assert judge(100.0, "bullish", [(100.10, 99.90, 100.00)], 0.0015)[0] is None


def test_judge_returns_window_close():
    window = [(100.05, 99.95, 100.00)] * 2 + [(100.05, 99.95, 100.07)]
    assert judge(100.0, "bullish", window, 0.0015)[1] == 100.07


def test_bin_label_boundaries():
    assert bin_label(54.9, SCORE_BINS) == "45-55"
    assert bin_label(55.0, SCORE_BINS) == "55-65"
    assert bin_label(80.0, SCORE_BINS) == "75-101"


def test_summarize_uses_random_entry_lift():
    rows = [{"outcome": "win", "ret": 1.0},
            {"outcome": "loss", "ret": -1.0},
            {"outcome": "flat", "ret": 0.0}]
    s = summarize(rows, [0.0, 0.0])
    assert s["n"] == 3 and s["judged"] == 2 and s["wins"] == 1
    assert s["winrate"] == 0.5
    assert s["lift"] == 0.0
