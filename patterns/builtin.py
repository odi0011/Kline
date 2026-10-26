"""内置形态库。新增形态：复制一个类、改 key/name_zh/条件即可，装饰器自动注册。"""
from __future__ import annotations

from .base import (
    BEARISH, BULLISH, NEUTRAL,
    Pattern, register,
    body, body_bottom, body_top, is_bear, is_bull, lower_wick, rng, trend_of, upper_wick,
)

# 趋势参数与 config 对齐的默认值；check 里取的 candles 窗口足够长（引擎给 60 根）


def _hammer_shape(c) -> bool:
    r = rng(c)
    if r <= 0:
        return False
    b = body(c)
    return lower_wick(c) >= 2 * b and upper_wick(c) <= 0.25 * r and b <= 0.35 * r


def _star_shape(c) -> bool:
    r = rng(c)
    if r <= 0:
        return False
    b = body(c)
    return upper_wick(c) >= 2 * b and lower_wick(c) <= 0.25 * r and b <= 0.35 * r


def _avg_range(cs) -> float:
    """平均振幅：用作"显著波动"的度量，比按价格百分比更适应不同币种/周期。"""
    if not cs:
        return 0.0
    return sum(c.high - c.low for c in cs) / len(cs)


def _is_swing_low(cs, lo: int, hi: int, lookback: int = 8, unit: float = 0.0) -> bool:
    """[lo,hi] 区间的最低价是否接近近端最低（局部摆动低点）。

    反转形态必须出现在趋势末端：如果之前的 lookback 根里出现过明显更低的低点
    （低出半个平均振幅以上），说明价格还在下跌途中，这两根只是巧合，不是底部。
    """
    if lo <= 0:
        return True
    seg_low = min(c.low for c in cs[lo:hi + 1])
    prior = cs[max(0, lo - lookback):lo]
    if not prior:
        return True
    prev_low = min(c.low for c in prior)
    tol = (unit or _avg_range(prior)) * 0.5
    return seg_low <= prev_low + tol


def _is_swing_high(cs, lo: int, hi: int, lookback: int = 8, unit: float = 0.0) -> bool:
    """[lo,hi] 区间的最高价是否接近近端最高（局部摆动高点）。顶部形态用。"""
    if lo <= 0:
        return True
    seg_high = max(c.high for c in cs[lo:hi + 1])
    prior = cs[max(0, lo - lookback):lo]
    if not prior:
        return True
    prev_high = max(c.high for c in prior)
    tol = (unit or _avg_range(prior)) * 0.5
    return seg_high >= prev_high - tol


# ---------------- 单根形态 ----------------

@register
class Hammer(Pattern):
    """锤子线：下跌末端长下影小实体。实体必须是有效K线（十字星交给十字星形态）。"""
    key, name_zh, direction = "hammer", "锤子线", BULLISH
    priority = 10

    def check(self, cs):
        c = cs[-1]
        unit = _avg_range(cs[-20:-1])
        if unit > 0 and body(c) < 0.15 * unit:
            return False
        return _hammer_shape(c) and trend_of(cs[:-1]) == "down"


@register
class HangingMan(Pattern):
    """上吊线：上涨末端长下影小实体（锤子线的顶部镜像）。校验同流星线。"""
    key, name_zh, direction = "hanging_man", "上吊线", BEARISH
    priority = 10

    def check(self, cs):
        c = cs[-1]
        if not _hammer_shape(c):
            return False
        unit = _avg_range(cs[-20:-1])
        if unit > 0:
            if body(c) < 0.15 * unit:
                return False
            if lower_wick(c) < unit * 0.9:
                return False
        if not _is_swing_high(cs, len(cs) - 1, len(cs) - 1, 8, unit):
            return False
        return trend_of(cs[:-1]) == "up"


@register
class InvertedHammer(Pattern):
    """倒锤子：下跌末端长上影小实体（流星线的底部镜像）。校验同理。"""
    key, name_zh, direction = "inverted_hammer", "倒锤子", BULLISH
    priority = 10

    def check(self, cs):
        c = cs[-1]
        if not _star_shape(c):
            return False
        unit = _avg_range(cs[-20:-1])
        if unit > 0:
            if body(c) < 0.15 * unit:
                return False
            if upper_wick(c) < unit * 0.9:
                return False
        if not _is_swing_low(cs, len(cs) - 1, len(cs) - 1, 8, unit):
            return False
        return trend_of(cs[:-1]) == "down"


@register
class ShootingStar(Pattern):
    """流星线：上涨末端长上影小实体。

    原实现仅要求"长上影小实体 + 前10根涨过0.2%"，实测准确率仅 14%：
    上涨途中任何一次冲高回落都会命中。加三道校验：
      1. 实体不能是十字（十字星不算流星）
      2. 上影要显著长于近期平均振幅，否则只是普通上影
      3. 必须是局部最高点（前 8 根没有明显更高的高点）
    """
    key, name_zh, direction = "shooting_star", "流星线", BEARISH
    priority = 10

    def check(self, cs):
        c = cs[-1]
        if not _star_shape(c):
            return False
        unit = _avg_range(cs[-20:-1])
        if unit > 0:
            if body(c) < 0.15 * unit:
                return False
            if upper_wick(c) < unit * 0.9:
                return False
        if not _is_swing_high(cs, len(cs) - 1, len(cs) - 1, 8, unit):
            return False
        return trend_of(cs[:-1]) == "up"


@register
class Doji(Pattern):
    key, name_zh, direction = "doji", "十字星", NEUTRAL
    priority = 5

    def check(self, cs):
        c = cs[-1]
        r = rng(c)
        return r > 0 and body(c) <= 0.05 * r


@register
class DragonflyDoji(Pattern):
    key, name_zh, direction = "dragonfly_doji", "蜻蜓十字", BULLISH
    priority = 12

    def check(self, cs):
        c = cs[-1]
        r = rng(c)
        if r <= 0 or body(c) > 0.05 * r:
            return False
        return lower_wick(c) >= 0.6 * r and upper_wick(c) <= 0.1 * r and trend_of(cs[:-1]) == "down"


@register
class GravestoneDoji(Pattern):
    key, name_zh, direction = "gravestone_doji", "墓碑十字", BEARISH
    priority = 12

    def check(self, cs):
        c = cs[-1]
        r = rng(c)
        if r <= 0 or body(c) > 0.05 * r:
            return False
        return upper_wick(c) >= 0.6 * r and lower_wick(c) <= 0.1 * r and trend_of(cs[:-1]) == "up"


# ---------------- 双根形态 ----------------

@register
class BullishEngulfing(Pattern):
    key, name_zh, direction = "bullish_engulfing", "看涨吞没", BULLISH
    priority = 26

    def check(self, cs):
        p, c = cs[-2], cs[-1]
        return (is_bear(p) and is_bull(c)
                and c.close >= p.open and c.open <= p.close
                and body(p) > 0
                and trend_of(cs[:-2]) == "down")


@register
class BearishEngulfing(Pattern):
    key, name_zh, direction = "bearish_engulfing", "看跌吞没", BEARISH
    priority = 26

    def check(self, cs):
        p, c = cs[-2], cs[-1]
        return (is_bull(p) and is_bear(c)
                and c.close <= p.open and c.open >= p.close
                and body(p) > 0
                and trend_of(cs[:-2]) == "up")


@register
class PiercingLine(Pattern):
    key, name_zh, direction = "piercing", "刺透线", BULLISH
    priority = 24

    def check(self, cs):
        p, c = cs[-2], cs[-1]
        mid = (p.open + p.close) / 2
        return (is_bear(p) and is_bull(c) and body(p) > 0
                and c.open < p.close and mid < c.close < p.open
                and trend_of(cs[:-2]) == "down")


@register
class DarkCloudCover(Pattern):
    key, name_zh, direction = "dark_cloud", "乌云盖顶", BEARISH
    priority = 24

    def check(self, cs):
        p, c = cs[-2], cs[-1]
        mid = (p.open + p.close) / 2
        return (is_bull(p) and is_bear(c) and body(p) > 0
                and c.open > p.close and p.open > c.close > mid
                and trend_of(cs[:-2]) == "up")


@register
class BullishHarami(Pattern):
    """看涨孕线：下跌末端大阴线内包一根小阳线（看跌孕线镜像）。"""
    key, name_zh, direction = "bullish_harami", "看涨孕线", BULLISH
    priority = 15

    def check(self, cs):
        p, c = cs[-2], cs[-1]
        if not (is_bear(p) and is_bull(c)) or body(p) <= 0:
            return False
        unit = _avg_range(cs[-20:-2])
        if unit > 0 and body(p) < unit * 0.8:
            return False
        if not (body_bottom(c) >= body_bottom(p) and body_top(c) <= body_top(p)):
            return False
        if body(c) >= body(p) * 0.4:
            return False
        if not _is_swing_low(cs, len(cs) - 2, len(cs) - 1, 8, unit):
            return False
        return trend_of(cs[:-2]) == "down"


@register
class BearishHarami(Pattern):
    """看跌孕线：上涨末端大阳线内包一根小阴线，动能衰减。

    原实现准确率仅 33%：只要小实体落在大实体里就算，不管母线多大。
    加：母线实体要显著（>=0.8 个平均振幅）、子线要明显缩小（<0.4），
    且必须是局部高点附近。
    """
    key, name_zh, direction = "bearish_harami", "看跌孕线", BEARISH
    priority = 15

    def check(self, cs):
        p, c = cs[-2], cs[-1]
        if not (is_bull(p) and is_bear(c)) or body(p) <= 0:
            return False
        unit = _avg_range(cs[-20:-2])
        if unit > 0 and body(p) < unit * 0.8:
            return False
        if not (body_bottom(c) >= body_bottom(p) and body_top(c) <= body_top(p)):
            return False
        if body(c) >= body(p) * 0.4:
            return False
        if not _is_swing_high(cs, len(cs) - 2, len(cs) - 1, 8, unit):
            return False
        return trend_of(cs[:-2]) == "up"


@register
class TweezerBottom(Pattern):
    """平头底部：下跌末端两根K线低点几乎相同，说明该价位有承接。

    原实现只看"两低点接近"，下跌途中任意两根巧合低点相近就命中（实测
    240 根内 8 次、50% 之后继续跌破）。加三道校验：
      1. 两低点要接近（价格 0.05% 与 0.6 个平均振幅取小）
      2. 必须是局部最低点：前 8 根里不能有明显更低的低点，否则不是底
      3. 反转要有力度：阳线收在前阴收盘之上、两根实体都不是十字
    """
    key, name_zh, direction = "tweezer_bottom", "平头底部", BULLISH
    priority = 20

    def check(self, cs):
        p, c = cs[-2], cs[-1]
        if not (is_bear(p) and is_bull(c)) or p.low <= 0:
            return False
        unit = _avg_range(cs[-20:])
        tol = min(p.low * 0.0005, unit * 0.6) if unit > 0 else p.low * 0.0005
        if abs(p.low - c.low) > tol:
            return False
        if not _is_swing_low(cs, len(cs) - 2, len(cs) - 1, 8, unit):
            return False
        if c.close <= p.close:                 # 阳线没收回阴线失地 → 反弹无力
            return False
        if body(c) < 0.25 * unit or body(p) < 0.2 * unit:   # 十字星不算有效K线
            return False
        return trend_of(cs[:-2]) == "down"


@register
class TweezerTop(Pattern):
    """平头顶部：上涨末端两根K线高点几乎相同，说明该价位有抛压。校验同平头底部（镜像）。"""
    key, name_zh, direction = "tweezer_top", "平头顶部", BEARISH
    priority = 20

    def check(self, cs):
        p, c = cs[-2], cs[-1]
        if not (is_bull(p) and is_bear(c)) or p.high <= 0:
            return False
        unit = _avg_range(cs[-20:])
        tol = min(p.high * 0.0005, unit * 0.6) if unit > 0 else p.high * 0.0005
        if abs(p.high - c.high) > tol:
            return False
        if not _is_swing_high(cs, len(cs) - 2, len(cs) - 1, 8, unit):
            return False
        if c.close >= p.close:                 # 阴线没吞回阳线涨幅 → 抛压不足
            return False
        if body(c) < 0.25 * unit or body(p) < 0.2 * unit:
            return False
        return trend_of(cs[:-2]) == "up"


# ---------------- 三根形态 ----------------

@register
class MorningStar(Pattern):
    key, name_zh, direction = "morning_star", "启明星", BULLISH
    priority = 30

    def check(self, cs):
        p1, p2, p3 = cs[-3], cs[-2], cs[-1]
        b1 = body(p1)
        return (is_bear(p1) and b1 > 0
                and body(p2) <= 0.4 * b1
                and body_top(p2) <= p1.close + 0.2 * b1
                and is_bull(p3) and p3.close >= (p1.open + p1.close) / 2
                and trend_of(cs[:-3]) == "down")


@register
class EveningStar(Pattern):
    key, name_zh, direction = "evening_star", "黄昏星", BEARISH
    priority = 30

    def check(self, cs):
        p1, p2, p3 = cs[-3], cs[-2], cs[-1]
        b1 = body(p1)
        return (is_bull(p1) and b1 > 0
                and body(p2) <= 0.4 * b1
                and body_bottom(p2) >= p1.close - 0.2 * b1
                and is_bear(p3) and p3.close <= (p1.open + p1.close) / 2
                and trend_of(cs[:-3]) == "up")


@register
class ThreeWhiteSoldiers(Pattern):
    """三白兵：连续三根实体饱满的阳线逐级抬升（三只乌鸦镜像）。"""
    key, name_zh, direction = "three_white_soldiers", "三白兵", BULLISH
    priority = 30

    def check(self, cs):
        a, b, c = cs[-3], cs[-2], cs[-1]
        if not (is_bull(a) and is_bull(b) and is_bull(c)):
            return False
        if not (b.close > a.close and c.close > b.close):
            return False
        if not (a.open < b.open < b.close and b.open < c.open < c.close):
            return False
        if not all(body(x) >= 0.5 * rng(x) for x in (a, b, c) if rng(x) > 0):
            return False
        unit = _avg_range(cs[-20:-3])
        if unit > 0:
            if any(body(x) < unit * 0.7 for x in (a, b, c)):
                return False
            if c.close - a.open < unit * 1.5:
                return False
        if not _is_swing_low(cs, len(cs) - 3, len(cs) - 1, 8, unit):
            return False
        return True


@register
class ThreeBlackCrows(Pattern):
    """三只乌鸦：连续三根实体饱满的阴线逐级下探。

    原实现准确率 44%：三根小阴线也算，实际只是横盘。加：
    每根实体 >= 0.7 平均振幅、三根累计跌幅 >= 1.5 个振幅、出现在局部高点后。
    """
    key, name_zh, direction = "three_black_crows", "三只乌鸦", BEARISH
    priority = 30

    def check(self, cs):
        a, b, c = cs[-3], cs[-2], cs[-1]
        if not (is_bear(a) and is_bear(b) and is_bear(c)):
            return False
        if not (b.close < a.close and c.close < b.close):
            return False
        if not (a.open > b.open > b.close and b.open > c.open > c.close):
            return False
        if not all(body(x) >= 0.5 * rng(x) for x in (a, b, c) if rng(x) > 0):
            return False
        unit = _avg_range(cs[-20:-3])
        if unit > 0:
            if any(body(x) < unit * 0.7 for x in (a, b, c)):
                return False
            if a.open - c.close < unit * 1.5:
                return False
        if not _is_swing_high(cs, len(cs) - 3, len(cs) - 1, 8, unit):
            return False
        return True
