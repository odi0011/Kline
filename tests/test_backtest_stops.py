"""回测引擎可复现生产止损规则（默认关闭，保持历史基线可比）。

生产 watch_plan 把止损夹到 [0.4%, 2.0%] 并保证 rr>=3.0；回测原来直接
用 ATR×倍数，短周期 ATR 相对价格很小时会把止损压到 0.1% 档，结论偏悲观。
"""
from types import SimpleNamespace

from decision.backtest import run_backtest
from decision.config import DecisionConfig


class AlwaysHit:
    key = "probe"
    min_bars = 3
    priority = 1
    direction = "bullish"

    def check(self, window):
        return True


def _bars(n: int = 140):
    """极小 ATR 的横盘，ATR×1.5 远小于 0.4% 止损下限。"""
    out = []
    for i in range(n):
        o = 100.0 + (i % 2) * 0.002
        out.append(SimpleNamespace(open_time=i * 60_000, open=o,
                                   high=o + 0.003, low=o - 0.003,
                                   close=o, volume=1.0, interval="3m"))
    return out


def _first_trade(**kw):
    res = run_backtest(_bars(), [AlwaysHit()], DecisionConfig(), **kw)
    assert res.trades, "合成数据应至少产生一笔交易"
    return res.trades[0]


def test_defaults_keep_legacy_atr_only_stops():
    t = _first_trade()
    assert (t.entry - t.stop) < 100 * 0.004, "默认不夹紧，止损应远小于 0.4%"


def test_stop_min_pct_enforces_floor():
    t = _first_trade(stop_min_pct=0.004)
    assert (t.entry - t.stop) >= 100 * 0.004 * 0.999


def test_min_rr_widens_target_relative_to_stop():
    t = _first_trade(stop_min_pct=0.004, min_rr=3.0)
    sl = t.entry - t.stop
    tp = t.target - t.entry
    assert tp >= sl * 3.0 * 0.999, (tp, sl)


def test_both_switches_are_opt_in():
    a = _first_trade()
    b = _first_trade(stop_min_pct=0.0, min_rr=0.0)
    assert a.stop == b.stop and a.target == b.target
