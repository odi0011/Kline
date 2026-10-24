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


# ---------------- 单根形态 ----------------

@register
class Hammer(Pattern):
    key, name_zh, direction = "hammer", "锤子线", BULLISH
    priority = 10

    def check(self, cs):
        return _hammer_shape(cs[-1]) and trend_of(cs[:-1]) == "down"


@register
class HangingMan(Pattern):
    key, name_zh, direction = "hanging_man", "上吊线", BEARISH
    priority = 10

    def check(self, cs):
        return _hammer_shape(cs[-1]) and trend_of(cs[:-1]) == "up"


@register
class InvertedHammer(Pattern):
    key, name_zh, direction = "inverted_hammer", "倒锤子", BULLISH
    priority = 10

    def check(self, cs):
        return _star_shape(cs[-1]) and trend_of(cs[:-1]) == "down"


@register
class ShootingStar(Pattern):
    key, name_zh, direction = "shooting_star", "流星线", BEARISH
    priority = 10

    def check(self, cs):
        return _star_shape(cs[-1]) and trend_of(cs[:-1]) == "up"


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
    key, name_zh, direction = "bullish_harami", "看涨孕线", BULLISH
    priority = 15

    def check(self, cs):
        p, c = cs[-2], cs[-1]
        return (is_bear(p) and body(p) > 0
                and body_bottom(c) >= body_bottom(p) and body_top(c) <= body_top(p)
                and body(c) < body(p) * 0.6
                and trend_of(cs[:-2]) == "down")


@register
class BearishHarami(Pattern):
    key, name_zh, direction = "bearish_harami", "看跌孕线", BEARISH
    priority = 15

    def check(self, cs):
        p, c = cs[-2], cs[-1]
        return (is_bull(p) and body(p) > 0
                and body_bottom(c) >= body_bottom(p) and body_top(c) <= body_top(p)
                and body(c) < body(p) * 0.6
                and trend_of(cs[:-2]) == "up")


@register
class TweezerBottom(Pattern):
    key, name_zh, direction = "tweezer_bottom", "平头底部", BULLISH
    priority = 20

    def check(self, cs):
        p, c = cs[-2], cs[-1]
        return (is_bear(p) and is_bull(c) and p.low > 0
                and abs(p.low - c.low) <= 0.0005 * p.low
                and trend_of(cs[:-2]) == "down")


@register
class TweezerTop(Pattern):
    key, name_zh, direction = "tweezer_top", "平头顶部", BEARISH
    priority = 20

    def check(self, cs):
        p, c = cs[-2], cs[-1]
        return (is_bull(p) and is_bear(c) and p.high > 0
                and abs(p.high - c.high) <= 0.0005 * p.high
                and trend_of(cs[:-2]) == "up")


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
        return all(body(x) >= 0.5 * rng(x) for x in (a, b, c) if rng(x) > 0)


@register
class ThreeBlackCrows(Pattern):
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
        return all(body(x) >= 0.5 * rng(x) for x in (a, b, c) if rng(x) > 0)
