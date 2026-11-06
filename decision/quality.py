"""信号质量门：震荡 / 噪声 / 假信号三层统一处理。

依据 scripts/signal_quality.py 的线上回放（1904 笔、动态止损 0.4~2.0%、
rr 3.0、扣双边 0.1%）。可回放样本实际跨度 3.5 天，按时间前后半做样本外
验证，**没有任何组合能稳定转正**。所以这个模块的目标不是"让策略赚钱"，
而是把两段里都稳定更差的条件剔掉/降仓，少付手续费、少被噪声扫损。

两段都稳定为负（硬拒）：
  · UTC 12-18 时段        前半 -0.190 / 后半 -0.267 %每笔
  · 高影线噪声 noise>=q75 前半 -0.139 / 后半 -0.157
  · 形态 morning_star / hammer / bullish_harami / tweezer_bottom
两段都稳定偏弱（降仓，不硬拒）：
  · regime=trend          -0.131 / -0.109（range 是 -0.072 / -0.068，最好）
  · score>=55 / gap>=30   -0.215 / -0.134、-0.190 / -0.175（评分反向）
  · 极端波动分位 <15 或 >85
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from .config import DecisionConfig, SessionWindow


@dataclass
class QualityVerdict:
    allowed: bool
    size_factor: float = 1.0
    reasons: list[str] = field(default_factory=list)
    tags: dict[str, Any] = field(default_factory=dict)


def noise_ratio(bars: Iterable[Any], window: int = 30) -> float | None:
    """最近 window 根 K 线的影线占比：1 - Σ|close-open| / Σ(high-low)。

    越大说明来回插针、实体越小，越容易被止损扫掉。回放里 q33≈0.367、
    q66≈0.433、q75≈0.451（BTC 1m）。
    """
    seq = list(bars or [])[-window:]
    if len(seq) < 5:
        return None
    total_range = 0.0
    total_body = 0.0
    for bar in seq:
        try:
            high = float(getattr(bar, "high", 0) or 0)
            low = float(getattr(bar, "low", 0) or 0)
            close = float(getattr(bar, "close", 0) or 0)
            open_ = float(getattr(bar, "open", 0) or 0)
        except (TypeError, ValueError):
            continue
        total_range += max(high - low, 1e-12)
        total_body += abs(close - open_)
    if total_range <= 0:
        return None
    return 1 - total_body / total_range


def _in_window(minute: int, windows: tuple[SessionWindow, ...]) -> SessionWindow | None:
    for w in windows:
        if w.contains(minute):
            return w
    return None


def evaluate_quality(
    *,
    cfg: DecisionConfig,
    direction: str,
    pattern: str,
    regime: str,
    atr_pct: float | None,
    bars: Iterable[Any] = (),
    score: float | None = None,
    gap: float | None = None,
    now_ms: int | None = None,
) -> QualityVerdict:
    """返回是否放行、建议仓位系数与可审计理由。"""
    if not cfg.quality_enabled:
        return QualityVerdict(True, 1.0, ["质量门关闭"], {"enabled": False})

    reasons: list[str] = []
    tags: dict[str, Any] = {"enabled": True}
    now = now_ms or int(datetime.now(timezone.utc).timestamp() * 1000)
    dt = datetime.fromtimestamp(now / 1000, timezone.utc)
    minute = dt.hour * 60 + dt.minute
    tags["utcHour"] = dt.hour

    # --- 时段：两段回放里最稳定、幅度最大的负贡献 ---
    hit_window = _in_window(minute, cfg.quality_blackouts)
    if hit_window is not None:
        return QualityVerdict(
            False, 0.0,
            [f"UTC {hit_window.label or '噪声时段'} 历史两段均为负期望，暂停开仓"],
            {**tags, "blocked": "session"})

    # --- 影线噪声：两端都差，高噪声最稳定 ---
    nr = noise_ratio(bars)
    tags["noiseRatio"] = round(nr, 4) if nr is not None else None
    if nr is not None and nr >= cfg.quality_noise_reject:
        return QualityVerdict(
            False, 0.0,
            [f"影线噪声 {nr:.3f} ≥ {cfg.quality_noise_reject:.3f}，"
             "假突破/来回扫损高发，拒绝开仓"],
            {**tags, "blocked": "noise"})

    # --- 形态：两段都稳定为负的反转形态 ---
    key = str(pattern or "").lower()
    if key in cfg.quality_pattern_blacklist:
        return QualityVerdict(
            False, 0.0,
            [f"形态 {key} 在两段回放里均为负期望，跳过"],
            {**tags, "blocked": "pattern"})

    size = 1.0
    # --- 震荡处理：range 最好、trend 最差，趋势市降仓而不是硬拒 ---
    if regime == "trend":
        size *= cfg.quality_trend_size_factor
        reasons.append(f"趋势市降仓至 {size:.0%}")
    tags["regime"] = regime

    # 极低噪声（死水）也偏弱，但不稳定，只降仓
    if nr is not None and nr <= cfg.quality_noise_low:
        size *= 0.8
        reasons.append(f"低噪声 {nr:.3f} 降仓至 {size:.0%}")

    # --- 极端波动分位：两端都不如中段 ---
    if atr_pct is not None and (atr_pct <= 15 or atr_pct >= 85):
        size *= cfg.quality_extreme_vol_size_factor
        reasons.append(f"波动分位 {atr_pct:.0f} 极端，降仓至 {size:.0%}")
    tags["atrPct"] = atr_pct

    # --- 评分反向：高分/大分离度只降仓，不再当硬门槛（可回滚复核） ---
    if (score is not None and score >= cfg.minimum_score) or (
            gap is not None and gap >= cfg.quality_high_gap):
        size *= cfg.quality_high_score_size_factor
        reasons.append(f"高分/大分离度在回放里反向，降仓至 {size:.0%}")

    tags["sizeFactor"] = round(size, 4)
    return QualityVerdict(True, round(size, 4), reasons, tags)
