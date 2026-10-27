"""Pure market-feature calculations used by the decision layer.

The functions accept Candle-like objects (attributes: open/high/low/close/
volume/buy_vol/sell_vol) and never mutate them.  They are deliberately free
of I/O so every decision feature can be replayed and unit-tested.
"""
from __future__ import annotations

import math
from statistics import pstdev
from typing import Any, Iterable, Sequence


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def ema_series(values: Sequence[float], n: int) -> list[float]:
    if not values or n <= 0:
        return []
    k = 2.0 / (n + 1)
    out = [float(values[0])]
    for value in values[1:]:
        out.append(float(value) * k + out[-1] * (1 - k))
    return out


def sma(values: Sequence[float], n: int) -> float | None:
    if n <= 0 or len(values) < n:
        return None
    return sum(float(v) for v in values[-n:]) / n


def atr_series(cs: Sequence[Any], n: int = 14) -> list[float]:
    if n <= 0 or len(cs) < n:
        return []
    trs = []
    prev_close = None
    for bar in cs:
        high = float(bar.high)
        low = float(bar.low)
        if prev_close is None:
            trs.append(high - low)
        else:
            trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        prev_close = float(bar.close)
    out = []
    seed = sum(trs[:n]) / n
    out.append(seed)
    for tr in trs[n:]:
        out.append((out[-1] * (n - 1) + tr) / n)
    return out


def macd(cs: Sequence[Any], fast: int = 12, slow: int = 26, signal: int = 9) -> dict | None:
    if len(cs) < slow + signal:
        return None
    closes = [float(bar.close) for bar in cs]
    fast_line = ema_series(closes, fast)
    slow_line = ema_series(closes, slow)
    diff = [fast_line[i] - slow_line[i] for i in range(len(closes))]
    signal_line = ema_series(diff, signal)
    hist = [diff[i] - signal_line[i] for i in range(len(diff))]
    price = closes[-1] or 1.0
    return {
        "macd": diff[-1],
        "signal": signal_line[-1],
        "hist": hist[-1],
        "histPrev": hist[-2],
        "histPct": hist[-1] / price,
        "rising": hist[-1] > hist[-2],
    }


def bollinger(cs: Sequence[Any], n: int = 20, mult: float = 2.0) -> dict | None:
    if len(cs) < n:
        return None
    closes = [float(bar.close) for bar in cs[-n:]]
    mid = sum(closes) / n
    if mid <= 0:
        return None
    sd = pstdev(closes)
    upper = mid + mult * sd
    lower = mid - mult * sd
    span = upper - lower
    return {
        "mid": mid,
        "upper": upper,
        "lower": lower,
        "std": sd,
        "bandwidth": span / mid,
        "pctB": (closes[-1] - lower) / span if span > 1e-12 else 0.5,
    }


def keltner(cs: Sequence[Any], ema_n: int = 20, atr_n: int = 10, mult: float = 1.5) -> dict | None:
    if len(cs) < max(ema_n, atr_n):
        return None
    closes = [float(bar.close) for bar in cs]
    ema = ema_series(closes, ema_n)[-1]
    atr = atr_series(cs, atr_n)
    if not atr:
        return None
    width = mult * atr[-1]
    return {
        "mid": ema,
        "upper": ema + width,
        "lower": ema - width,
        "atr": atr[-1],
    }


def squeeze_state(
    cs: Sequence[Any],
    bb_n: int = 20,
    bb_mult: float = 2.0,
    kc_n: int = 20,
    kc_atr_n: int = 10,
    kc_mult: float = 1.5,
    lookback: int = 160,
) -> dict:
    """TT squeeze detection plus release direction and duration.

    Squeeze is the Bollinger band sitting completely inside Keltner.  A
    release is the first bar where that containment ends.  Short releases are
    marked unreliable; callers can apply the minimum duration in scoring.
    """
    if len(cs) < max(bb_n, kc_n, kc_atr_n) + 1:
        return {"on": False, "release": False, "duration": 0, "direction": None}
    start = max(bb_n, kc_n, kc_atr_n, len(cs) - lookback)
    flags: list[bool] = []
    for end in range(start, len(cs) + 1):
        seg = cs[:end]
        bb = bollinger(seg, bb_n, bb_mult)
        kc = keltner(seg, kc_n, kc_atr_n, kc_mult)
        if not bb or not kc:
            flags.append(False)
            continue
        flags.append(bb["upper"] < kc["upper"] and bb["lower"] > kc["lower"])
    current = bool(flags[-1])
    duration = 0
    for flag in reversed(flags):
        if flag != current:
            break
        duration += 1
    release_bars = 0
    if not current:
        for i in range(len(flags) - 2, -1, -1):
            if flags[i]:
                release_bars = len(flags) - 1 - i
                break
    closes = [float(bar.close) for bar in cs]
    direction = None
    if closes[-1] > closes[-2]:
        direction = "bullish"
    elif closes[-1] < closes[-2]:
        direction = "bearish"
    return {
        "on": current,
        "release": (not current and 0 < release_bars <= 3),
        "releaseBarsAgo": release_bars or None,
        "duration": duration,
        "direction": direction,
    }


def cvd_metrics(cs: Sequence[Any], window: int = 12) -> dict:
    if len(cs) < window + 1:
        return {"cvd": 0.0, "delta": 0.0, "signal": None, "strength": 0.0}
    rows = cs[-(window + 1):]
    cvd_values = [0.0]
    for bar in rows[1:]:
        cvd_values.append(cvd_values[-1] + float(getattr(bar, "buy_vol", 0.0)) -
                          float(getattr(bar, "sell_vol", 0.0)))
    closes = [float(bar.close) for bar in rows]
    current_cvd = cvd_values[-1]
    prior_cvd = cvd_values[:-1]
    prior_closes = closes[:-1]
    max_close = max(prior_closes)
    min_close = min(prior_closes)
    max_cvd = max(prior_cvd)
    min_cvd = min(prior_cvd)
    signal = None
    if closes[-1] > max_close and current_cvd <= max_cvd:
        signal = "bearish"
    elif closes[-1] < min_close and current_cvd >= min_cvd:
        signal = "bullish"
    total_volume = sum(float(getattr(bar, "volume", 0.0)) for bar in rows[1:]) or 1.0
    delta = (current_cvd - prior_cvd[0]) / total_volume
    return {
        "cvd": current_cvd,
        "delta": delta,
        "deltaPct": delta * 100,
        "signal": signal,
        "strength": min(1.0, abs(delta) * 2.5),
    }


def vsa_metrics(cs: Sequence[Any], direction: str = "bullish", lookback: int = 20) -> dict:
    if not cs:
        return {"confirmed": False, "volumeRatio": 0.0, "deltaRatio": 0.0, "score": 50.0}
    last = cs[-1]
    previous = cs[-lookback - 1:-1]
    avg_volume = (sum(float(getattr(bar, "volume", 0.0)) for bar in previous) /
                  len(previous)) if previous else 0.0
    volume = float(getattr(last, "volume", 0.0))
    ratio = volume / avg_volume if avg_volume > 1e-12 else 0.0
    delta = float(getattr(last, "buy_vol", 0.0)) - float(getattr(last, "sell_vol", 0.0))
    delta_ratio = delta / volume if volume > 1e-12 else 0.0
    rng = float(last.high) - float(last.low)
    close_location = ((float(last.close) - float(last.low)) / rng) if rng > 1e-12 else 0.5
    directional_delta = delta_ratio if direction == "bullish" else -delta_ratio
    directional_close = close_location if direction == "bullish" else 1 - close_location
    score = 50.0
    score += 20.0 if ratio >= 0.8 else -20.0 if ratio < 0.55 else 0.0
    score += directional_delta * 50.0
    score += (directional_close - 0.5) * 40.0
    return {
        "confirmed": ratio >= 0.8 and directional_delta >= -0.1,
        "volumeRatio": ratio,
        "deltaRatio": delta_ratio,
        "closeLocation": close_location,
        "score": clamp(score, 0.0, 100.0),
    }


def order_book_imbalance(
    depth: dict | None,
    levels: int = 10,
    now_ms: int | None = None,
    max_age_ms: int = 2000,
) -> dict:
    if not depth:
        return {"valid": False, "ratio": 1.0, "bias": 0.0, "bid": 0.0, "ask": 0.0}
    ts = int(depth.get("ts") or 0)
    if now_ms is not None and ts and now_ms - ts > max_age_ms:
        return {"valid": False, "ratio": 1.0, "bias": 0.0, "bid": 0.0, "ask": 0.0}
    bids = depth.get("bids") or []
    asks = depth.get("asks") or []
    bid = sum(float(row[1]) for row in bids[:levels] if len(row) >= 2)
    ask = sum(float(row[1]) for row in asks[:levels] if len(row) >= 2)
    total = bid + ask
    ratio = bid / ask if ask > 1e-12 else (999.0 if bid > 0 else 1.0)
    bias = (bid - ask) / total if total > 1e-12 else 0.0
    return {"valid": bool(total > 0), "ratio": ratio, "bias": bias, "bid": bid, "ask": ask}


def open_interest_bias(oi_change_pct: float, price_change_pct: float, strength_scale: float = 10.0) -> dict:
    """Classic price/OI four-quadrant interpretation."""
    if price_change_pct > 0 and oi_change_pct > 0:
        bias, label = "bullish", "price up + OI up"
    elif price_change_pct < 0 and oi_change_pct > 0:
        bias, label = "bearish", "price down + OI up"
    elif price_change_pct > 0 and oi_change_pct < 0:
        bias, label = "bearish", "short covering"
    elif price_change_pct < 0 and oi_change_pct < 0:
        bias, label = "bullish", "long liquidation"
    else:
        bias, label = "neutral", "flat"
    strength = clamp((abs(oi_change_pct) + abs(price_change_pct)) / max(strength_scale, 1e-9))
    return {"bias": bias, "label": label, "strength": strength}


def funding_context(rate: float | None, next_time_ms: int | None, side: str,
                    now_ms: int, block_minutes: int = 10,
                    min_abs_rate: float = 0.0001) -> dict:
    rate = float(rate or 0.0)
    next_time_ms = int(next_time_ms or 0)
    minutes = (next_time_ms - now_ms) / 60_000 if next_time_ms else None
    adverse = (side.upper() == "BUY" and rate > 0) or (side.upper() == "SELL" and rate < 0)
    blocked = bool(minutes is not None and 0 <= minutes <= block_minutes and
                   abs(rate) > min_abs_rate and adverse)
    return {
        "rate": rate,
        "minutesToSettlement": round(minutes, 2) if minutes is not None else None,
        "adverse": adverse,
        "blocked": blocked,
    }


def returns(cs: Sequence[Any], n: int = 100) -> list[float]:
    closes = [float(bar.close) for bar in cs[-(n + 1):]]
    out = []
    for left, right in zip(closes, closes[1:]):
        if left > 0 and right > 0:
            out.append(math.log(right / left))
    return out


def correlation(left: Sequence[float], right: Sequence[float]) -> float:
    n = min(len(left), len(right))
    if n < 10:
        return 0.0
    a = list(left[-n:])
    b = list(right[-n:])
    ma = sum(a) / n
    mb = sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (n - 1)
    va = sum((x - ma) ** 2 for x in a) / (n - 1)
    vb = sum((y - mb) ** 2 for y in b) / (n - 1)
    if va <= 1e-18 or vb <= 1e-18:
        return 0.0
    return cov / math.sqrt(va * vb)


def session_minute(now_ms: int) -> int:
    seconds = int(now_ms // 1000) % 86400
    return seconds // 60


def layered_slippage_bps(notional: float, avg_volume_notional: float) -> float:
    """Scale slippage with order size relative to recent average volume."""
    if avg_volume_notional <= 0 or notional <= 0:
        return 0.0
    ratio = notional / avg_volume_notional
    if ratio < 0.05:
        return 1.0
    if ratio < 0.10:
        return 3.0
    return 10.0
