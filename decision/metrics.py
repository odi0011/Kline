"""统一的胜率与回撤口径。

## 为什么必须有这个模块

线上曾经同时存在四套算法，同一个「总胜率」能差出几十个百分点：

- `decision/paper.py::_db_summary` 用 wins/(wins+losses)（正确）
- `scripts/strategy_report.py` 用 wins/全部笔数（把平价单算进分母，偏低）
- `decision/backtest.py::_metrics` 用 wins/len(trades)（回测里没有平价单，
  数值接近但定义不同）
- `decision/paper.py::check_stats` 从 equity=0/peak=0 起算回撤，
  **一开始就亏的话 peak 始终为 0，早期亏损完全不计入**，且只取最近
  2000 条流水（其中 93% 是 removed 零盈亏噪音），曲线几乎是平的

结论互相矛盾时，先统一口径再谈结论。所有消费方都走这里。

## 定义

- **胜率** = 胜 / (胜 + 负)，返回百分数；pnl 恰好为 0 的平价离场、
  超时、被移除的噪音流水都不进分母。分母为 0 时返回 None（不是 0%，
  「没有样本」和「全输」是两回事）。
- **最大回撤** = 权益曲线从历史峰值回落的最大比例，返回**正数**百分比。
  peak 从曲线第一个值起算，不是从 0 起算；因此开局连亏也会被计入。
  同时给金额、当前回撤、峰值与谷值，便于核对。
"""
from __future__ import annotations

from typing import Iterable, Sequence

__all__ = ["decided_win_rate", "max_drawdown", "equity_curve"]


def decided_win_rate(wins: int, losses: int) -> float | None:
    """只以有明确输赢的交易为分母的胜率（百分数）。"""
    w, l = int(wins or 0), int(losses or 0)
    decided = w + l
    return round(w / decided * 100, 2) if decided > 0 else None


def equity_curve(pnls: Iterable[float], base: float = 0.0) -> list[float]:
    """把逐笔盈亏累计成权益绝对值序列，首元素是起始权益。"""
    eq = float(base)
    out = [eq]
    for x in pnls:
        eq += float(x or 0)
        out.append(eq)
    return out


def max_drawdown(curve: Sequence[float]) -> dict:
    """权益绝对值序列 -> 回撤指标。

    返回正数百分比（例如 2.57 表示最大回撤 2.57%），并附金额与关键点位。
    空序列 / 非正峰值时返回全零结构，不抛异常。
    """
    vals = [float(v) for v in curve]
    if not vals:
        return {"maxPct": 0.0, "maxAbs": 0.0, "currentPct": 0.0,
                "currentAbs": 0.0, "peak": 0.0, "trough": 0.0,
                "end": 0.0, "points": 0}
    peak = vals[0]
    worst_pct = 0.0
    worst_abs = 0.0
    trough = vals[0]
    for v in vals:
        peak = max(peak, v)
        if peak > 0:
            drop = peak - v
            pct = drop / peak * 100
            if pct > worst_pct:
                worst_pct = pct
                worst_abs = drop
                trough = v
    last = vals[-1]
    cur_abs = last - peak
    cur_pct = (-cur_abs / peak * 100) if peak > 0 else 0.0
    return {
        "maxPct": round(worst_pct, 4),
        "maxAbs": round(worst_abs, 4),
        "currentPct": round(cur_pct, 4),
        "currentAbs": round(-cur_abs, 4),
        "peak": round(peak, 4),
        "trough": round(trough, 4),
        "end": round(last, 4),
        "points": len(vals),
    }
