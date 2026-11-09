"""统一胜率/回撤口径的回归测试，锁住几个真实踩过的坑。"""
from decision.backtest import _metrics
from decision.metrics import decided_win_rate, equity_curve, max_drawdown
from decision.backtest import BacktestTrade


def _trade(pnl: float) -> BacktestTrade:
    return BacktestTrade(entry_ts=0, exit_ts=0, pattern="p", direction="bullish",
                         entry=1.0, exit=1.0, stop=0.9, target=1.1, qty=1.0,
                         pnl=pnl, return_pct=pnl, bars_held=1, reason="t")


def test_win_rate_excludes_flat_and_returns_none_without_sample():
    assert decided_win_rate(3, 2) == 60.0
    # 平价单不传进来；传 0 负也应按 decided 分母
    assert decided_win_rate(0, 4) == 0.0
    assert decided_win_rate(0, 0) is None


def test_win_rate_differs_from_all_trades_denominator():
    # 线上真实数字：3843 胜 / 3518 负 / 23219 平
    w, l, flat = 3843, 3518, 23219
    naive = w / (w + l + flat) * 100
    assert round(naive, 1) == 12.6
    assert decided_win_rate(w, l) == 52.21


def test_drawdown_counts_early_losses():
    """旧实现 peak 从 0 起算，开局连亏会被整段忽略。"""
    curve = equity_curve([-100.0, -100.0, 300.0, -50.0], base=10_000.0)
    d = max_drawdown(curve)
    # 10000 -> 9800 是 -2%，新实现必须算进去
    assert d["maxPct"] > 0
    assert abs(d["maxPct"] - 2.0) < 0.01, d
    # 10000 -> 9900 -> 9800 -> 10100 -> 10050
    assert d["peak"] == 10_100.0


def test_drawdown_returns_positive_pct_and_consistent_fields():
    curve = [10_000.0, 10_200.0, 9_940.0, 10_500.0, 10_100.0]
    d = max_drawdown(curve)
    assert d["maxPct"] == round(400 / 10_500 * 100, 4), d
    assert d["maxAbs"] == 400.0
    assert d["currentAbs"] == 400.0
    assert d["peak"] == 10_500.0
    assert d["points"] == 5


def test_drawdown_empty_and_flat_curve():
    assert max_drawdown([])["maxPct"] == 0.0
    assert max_drawdown([100.0, 100.0, 100.0])["maxPct"] == 0.0


def test_backtest_drawdown_matches_shared_implementation():
    """统一实现后，回测的负号字段数值不能漂移。"""
    pnls = [12.0, -30.0, 8.0, -45.0, 20.0, -5.0]
    old = _metrics([_trade(p) for p in pnls], 10_000.0)["maxDrawdownPct"]
    d = max_drawdown(equity_curve(pnls, 10_000.0))
    assert old == round(-d["maxPct"], 4), (old, d)
