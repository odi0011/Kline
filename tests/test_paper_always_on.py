"""模拟仓设计契约：每个监控币时时刻刻有单，平仓即按当下评估重开。

2026-11-11 实机事故：paper_positions 从 4 个变成 0 个，前端只剩
「等待评估数据…」。两个根因都与模拟仓被实盘规则绑架有关：
  1) 前端展示勾选变化时 close_missing 平掉范围外的仓；
  2) allow_long=False 让评估偏多的币根本开不出仓（探针 5 币只开 2 个）。
"""
from __future__ import annotations

import inspect
from pathlib import Path

from decision.config import DecisionConfig
from decision.paper import PaperBook
from quant import QuantEngine

REPO = Path(__file__).resolve().parents[1]


def _ctx(**kw):
    d = {
        "price": 100.0, "atr": 1.0, "score": 60.0, "gap": 20.0,
        "grade": "medium", "direction": "bullish", "reason": "test",
        "regime": "range", "pattern": "", "patternStats": {},
        "maxLeverage": 20, "allowLong": True, "bars": [],
    }
    d.update(kw)
    return d


def test_defaults_keep_paper_positions_always_on():
    cfg = DecisionConfig()
    assert cfg.paper_scope_cleanup is False
    assert cfg.paper_follow_live_gate is False
    # 实盘仍只做空，两个开关不影响真实下单规则
    assert cfg.allow_long is False

    on = DecisionConfig.from_dict({
        "paper_scope_cleanup": True, "paper_follow_live_gate": True})
    assert on.paper_scope_cleanup is True
    assert on.paper_follow_live_gate is True


def test_paper_context_does_not_inherit_short_only_gate():
    eng = object.__new__(QuantEngine)
    eng.indicators = {"btcusdt": {"price": 100.0, "atr": 1.0, "regime": "range"}}
    eng.live_evaluations = {"btcusdt": {
        "direction": "bullish", "score": 60.0, "gap": 20.0,
        "grade": "medium", "vetoes": []}}
    eng.max_leverage_fn = None
    eng.pattern_stats_fn = None
    eng.bars_fn = None
    eng.decision_cfg = DecisionConfig()

    assert eng._paper_context("btcusdt")["allowLong"] is True

    eng.decision_cfg = DecisionConfig(paper_follow_live_gate=True)
    assert eng._paper_context("btcusdt")["allowLong"] is False


def test_short_only_gate_was_the_reason_positions_went_empty():
    """旧口径：偏多方向 + allowLong=False → 一个仓都开不出来。"""
    book = PaperBook()
    book.tick("btcusdt", _ctx(direction="bullish", allowLong=False))
    assert not book.positions

    # 新口径：同一信号能开
    got = book.tick("btcusdt", _ctx(direction="bullish", allowLong=True))
    assert got is not None and "btcusdt" in book.positions


def test_closed_position_reopens_on_next_tick():
    book = PaperBook()
    book.tick("btcusdt", _ctx())
    assert "btcusdt" in book.positions

    book.close_missing(set())          # 按 mark 平掉并记流水
    assert "btcusdt" not in book.positions

    book.tick("btcusdt", _ctx())       # 下一轮立刻按当下评估重开
    assert "btcusdt" in book.positions


def test_indicator_scope_includes_config_pool_not_just_display_filter():
    """前端页面全断开时，配置池里的币仍要 tick，否则平仓后不再重开。"""
    src = inspect.getsource(QuantEngine.refresh_indicators)
    assert "paper_pool" in src
    assert "syms.update(pool)" in src
    main_src = (REPO / "main.py").read_text(encoding="utf-8")
    assert "quant.paper_pool = set(symbols)" in main_src
