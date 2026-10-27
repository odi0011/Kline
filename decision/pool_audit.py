"""Advisory universe audit. Never changes the live pool automatically."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Sequence

from .config import DecisionConfig


def _daily_ranges(bars: Sequence[Any]) -> list[float]:
    days = defaultdict(lambda: [float("inf"), 0.0, 0.0, 0.0])
    for bar in bars:
        day = datetime.fromtimestamp(
            int(getattr(bar, "open_time", 0)) / 1000, tz=timezone.utc
        ).date().isoformat()
        high, low = float(bar.high), float(bar.low)
        row = days[day]
        row[0] = min(row[0], low)
        row[1] = max(row[1], high)
        row[2] = float(bar.close)
        row[3] += float(getattr(bar, "volume", 0.0))
    out = []
    for low, high, close, _volume in days.values():
        if close > 0:
            out.append((high - low) / close)
    return out


def audit_symbol(
    symbol: str,
    config: DecisionConfig,
    *,
    bars: Sequence[Any] | None = None,
    depth: dict | None = None,
    quote_volume: float | None = None,
    onboard_ms: int | None = None,
    now_ms: int,
) -> dict:
    reasons = []
    metrics = {}
    if symbol.lower() in config.pool_blacklist:
        reasons.append("manual blacklist")
    bars = bars or []
    if quote_volume is None:
        quote_volume = sum(
            float(getattr(bar, "close", 0.0)) * float(getattr(bar, "volume", 0.0))
            for bar in bars[-480:]
        )
    metrics["quoteVolume24h"] = round(float(quote_volume or 0), 2)
    if quote_volume and quote_volume < config.pool_min_quote_volume:
        reasons.append("24h quote volume below minimum")
    if onboard_ms:
        age_days = (now_ms - int(onboard_ms)) / 86_400_000
        metrics["ageDays"] = round(age_days, 2)
        if age_days < config.pool_min_age_days:
            reasons.append("listed too recently")
    ranges = _daily_ranges(bars)
    if ranges:
        avg_range = sum(ranges) / len(ranges)
        metrics["avgDailyRangePct"] = round(avg_range * 100, 4)
        if avg_range < config.pool_min_daily_range:
            reasons.append("daily range too low")
        elif avg_range > config.pool_max_daily_range:
            reasons.append("daily range too high")
    if depth:
        bids = depth.get("bids") or []
        asks = depth.get("asks") or []
        if bids and asks:
            bid, ask = float(bids[0][0]), float(asks[0][0])
            mid = (bid + ask) / 2
            spread = (ask - bid) / mid if mid > 0 else 0.0
            metrics["spreadPct"] = round(spread * 100, 5)
            if spread > config.pool_max_spread:
                reasons.append("spread too wide")
    return {
        "symbol": symbol.lower(),
        "status": "downgrade" if reasons else "ok",
        "reasons": reasons,
        "metrics": metrics,
        "shadowDays": 1,
    }


def audit_universe(
    symbols: Sequence[str],
    config: DecisionConfig,
    *,
    bars_by_symbol: dict[str, Sequence[Any]],
    depth_by_symbol: dict[str, dict],
    tickers: dict[str, dict],
    onboard_by_symbol: dict[str, int],
    now_ms: int,
) -> dict:
    rows = []
    for symbol in symbols:
        ticker = tickers.get(symbol) or {}
        rows.append(audit_symbol(
            symbol, config,
            bars=bars_by_symbol.get(symbol) or [],
            depth=depth_by_symbol.get(symbol),
            quote_volume=ticker.get("quoteVolume"),
            onboard_ms=onboard_by_symbol.get(symbol),
            now_ms=now_ms,
        ))
    return {
        "ts": now_ms,
        "mode": "advisory",
        "ok": [row for row in rows if row["status"] == "ok"],
        "downgrade": [row for row in rows if row["status"] != "ok"],
    }
