"""风险指标：CVaR、已实现波动率、回撤。

这几个数看板上一直缺失，但它们才是"这个策略能不能扛住"的直接答案：
胜率高不代表能活，一笔尾部亏损就能吃掉几十笔利润。

全部是**历史样本**口径，不做参数假设、不拟合分布：
  · CVaR（Expected Shortfall）= 最差 q% 样本的均值，比 VaR 更能反映尾部有多深
  · 已实现波动率 = 收益率标准差（按持有周期折算，不做年化——这是超短线，
    年化只会得到一个没有意义的巨大数字）
  · 回撤 = 从历史峰值往下的百分比，同时给出持续时间

每个函数都明确返回 samples，样本不足时宁可返回 None 也不给一个假数字。
"""
from __future__ import annotations

import math

# 样本少于这个数就不报 CVaR：3 笔亏损的"最差 5%"没有统计意义
MIN_CVAR_SAMPLES = 20
# 回撤至少需要两个采样点才有"从峰值往下的距离"可言
MIN_DRAWDOWN_POINTS = 3


def _finite(values) -> list[float]:
    out = []
    for value in values or []:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            out.append(number)
    return out


def cvar(returns, level: float = 0.95) -> float | None:
    """历史 CVaR（期望损失）：最差 (1-level) 那部分样本的均值。

    返回负数（亏损为负）或 None。level=0.95 → 最差 5% 的平均。
    样本不足 MIN_CVAR_SAMPLES 时返回 None——几笔样本算出来的尾部毫无意义，
    宁可显示"样本不足"，也不要让一个随机数字看起来像风险度量。
    """
    values = sorted(_finite(returns))
    if len(values) < MIN_CVAR_SAMPLES:
        return None
    try:
        tail = max(1, int(len(values) * (1 - float(level))))
    except (TypeError, ValueError):
        return None
    worst = values[:tail]
    return round(sum(worst) / len(worst), 6) if worst else None


def volatility(returns) -> float | None:
    """已实现波动率 = 收益率样本标准差（不年化）。

    超短线没有"年"的概念：3 分钟周期的策略年化会得到 100%+ 的噪声数字，
    对"这一笔可能抖多少"毫无帮助。要看的是每一期的典型波动幅度。
    """
    values = _finite(returns)
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return round(math.sqrt(max(variance, 0.0)), 6)


def drawdown(equity_values) -> dict | None:
    """从峰值回撤：当前、最大、峰值本身，以及当前回撤已持续多少个采样点。

    输入可以是权益采样序列（升序）。返回的百分比都是**负数或 0**。
    """
    values = _finite(equity_values)
    if len(values) < MIN_DRAWDOWN_POINTS:
        return None
    peak = values[0]
    max_dd = 0.0
    max_dd_at = 0
    peak_at = 0
    current_peak = values[0]
    current_peak_at = 0
    for index, value in enumerate(values):
        if value > peak:
            peak = value
            peak_at = index
        if peak > 0:
            dd = (value - peak) / peak
            if dd < max_dd:
                max_dd = dd
                max_dd_at = index
        if value > current_peak:
            current_peak = value
            current_peak_at = index
    current = ((values[-1] - current_peak) / current_peak) if current_peak > 0 else 0.0
    return {
        "current": round(current * 100, 3),
        "max": round(max_dd * 100, 3),
        "peak": round(peak, 2),
        # 当前回撤持续了多少个采样点（采样间隔固定时可直接换算成时间）
        "currentBars": max(0, len(values) - 1 - current_peak_at),
        "maxBars": max(0, max_dd_at - peak_at),
        "samples": len(values),
    }


def tail_profile(returns) -> dict:
    """尾部画像：把 CVaR / VaR / 最差值 / 波动率 打包，并给出样本量。

    这是看板上那一格"风险"要的全部内容。样本不足时每个数各自为 None，
    但 samples 一定给出——看的人必须知道这个结论建立在多少笔上。
    """
    values = _finite(returns)
    profile = {
        "samples": len(values),
        "volatility": volatility(values),
        "cvar95": cvar(values, 0.95),
        "cvar99": cvar(values, 0.99),
        "worst": round(min(values), 6) if values else None,
        "best": round(max(values), 6) if values else None,
        "mean": round(sum(values) / len(values), 6) if values else None,
        "enoughSamples": len(values) >= MIN_CVAR_SAMPLES,
    }
    return profile
