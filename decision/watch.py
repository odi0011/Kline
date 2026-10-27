"""Multi-timeframe watch plans.

The entry signal remains the confirmed candlestick pattern.  This module adds a
4h directional gate, requires at least N independent 1h confirmations, and
turns an accepted signal into a concrete pre-trade plan (entry, stop, target,
risk-budgeted quantity).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from .config import DecisionConfig
from .features import atr_series, bollinger, clamp, ema_series, macd, sma


@dataclass
class WatchPlan:
    allowed: bool
    code: str
    reason: str
    direction: str
    entry: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    qty: float = 0.0
    notional: float = 0.0
    risk_amount: float = 0.0
    h4_direction: str = "neutral"
    h1_direction: str = "neutral"
    h1_rules: list[dict] = field(default_factory=list)
    score: float = 0.0
    metrics: dict[str, Any] = field(default_factory=dict)


def ema_direction(cs: Sequence[Any], fast_n: int, slow_n: int) -> dict:
    if len(cs) < slow_n + 3:
        return {"direction": "neutral", "strength": 0.0, "fast": None, "slow": None}
    closes = [float(bar.close) for bar in cs]
    fast = ema_series(closes, fast_n)
    slow = ema_series(closes, slow_n)
    atr = atr_series(cs, 14)
    distance = abs(closes[-1] - slow[-1]) / atr[-1] if atr and atr[-1] > 0 else 0.0
    slope = (fast[-1] - fast[-4]) / atr[-1] if atr and atr[-1] > 0 else 0.0
    if closes[-1] > fast[-1] > slow[-1] and slope > 0:
        direction = "bullish"
    elif closes[-1] < fast[-1] < slow[-1] and slope < 0:
        direction = "bearish"
    else:
        direction = "neutral"
    return {
        "direction": direction,
        "strength": round(clamp(distance), 4),
        "fast": fast[-1],
        "slow": slow[-1],
        "slopeAtr": round(slope, 4),
    }


def _h1_rules(cs: Sequence[Any], direction: str, config: DecisionConfig) -> list[dict]:
    if len(cs) < max(30, config.watch_ema_slow):
        return []
    closes = [float(bar.close) for bar in cs]
    fast = ema_series(closes, config.watch_ema_fast)
    slow = ema_series(closes, config.watch_ema_slow)
    macd_value = macd(cs)
    bb = bollinger(cs, 20, 2.0)
    last = cs[-1]
    volume = float(getattr(last, "volume", 0.0))
    avg_volume = sma([float(getattr(bar, "volume", 0.0)) for bar in cs[-21:-1]], 20) or 0.0
    volume_ratio = volume / avg_volume if avg_volume > 0 else 0.0
    delta = float(getattr(last, "buy_vol", 0.0)) - float(getattr(last, "sell_vol", 0.0))
    delta_ratio = delta / volume if volume > 0 else 0.0
    rsi_value = None
    try:
        from analysis.indicators import rsi as rsi_fn
        rsi_value = rsi_fn(cs, 14)
    except Exception:
        pass
    if direction == "bullish":
        conditions = [
            ("EMA20 above EMA50", fast[-1] > slow[-1]),
            ("price above EMA20", closes[-1] > fast[-1]),
            ("MACD hist positive", bool(macd_value and macd_value["hist"] > 0)),
            ("RSI above 50", bool(rsi_value is not None and 50 < rsi_value < 75)),
            ("price above BB mid", bool(bb and closes[-1] > bb["mid"])),
            ("volume >= 0.8x average", volume_ratio >= 0.8),
            ("active buy delta positive", delta_ratio > 0),
        ]
    else:
        conditions = [
            ("EMA20 below EMA50", fast[-1] < slow[-1]),
            ("price below EMA20", closes[-1] < fast[-1]),
            ("MACD hist negative", bool(macd_value and macd_value["hist"] < 0)),
            ("RSI below 50", bool(rsi_value is not None and 25 < rsi_value < 50)),
            ("price below BB mid", bool(bb and closes[-1] < bb["mid"])),
            ("volume >= 0.8x average", volume_ratio >= 0.8),
            ("active sell delta positive", delta_ratio < 0),
        ]
    return [{"name": name, "passed": bool(ok)} for name, ok in conditions]


def build_watch_plan(
    symbol: str,
    direction: str,
    entry_bars: Sequence[Any],
    h1_bars: Sequence[Any],
    h4_bars: Sequence[Any],
    equity: float,
    available: float,
    config: DecisionConfig,
    entry_price: float | None = None,
) -> WatchPlan:
    if direction not in ("bullish", "bearish"):
        return WatchPlan(False, "neutral_signal", "中性形态没有交易方向", direction)
    h4 = ema_direction(h4_bars, config.watch_ema_fast, config.watch_ema_slow)
    if h4["direction"] == "neutral":
        return WatchPlan(False, "h4_neutral", "4h EMA 未形成明确方向", direction,
                         h4_direction="neutral", metrics={"h4": h4})
    if h4["direction"] != direction:
        return WatchPlan(False, "h4_conflict",
                         f"4h 方向为 {h4['direction']}，与信号 {direction} 冲突",
                         direction, h4_direction=h4["direction"], metrics={"h4": h4})
    rules = _h1_rules(h1_bars, direction, config)
    passed_rules = [row for row in rules if row["passed"]]
    if len(passed_rules) < config.watch_min_h1_rules:
        return WatchPlan(
            False, "h1_rules", f"1h 仅通过 {len(passed_rules)} 条规则，"
            f"要求至少 {config.watch_min_h1_rules} 条", direction,
            h4_direction=h4["direction"], h1_direction=direction,
            h1_rules=rules, metrics={"h4": h4},
        )
    previous_close = float(entry_bars[-2].close) if len(entry_bars) >= 2 else None
    current_close = float(entry_bars[-1].close) if entry_bars else float(entry_price or 0)
    entry = float(entry_price or current_close)
    atr_values = atr_series(entry_bars or h1_bars, 14)
    atr = atr_values[-1] if atr_values else None
    if not entry or not atr:
        return WatchPlan(False, "atr_unavailable", "ATR 不足，无法生成交易计划", direction,
                         h4_direction=h4["direction"], h1_direction=direction,
                         h1_rules=rules, metrics={"h4": h4})
    sl_distance = atr * config.watch_sl_atr_mult
    tp_distance = atr * config.watch_tp_atr_mult
    if direction == "bullish":
        sl = entry - sl_distance
        tp = entry + tp_distance
    else:
        sl = entry + sl_distance
        tp = entry - tp_distance
    if sl <= 0 or tp <= 0:
        return WatchPlan(False, "invalid_plan", "计划价超出有效范围", direction,
                         h4_direction=h4["direction"], h1_direction=direction,
                         h1_rules=rules, metrics={"h4": h4})
    risk_amount = max(0.0, equity) * config.risk_per_trade_pct
    qty = risk_amount / sl_distance if sl_distance > 0 else 0.0
    capital_cap = max(0.0, available) * config.margin_pct_default / entry if entry > 0 else 0.0
    qty = min(qty, capital_cap)
    strength = clamp(0.35 + len(passed_rules) / len(rules) * 0.5 + h4["strength"] * 0.15)
    score = 50.0 + strength * 50.0
    return WatchPlan(
        True,
        "ok",
        "",
        direction,
        entry=round(entry, 10),
        sl=round(sl, 10),
        tp=round(tp, 10),
        qty=round(qty, 12),
        notional=round(qty * entry, 8),
        risk_amount=round(qty * sl_distance, 8),
        h4_direction=h4["direction"],
        h1_direction=direction,
        h1_rules=rules,
        score=round(clamp(score, 0, 100), 2),
        metrics={
            "h4": h4,
            "atr": atr,
            "passedH1Rules": len(passed_rules),
            "previousClose": previous_close,
            "currentClose": current_close,
            "slPct": round(sl_distance / entry * 100, 4),
            "tpPct": round(tp_distance / entry * 100, 4),
        },
    )
