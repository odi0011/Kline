"""Central configuration for the quantitative decision layer.

All percentage-like values are stored as fractions (0.03 = 3%).  ``from_dict``
also accepts human-friendly values greater than 1 (3 = 3%) so YAML can use
``daily_loss_limit_pct: 3`` without changing the internal math.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _fraction(value: Any, default: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if abs(out) >= 1:
        out /= 100.0
    return max(0.0, out)


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _integer(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_utc_ms(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or "").strip()
    if not text:
        return 0
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


@dataclass(frozen=True)
class SessionWindow:
    """UTC time-of-day window. Windows crossing midnight are supported."""

    start_minute: int
    end_minute: int
    label: str = ""

    @classmethod
    def parse(cls, value: str) -> "SessionWindow":
        left, right = value.split("-", 1)
        sh, sm = [int(x) for x in left.strip().split(":", 1)]
        eh, em = [int(x) for x in right.strip().split(":", 1)]
        return cls(sh * 60 + sm, eh * 60 + em, value)

    def contains(self, minute_of_day: int) -> bool:
        if self.start_minute == self.end_minute:
            return True
        if self.start_minute < self.end_minute:
            return self.start_minute <= minute_of_day < self.end_minute
        return minute_of_day >= self.start_minute or minute_of_day < self.end_minute


@dataclass(frozen=True)
class MacroEventWindow:
    start_ms: int
    end_ms: int
    label: str = ""

    def contains(self, now_ms: int) -> bool:
        return self.start_ms <= now_ms < self.end_ms


@dataclass(frozen=True)
class DecisionConfig:
    # Feature switches
    enabled: bool = True
    risk_enabled: bool = True
    scoring_enabled: bool = True
    watch_enabled: bool = True
    maker_first: bool = False
    exchange_protection: bool = True

    # Signal scoring
    minimum_score: float = 55.0
    minimum_score_gap: float = 15.0
    strong_score: float = 75.0
    strong_score_gap: float = 20.0
    pattern_min_samples: int = 5
    pattern_min_winrate: float = 0.35
    pattern_winrate_alpha: float = 0.94
    pattern_window_days: int = 7

    # Account protections
    daily_loss_limit_pct: float = 0.03
    daily_profit_lock_pct: float = 0.05
    consecutive_loss_limit: int = 4
    consecutive_loss_cooldown_min: int = 60
    drawdown_limit_pct: float = 0.05
    drawdown_pause_min: int = 30

    # Position and portfolio limits
    max_positions: int = 4
    max_same_direction: int = 3
    cooldown_base_sec: int = 300
    cooldown_max_sec: int = 7200
    portfolio_heat_max_pct: float = 0.06
    correlation_threshold: float = 0.80
    correlation_lookback: int = 200

    # Sizing
    margin_pct_default: float = 0.05
    margin_pct_min: float = 0.02
    margin_pct_max: float = 0.08
    margin_min_usdt: float = 10.0
    risk_per_trade_pct: float = 0.01
    kelly_fraction: float = 0.25
    kelly_min_samples: int = 20
    high_vol_atr_pct: float = 85.0
    extreme_vol_atr_pct: float = 95.0
    high_vol_size_factor: float = 0.50

    # Execution protection
    max_notional_per_order: float = 60_000.0
    max_orders_per_min: int = 10
    maker_timeout_sec: int = 45
    maker_max_reposts: int = 1
    stale_trade_sec: float = 90.0
    stale_depth_sec: float = 15.0
    liquidation_window_sec: int = 300
    liquidation_min_events: int = 5
    liquidation_min_notional: float = 1_000_000.0
    liquidation_cooldown_sec: int = 900

    # Trend-following / exits
    trail_atr_mult: float = 2.0
    trail_atr_n: int = 22
    trail_arm_atr: float = 1.0
    watch_ema_fast: int = 20
    watch_ema_slow: int = 50
    watch_min_h1_rules: int = 2
    watch_sl_atr_mult: float = 1.5
    watch_tp_atr_mult: float = 2.5
    hmm_shadow: bool = True
    hmm_use_in_score: bool = False
    hmm_min_confidence: float = 0.60
    drift_enabled: bool = True
    drift_delta: float = 0.002
    drift_caution_hours: int = 72
    drift_shadow_hours: int = 48
    pool_audit_enabled: bool = True
    pool_min_quote_volume: float = 50_000_000.0
    pool_min_age_days: int = 30
    pool_min_daily_range: float = 0.015
    pool_max_daily_range: float = 0.08
    pool_max_spread: float = 0.0005
    pool_blacklist: tuple[str, ...] = ()

    # Time filters
    session_blackouts: tuple[SessionWindow, ...] = ()
    macro_events: tuple[MacroEventWindow, ...] = ()

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "DecisionConfig":
        d = raw or {}
        sessions = tuple(
            SessionWindow.parse(str(item))
            for item in (d.get("session_blackouts_utc") or [])
            if str(item).strip()
        )
        events = []
        for item in d.get("macro_events") or []:
            if not isinstance(item, dict):
                continue
            try:
                start = _parse_utc_ms(item.get("start"))
                end = _parse_utc_ms(item.get("end"))
            except (TypeError, ValueError):
                continue
            if start and end and end > start:
                events.append(MacroEventWindow(start, end, str(item.get("label") or "")))
        return cls(
            enabled=bool(d.get("enabled", True)),
            risk_enabled=bool(d.get("risk_enabled", True)),
            scoring_enabled=bool(d.get("scoring_enabled", True)),
            watch_enabled=bool(d.get("watch_enabled", True)),
            maker_first=bool(d.get("maker_first", False)),
            exchange_protection=bool(d.get("exchange_protection", True)),
            minimum_score=_number(d.get("minimum_score"), 55.0),
            minimum_score_gap=_number(d.get("minimum_score_gap"), 15.0),
            strong_score=_number(d.get("strong_score"), 75.0),
            strong_score_gap=_number(d.get("strong_score_gap"), 20.0),
            pattern_min_samples=_integer(d.get("pattern_min_samples"), 5),
            pattern_min_winrate=_fraction(d.get("pattern_min_winrate"), 0.35),
            pattern_winrate_alpha=_number(d.get("pattern_winrate_alpha"), 0.94),
            pattern_window_days=max(1, _integer(d.get("pattern_window_days"), 7)),
            daily_loss_limit_pct=_fraction(d.get("daily_loss_limit_pct"), 0.03),
            daily_profit_lock_pct=_fraction(d.get("daily_profit_lock_pct"), 0.05),
            consecutive_loss_limit=max(1, _integer(d.get("consecutive_loss_limit"), 4)),
            consecutive_loss_cooldown_min=max(1, _integer(d.get("consecutive_loss_cooldown_min"), 60)),
            drawdown_limit_pct=_fraction(d.get("drawdown_limit_pct"), 0.05),
            drawdown_pause_min=max(1, _integer(d.get("drawdown_pause_min"), 30)),
            max_positions=max(1, _integer(d.get("max_positions"), 4)),
            max_same_direction=max(1, _integer(d.get("max_same_direction"), 3)),
            cooldown_base_sec=max(0, _integer(d.get("cooldown_base_sec"), 300)),
            cooldown_max_sec=max(0, _integer(d.get("cooldown_max_sec"), 7200)),
            portfolio_heat_max_pct=_fraction(d.get("portfolio_heat_max_pct"), 0.06),
            correlation_threshold=min(0.999, _number(d.get("correlation_threshold"), 0.80)),
            correlation_lookback=max(20, _integer(d.get("correlation_lookback"), 200)),
            margin_pct_default=_fraction(d.get("margin_pct_default"), 0.05),
            margin_pct_min=_fraction(d.get("margin_pct_min"), 0.02),
            margin_pct_max=_fraction(d.get("margin_pct_max"), 0.08),
            margin_min_usdt=max(0.0, _number(d.get("margin_min_usdt"), 10.0)),
            risk_per_trade_pct=_fraction(d.get("risk_per_trade_pct"), 0.01),
            kelly_fraction=_fraction(d.get("kelly_fraction"), 0.25),
            kelly_min_samples=max(1, _integer(d.get("kelly_min_samples"), 20)),
            high_vol_atr_pct=_number(d.get("high_vol_atr_pct"), 85.0),
            extreme_vol_atr_pct=_number(d.get("extreme_vol_atr_pct"), 95.0),
            high_vol_size_factor=_fraction(d.get("high_vol_size_factor"), 0.50),
            max_notional_per_order=max(0.0, _number(d.get("max_notional_per_order"), 60_000.0)),
            max_orders_per_min=max(1, _integer(d.get("max_orders_per_min"), 10)),
            maker_timeout_sec=max(1, _integer(d.get("maker_timeout_sec"), 45)),
            maker_max_reposts=max(0, _integer(d.get("maker_max_reposts"), 1)),
            stale_trade_sec=max(1.0, _number(d.get("stale_trade_sec"), 90.0)),
            stale_depth_sec=max(1.0, _number(d.get("stale_depth_sec"), 15.0)),
            liquidation_window_sec=max(30, _integer(d.get("liquidation_window_sec"), 300)),
            liquidation_min_events=max(1, _integer(d.get("liquidation_min_events"), 5)),
            liquidation_min_notional=max(
                0.0, _number(d.get("liquidation_min_notional"), 1_000_000.0)),
            liquidation_cooldown_sec=max(
                0, _integer(d.get("liquidation_cooldown_sec"), 900)),
            trail_atr_mult=max(0.1, _number(d.get("trail_atr_mult"), 2.0)),
            trail_atr_n=max(2, _integer(d.get("trail_atr_n"), 22)),
            trail_arm_atr=max(0.1, _number(d.get("trail_arm_atr"), 1.0)),
            watch_ema_fast=max(2, _integer(d.get("watch_ema_fast"), 20)),
            watch_ema_slow=max(3, _integer(d.get("watch_ema_slow"), 50)),
            watch_min_h1_rules=max(1, _integer(d.get("watch_min_h1_rules"), 2)),
            watch_sl_atr_mult=max(0.1, _number(d.get("watch_sl_atr_mult"), 1.5)),
            watch_tp_atr_mult=max(0.1, _number(d.get("watch_tp_atr_mult"), 2.5)),
            hmm_shadow=bool(d.get("hmm_shadow", True)),
            hmm_use_in_score=bool(d.get("hmm_use_in_score", False)),
            hmm_min_confidence=_fraction(d.get("hmm_min_confidence"), 0.60),
            drift_enabled=bool(d.get("drift_enabled", True)),
            drift_delta=max(1e-6, _number(d.get("drift_delta"), 0.002)),
            drift_caution_hours=max(1, _integer(d.get("drift_caution_hours"), 72)),
            drift_shadow_hours=max(1, _integer(d.get("drift_shadow_hours"), 48)),
            pool_audit_enabled=bool(d.get("pool_audit_enabled", True)),
            pool_min_quote_volume=max(
                0.0, _number(d.get("pool_min_quote_volume"), 50_000_000.0)),
            pool_min_age_days=max(0, _integer(d.get("pool_min_age_days"), 30)),
            pool_min_daily_range=_fraction(d.get("pool_min_daily_range"), 0.015),
            pool_max_daily_range=_fraction(d.get("pool_max_daily_range"), 0.08),
            pool_max_spread=_fraction(d.get("pool_max_spread"), 0.0005),
            pool_blacklist=tuple(
                str(item).lower() for item in (d.get("pool_blacklist") or [])
                if str(item).strip()),
            session_blackouts=sessions,
            macro_events=tuple(events),
        )
