"""Confluence scoring for pattern candidates.

The score is explanatory rather than a black box: Trend, Momentum, Volume and
Structure each contribute a bull and bear score (0..100), weighted by the
current regime.  The selected pattern direction must clear both an absolute
score and a separation-from-opposite score.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import DecisionConfig
from .features import clamp


REGIME_WEIGHTS = {
    "trend": {"trend": 1.25, "momentum": 0.95, "volume": 0.65, "structure": 1.0},
    "range": {"trend": 0.60, "momentum": 1.0, "volume": 1.2, "structure": 1.2},
    "mixed": {"trend": 1.0, "momentum": 0.95, "volume": 0.9, "structure": 1.0},
    "unknown": {"trend": 0.8, "momentum": 0.9, "volume": 1.0, "structure": 1.0},
}


@dataclass
class SignalScore:
    allowed: bool
    direction: str
    score: float
    opposite_score: float
    gap: float
    grade: str
    confidence: float
    breakdown: dict[str, Any] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    vetoes: list[str] = field(default_factory=list)


def _trend_scores(state: dict, features: dict) -> tuple[float, float]:
    bull = bear = 50.0
    pdi = state.get("plusDI")
    mdi = state.get("minusDI")
    if pdi is not None and mdi is not None:
        diff = clamp((float(pdi) - float(mdi)) / 20.0, -1.5, 1.5)
        bull += diff * 20.0
        bear -= diff * 20.0
    adx = state.get("adx")
    if adx is not None:
        strength = clamp((float(adx) - 15.0) / 25.0) * 8.0
        if pdi is not None and mdi is not None:
            bull += strength if pdi > mdi else 0.0
            bear += strength if mdi > pdi else 0.0
    ema_fast = features.get("emaFast")
    ema_slow = features.get("emaSlow")
    ema_macro = features.get("emaMacro")
    close = features.get("close")
    if None not in (ema_fast, ema_slow, close):
        bull += 7.0 if close > ema_fast > ema_slow else 0.0
        bear += 7.0 if close < ema_fast < ema_slow else 0.0
    if None not in (ema_slow, ema_macro):
        bull += 6.0 if ema_slow > ema_macro else 0.0
        bear += 6.0 if ema_slow < ema_macro else 0.0
    regime = state.get("regime")
    if regime == "trend":
        bull += 4.0 if bull > bear else -4.0
        bear += 4.0 if bear > bull else -4.0
    elif regime == "range":
        bull -= 5.0
        bear -= 5.0
    return clamp(bull, 0.0, 100.0), clamp(bear, 0.0, 100.0)


def _momentum_scores(state: dict, features: dict) -> tuple[float, float]:
    bull = bear = 50.0
    rsi = state.get("rsi")
    if rsi is not None:
        bull += (float(rsi) - 50.0) * 1.7
        bear -= (float(rsi) - 50.0) * 1.7
    z = state.get("z")
    if z is not None:
        bull -= max(0.0, abs(float(z)) - 1.5) * 6.0
        bear -= max(0.0, abs(float(z)) - 1.5) * 6.0
    macd = features.get("macd") or {}
    hist = macd.get("hist")
    if hist is not None:
        if hist > 0:
            bull += 12.0
            bear -= 12.0
        elif hist < 0:
            bear += 12.0
            bull -= 12.0
        if macd.get("rising") and hist > 0:
            bull += 5.0
        elif macd.get("rising") and hist < 0:
            bear += 5.0
    roc = state.get("roc")
    if roc is not None:
        roc_score = clamp(float(roc) / 0.01, -1.0, 1.0) * 10.0
        bull += roc_score
        bear -= roc_score
    return clamp(bull, 0.0, 100.0), clamp(bear, 0.0, 100.0)


def _volume_scores(features: dict) -> tuple[float, float]:
    bull_vsa = features.get("vsaBull") or {}
    bear_vsa = features.get("vsaBear") or {}
    bull = float(bull_vsa.get("score", 50.0))
    bear = float(bear_vsa.get("score", 50.0))
    cvd = features.get("cvd") or {}
    signal = cvd.get("signal")
    strength = float(cvd.get("strength") or 0.0) * 12.0
    if signal == "bullish":
        bull += strength
        bear -= strength
    elif signal == "bearish":
        bear += strength
        bull -= strength
    return clamp(bull, 0.0, 100.0), clamp(bear, 0.0, 100.0)


def _structure_scores(state: dict, features: dict, pattern_stats: dict | None,
                      pattern: str | None) -> tuple[float, float]:
    bull = bear = 50.0
    obi = features.get("obi") or {}
    bias = float(obi.get("bias") or 0.0) if obi.get("valid") else 0.0
    bull += bias * 250.0
    bear -= bias * 250.0
    oi = features.get("oi") or {}
    oi_bias = oi.get("bias")
    oi_strength = float(oi.get("strength") or 0.0) * 12.0
    if oi_bias == "bullish":
        bull += oi_strength
        bear -= oi_strength
    elif oi_bias == "bearish":
        bear += oi_strength
        bull -= oi_strength
    squeeze = features.get("squeeze") or {}
    if squeeze.get("on"):
        bull -= 8.0
        bear -= 8.0
    if squeeze.get("release") and int(squeeze.get("duration") or 0) >= 6:
        direction = squeeze.get("direction")
        if direction == "bullish":
            bull += 8.0
            bear -= 5.0
        elif direction == "bearish":
            bear += 8.0
            bull -= 5.0
    htf = features.get("htf") or {}
    htf_direction = htf.get("direction")
    htf_strength = clamp(float(htf.get("strength") or 1.0)) * 12.0
    if htf_direction == "bullish":
        bull += htf_strength
        bear -= htf_strength
    elif htf_direction == "bearish":
        bear += htf_strength
        bull -= htf_strength
    sentiment = features.get("sentiment") or {}
    sentiment_score = float(sentiment.get("score") or 0.0)
    sentiment_confidence = clamp(float(sentiment.get("confidence") or 0.0))
    bull += sentiment_score * 10.0 * sentiment_confidence
    bear -= sentiment_score * 10.0 * sentiment_confidence
    stats = (pattern_stats or {}).get(pattern or "") or {}
    wins, losses = int(stats.get("wins") or 0), int(stats.get("losses") or 0)
    if wins + losses >= 5:
        weighted_winrate = stats.get("weightedWinrate")
        edge = ((float(weighted_winrate) * 2 - 1) if weighted_winrate is not None
                else (wins - losses) / (wins + losses))
        sample_count = float(stats.get("effectiveSamples") or (wins + losses))
        confidence = clamp(sample_count / 20.0)
        bull += edge * 14.0 * confidence
        bear -= edge * 14.0 * confidence
    return clamp(bull, 0.0, 100.0), clamp(bear, 0.0, 100.0)


def score_signal(
    direction: str,
    state: dict,
    features: dict,
    config: DecisionConfig,
    pattern: str | None = None,
    pattern_stats: dict | None = None,
) -> SignalScore:
    if direction not in ("bullish", "bearish"):
        return SignalScore(False, direction, 0, 0, 0, "none", 0.0,
                           vetoes=["中性形态没有交易方向"])
    trend_bull, trend_bear = _trend_scores(state, features)
    mom_bull, mom_bear = _momentum_scores(state, features)
    vol_bull, vol_bear = _volume_scores(features)
    struct_bull, struct_bear = _structure_scores(state, features, pattern_stats, pattern)
    regime = state.get("regime") if state.get("regime") in REGIME_WEIGHTS else "unknown"
    weights = REGIME_WEIGHTS[regime]
    total_weight = sum(weights.values())
    bull_score = (
        trend_bull * weights["trend"] + mom_bull * weights["momentum"] +
        vol_bull * weights["volume"] + struct_bull * weights["structure"]
    ) / total_weight
    bear_score = (
        trend_bear * weights["trend"] + mom_bear * weights["momentum"] +
        vol_bear * weights["volume"] + struct_bear * weights["structure"]
    ) / total_weight
    selected = bull_score if direction == "bullish" else bear_score
    opposite = bear_score if direction == "bullish" else bull_score
    gap = selected - opposite
    vetoes: list[str] = []
    if pattern_stats and pattern:
        stats = pattern_stats.get(pattern) or {}
        wins = int(stats.get("wins") or 0)
        losses = int(stats.get("losses") or 0)
        effective_samples = float(stats.get("effectiveSamples") or (wins + losses))
        if effective_samples >= config.pattern_min_samples:
            winrate = float(stats.get("weightedWinrate") if stats.get("weightedWinrate") is not None
                            else wins / (wins + losses))
            if winrate < config.pattern_min_winrate:
                vetoes.append(
                    f"形态滚动胜率 {winrate * 100:.0f}% 低于 "
                    f"{config.pattern_min_winrate * 100:.0f}%")
    funding = features.get("funding") or {}
    if funding.get("blocked"):
        vetoes.append("资金费率结算前方向不利，暂缓开仓")
    if selected < config.minimum_score:
        vetoes.append(f"共鸣分 {selected:.0f} < {config.minimum_score:.0f}")
    if gap < config.minimum_score_gap:
        vetoes.append(f"多空分离度 {gap:.0f} < {config.minimum_score_gap:.0f}")
    if selected >= config.strong_score and gap >= config.strong_score_gap:
        grade = "strong"
    elif selected >= config.minimum_score and gap >= config.minimum_score_gap:
        grade = "medium"
    elif selected >= 40:
        grade = "weak"
    else:
        grade = "none"
    reasons = [
        f"Trend {trend_bull if direction == 'bullish' else trend_bear:.0f}",
        f"Momentum {mom_bull if direction == 'bullish' else mom_bear:.0f}",
        f"Volume {vol_bull if direction == 'bullish' else vol_bear:.0f}",
        f"Structure {struct_bull if direction == 'bullish' else struct_bear:.0f}",
    ]
    return SignalScore(
        allowed=not vetoes,
        direction=direction,
        score=round(selected, 2),
        opposite_score=round(opposite, 2),
        gap=round(gap, 2),
        grade=grade,
        confidence=round(clamp(selected / 100.0), 4),
        breakdown={
            "regime": regime,
            "weights": dict(weights),
            "hmm": features.get("hmm"),
            "bull": {
                "trend": round(trend_bull, 2),
                "momentum": round(mom_bull, 2),
                "volume": round(vol_bull, 2),
                "structure": round(struct_bull, 2),
                "total": round(bull_score, 2),
            },
            "bear": {
                "trend": round(trend_bear, 2),
                "momentum": round(mom_bear, 2),
                "volume": round(vol_bear, 2),
                "structure": round(struct_bear, 2),
                "total": round(bear_score, 2),
            },
        },
        reasons=reasons,
        vetoes=vetoes,
    )
