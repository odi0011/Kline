"""模拟仓范围清理：展示勾选不是交易池，范围没变不能每 5 秒平一次仓。"""
from __future__ import annotations

from quant import QuantEngine


class FakePaper:
    def __init__(self):
        self.calls = 0

    def close_missing(self, keep):
        self.calls += 1
        return []


def make_engine() -> QuantEngine:
    eng = object.__new__(QuantEngine)
    eng.symbol_filter = {"btcusdt"}
    eng.paper = FakePaper()
    eng._event = lambda *a, **k: None
    return eng


def test_no_filter_does_nothing():
    eng = make_engine()
    eng.symbol_filter = None
    assert eng._settle_paper_out_of_scope() == []
    assert eng.paper.calls == 0


def test_cleanup_runs_once_per_scope_change():
    eng = make_engine()
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

    eng = make_engine()
    eng.paper = Broken()
    assert eng._settle_paper_out_of_scope() == []
    assert eng._settle_paper_out_of_scope() == []
    assert eng.paper.calls == 2
