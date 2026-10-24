"""引擎级测试：同一根K线命中多个形态时的优先级去重。"""
from patterns.base import all_patterns, get_enabled
import patterns.builtin  # noqa: F401
from core.engine import PatternEngine

from tests.test_patterns import mk, trend


def overlap_bars():
    """上升趋势 + 前阳后阴：新K线开在前高附近回落，同时满足看跌吞没与平头顶部。"""
    cs = trend(step=0.01)
    p = cs[-1].close
    prev = mk(p, p * 1.004, h=p * 1.004)                      # 阳线收在最高点
    cur = mk(p * 1.0042, p * 0.9995, h=p * 1.0043, l=p * 0.999)
    return cs + [prev, cur]


def test_overlap_both_match():
    cs = overlap_bars()
    assert all_patterns()["bearish_engulfing"].check(cs)
    assert all_patterns()["tweezer_top"].check(cs)            # 两个形态确实同时成立


def test_same_bar_reports_only_highest_priority():
    cs = overlap_bars()
    hits = []
    eng = PatternEngine(get_enabled("all"), hits.append)
    eng.on_candle("BTCUSDT", "1m", cs[-1], closed=True, history=cs)
    assert len(hits) == 1
    assert hits[0].pattern == "bearish_engulfing"             # 只报优先级更高的吞没


def test_same_bar_never_fires_twice():
    cs = overlap_bars()
    hits = []
    eng = PatternEngine(get_enabled("all"), hits.append)
    eng.on_candle("BTCUSDT", "1m", cs[-1], closed=True, history=cs)
    eng.on_candle("BTCUSDT", "1m", cs[-1], closed=True, history=cs)
    assert len(hits) == 1
