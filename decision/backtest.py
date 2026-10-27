"""Event-driven shadow backtest and Monte Carlo diagnostics.

Rules:
* A signal on bar T is executed at bar T+1 open.
* When stop and target are both touched in one bar, stop wins (worst case).
* Gaps through a stop fill at the opening price, not the stop price.
* Fees and slippage are applied on both legs.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from statistics import NormalDist
from typing import Any, Callable, Sequence

from analysis import indicators as IND
from .config import DecisionConfig
from .features import atr_series, clamp


@dataclass
class BacktestTrade:
    entry_ts: int
    exit_ts: int
    pattern: str
    direction: str
    entry: float
    exit: float
    stop: float
    target: float
    qty: float
    pnl: float
    return_pct: float
    bars_held: int
    reason: str


@dataclass
class BacktestResult:
    trades: list[BacktestTrade]
    metrics: dict[str, Any] = field(default_factory=dict)
    initial_equity: float = 10_000.0
    final_equity: float = 10_000.0


def _metrics(trades: list[BacktestTrade], initial_equity: float) -> dict:
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl < 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)
    equity = initial_equity
    peak = equity
    max_dd = 0.0
    for trade in trades:
        equity += trade.pnl
        peak = max(peak, equity)
        if peak > 0:
            max_dd = min(max_dd, (equity - peak) / peak)
    expectancy = sum(t.pnl for t in trades) / len(trades) if trades else 0.0
    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "winRate": round(len(wins) / len(trades), 4) if trades else 0.0,
        "pnl": round(sum(t.pnl for t in trades), 4),
        "expectancy": round(expectancy, 4),
        "profitFactor": round(gross_win / gross_loss, 4) if gross_loss > 0 else None,
        "maxDrawdownPct": round(max_dd * 100, 4),
        "avgHoldBars": round(sum(t.bars_held for t in trades) / len(trades), 2) if trades else 0.0,
        "avgReturnPct": round(sum(t.return_pct for t in trades) / len(trades), 4) if trades else 0.0,
    }


def run_backtest(
    bars: Sequence[Any],
    patterns: Sequence[Any],
    config: DecisionConfig | None = None,
    *,
    symbol: str = "",
    initial_equity: float = 10_000.0,
    risk_pct: float = 0.01,
    fee_bps: float = 4.0,
    slippage_bps: float = 1.0,
    max_hold_bars: int = 20,
    warmup: int = 40,
    signal_filter: Callable[[Sequence[Any], int, Any], bool] | None = None,
) -> BacktestResult:
    cfg = config or DecisionConfig()
    trades: list[BacktestTrade] = []
    equity = initial_equity
    if len(bars) <= warmup + 2 or not patterns:
        return BacktestResult(trades, _metrics(trades, initial_equity),
                              initial_equity, initial_equity)
    i = warmup - 1
    while i < len(bars) - 2:
        window = bars[:i + 1]
        candidates = []
        for pattern in patterns:
            try:
                if len(window) >= pattern.min_bars + 3 and pattern.check(window):
                    candidates.append(pattern)
            except Exception:
                continue
        if not candidates:
            i += 1
            continue
        best = max(candidates, key=lambda p: (p.priority, p.min_bars))
        if best.direction not in ("bullish", "bearish"):
            i += 1
            continue
        if signal_filter and not signal_filter(window, i, best):
            i += 1
            continue
        atr_values = atr_series(window, 14)
        if not atr_values:
            i += 1
            continue
        atr = atr_values[-1]
        direction = best.direction
        side_sign = 1 if direction == "bullish" else -1
        raw_entry = float(bars[i + 1].open)
        entry = raw_entry * (1 + side_sign * slippage_bps / 10_000)
        stop_distance = atr * cfg.watch_sl_atr_mult
        target_distance = atr * cfg.watch_tp_atr_mult
        stop = entry - side_sign * stop_distance
        target = entry + side_sign * target_distance
        if stop <= 0 or target <= 0:
            i += 1
            continue
        risk_budget = equity * risk_pct
        qty = risk_budget / stop_distance if stop_distance > 0 else 0.0
        qty = min(qty, equity * cfg.margin_pct_default / entry)
        if qty <= 0:
            i += 1
            continue
        exit_price = None
        exit_index = None
        reason = ""
        for j in range(i + 1, min(len(bars), i + 1 + max_hold_bars)):
            bar = bars[j]
            if direction == "bullish":
                if float(bar.open) <= stop:
                    exit_price, reason = float(bar.open), "gap_stop"
                elif float(bar.low) <= stop:
                    exit_price, reason = stop, "stop"
                elif float(bar.high) >= target:
                    exit_price, reason = target, "target"
            else:
                if float(bar.open) >= stop:
                    exit_price, reason = float(bar.open), "gap_stop"
                elif float(bar.high) >= stop:
                    exit_price, reason = stop, "stop"
                elif float(bar.low) <= target:
                    exit_price, reason = target, "target"
            if exit_price is not None:
                exit_index = j
                break
        if exit_price is None:
            exit_index = min(len(bars) - 1, i + max_hold_bars)
            exit_price = float(bars[exit_index].close)
            reason = "timeout"
        exit_price *= (1 - side_sign * slippage_bps / 10_000)
        gross = (exit_price - entry) * qty * side_sign
        fees = (entry + exit_price) * qty * fee_bps / 10_000
        pnl = gross - fees
        equity += pnl
        trades.append(BacktestTrade(
            entry_ts=int(getattr(bars[i + 1], "open_time", i + 1)),
            exit_ts=int(getattr(bars[exit_index], "open_time", exit_index)),
            pattern=best.key, direction=direction,
            entry=round(entry, 10), exit=round(exit_price, 10),
            stop=round(stop, 10), target=round(target, 10),
            qty=round(qty, 12), pnl=round(pnl, 6),
            return_pct=round(pnl / max(equity - pnl, 1e-9) * 100, 4),
            bars_held=exit_index - i,
            reason=reason,
        ))
        i = exit_index + 1
    return BacktestResult(trades, _metrics(trades, initial_equity),
                          initial_equity, equity)


def monte_carlo(
    pnls: Sequence[float],
    *,
    iterations: int = 1000,
    initial_equity: float = 10_000.0,
    ruin_drawdown_pct: float = 0.20,
    seed: int = 7,
) -> dict:
    values = [float(p) for p in pnls]
    if not values:
        return {"iterations": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "ruinProbability": 0.0}
    rng = random.Random(seed)
    draws = []
    ruined = 0
    for _ in range(max(1, iterations)):
        path = values[:]
        rng.shuffle(path)
        equity = initial_equity
        peak = equity
        worst = 0.0
        is_ruined = False
        for pnl in path:
            equity += pnl
            peak = max(peak, equity)
            if peak > 0:
                dd = (equity - peak) / peak
                worst = min(worst, dd)
                if dd <= -ruin_drawdown_pct:
                    is_ruined = True
        if is_ruined:
            ruined += 1
        draws.append(abs(worst) * 100)
    draws.sort()

    def pct(q: float) -> float:
        idx = min(len(draws) - 1, max(0, int(math.ceil(q * len(draws))) - 1))
        return round(draws[idx], 4)

    return {
        "iterations": len(draws),
        "p50": pct(0.50),
        "p95": pct(0.95),
        "p99": pct(0.99),
        "ruinProbability": round(ruined / len(draws), 6),
    }


def random_entry_lift(
    real_pnls: Sequence[float],
    control_pnls: Sequence[float],
) -> dict:
    if not real_pnls or not control_pnls:
        return {"lift": 0.0, "realExpectancy": 0.0, "controlExpectancy": 0.0}
    real = sum(real_pnls) / len(real_pnls)
    control = sum(control_pnls) / len(control_pnls)
    return {
        "realExpectancy": round(real, 6),
        "controlExpectancy": round(control, 6),
        "lift": round(real - control, 6),
    }


def deflated_sharpe_ratio(
    observed_sr: float,
    trial_srs: Sequence[float],
    observations: int,
    *,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> float:
    """Bailey/López de Prado deflated Sharpe ratio, simplified but honest."""
    if observations <= 1 or not trial_srs:
        return 0.0
    values = [float(value) for value in trial_srs]
    n = len(values)
    mean = sum(values) / n
    variance = sum((value - mean) ** 2 for value in values) / max(1, n - 1)
    std = math.sqrt(max(variance, 1e-18))
    normal = NormalDist()
    if std <= 1e-12:
        return 1.0 if observed_sr > mean else 0.0
    gamma = mean / (std * math.sqrt(max(observations - 1, 1)))
    sr0 = std * (
        (1 - gamma) * normal.inv_cdf(1 - 1 / n) +
        gamma * normal.inv_cdf(1 - 1 / (n * math.e))
    )
    denom = math.sqrt(max(
        1e-18,
        1 - skew * observed_sr + (kurtosis - 1) / 4 * observed_sr ** 2,
    ))
    z = (observed_sr - sr0) * math.sqrt(observations - 1) / denom
    return max(0.0, min(1.0, normal.cdf(z)))
