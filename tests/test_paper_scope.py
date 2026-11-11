"""模拟仓范围清理：展示勾选不是交易池，默认绝不能平掉虚拟仓。

2026-11-11 实机事故：页面重连让 symbol_filter 在 5 币↔4 币间反复变化，
旧逻辑每次变化都 close_missing 平掉范围外的仓，叠加只做空后
paper_positions 被清成 0 行。设计契约是「每个监控币常驻一单、平仓即重开」。
"""
from __future__ import annotations

from decision.config import DecisionConfig
from quant import QuantEngine


class FakePaper:
    def __init__(self):
        self.calls = 0

    def close_missing(self, keep):
        self.calls += 1
        return []


def make_engine(cleanup: bool | None = None) -> QuantEngine:
    eng = object.__new__(QuantEngine)
    eng.symbol_filter = {"btcusdt"}
    eng.paper = FakePaper()
    eng._event = lambda *a, **k: None
    # None = 完全不注入 decision_cfg，模拟最朴素的单测/降级场景
    if cleanup is not None:
        eng.decision_cfg = DecisionConfig(paper_scope_cleanup=cleanup)
    return eng


def test_default_never_clears_positions():
    """默认配置：展示范围怎么变都不动账本。"""
    eng = make_engine()
    eng._settle_paper_out_of_scope()
    eng._settle_paper_out_of_scope()
    assert eng.paper.calls == 0

    eng.decision_cfg = DecisionConfig()      # 显式给默认配置也一样
    eng.symbol_filter = {"ethusdt"}
    eng._settle_paper_out_of_scope()
    assert eng.paper.calls == 0


def test_no_filter_does_nothing():
    eng = make_engine(cleanup=True)
    eng.symbol_filter = None
    assert eng._settle_paper_out_of_scope() == []
    assert eng.paper.calls == 0


def test_explicit_opt_in_runs_once_per_scope_change():
    """只有显式开启才清理，且同一范围只跑一次。"""
    eng = make_engine(cleanup=True)
    eng._settle_paper_out_of_scope()
    eng._settle_paper_out_of_scope()
    eng._settle_paper_out_of_scope()
    assert eng.paper.calls == 1

    eng.symbol_filter = {"ethusdt"}
    eng._settle_paper_out_of_scope()
    eng._settle_paper_out_of_scope()
    assert eng.paper.calls == 2


def test_failed_cleanup_can_retry():
    class Broken(FakePaper):
        def close_missing(self, keep):
            self.calls += 1
            raise RuntimeError("db down")

    eng = make_engine(cleanup=True)
    eng.paper = Broken()
    assert eng._settle_paper_out_of_scope() == []
    assert eng._settle_paper_out_of_scope() == []
    assert eng.paper.calls == 2
