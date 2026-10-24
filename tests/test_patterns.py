from core.candle import Candle
from patterns.base import all_patterns
import patterns.builtin  # noqa: F401


def mk(o, c, h=None, l=None, ot=0):
    h = h if h is not None else max(o, c) * 1.001
    l = l if l is not None else min(o, c) * 0.999
    return Candle(ot, o, h, l, c, 1.0, 1, "1m", True)


def trend(n=10, start=100.0, step=-0.01):
    """n 根同向小实体K线，构造明确的趋势上下文。"""
    cs, p = [], start
    for i in range(n):
        o = p
        c = o * (1 + step)
        cs.append(mk(o, c, ot=i))
        p = c
    return cs


def flat(n=10, start=100.0):
    cs, p = [], start
    for i in range(n):
        c = p * (1 + (0.0005 if i % 2 else -0.0004))
        cs.append(mk(p, c, ot=i))
        p = c
    return cs


def check(key, candles):
    return all_patterns()[key].check(candles)


def test_hammer_after_downtrend():
    cs = trend()
    p = cs[-1].close
    cs.append(mk(p * 1.003, p * 1.0, h=p * 1.004, l=p * 0.982))  # 长下影、小上影
    assert check("hammer", cs)
    assert not check("hanging_man", cs)


def test_hammer_needs_trend():
    cs = flat()
    p = cs[-1].close
    cs.append(mk(p * 1.003, p * 1.0, h=p * 1.004, l=p * 0.982))
    assert not check("hammer", cs)          # 横盘时不触发


def test_hanging_man_after_uptrend():
    cs = trend(step=0.01)
    p = cs[-1].close
    cs.append(mk(p * 1.003, p * 1.0, h=p * 1.004, l=p * 0.982))
    assert check("hanging_man", cs)
    assert not check("hammer", cs)


def test_engulfing():
    cs = trend()
    p = cs[-1].close
    prev = mk(p * 1.002, p)                  # 阴线
    cur = mk(p * 0.999, p * 1.004)           # 阳线包住前一根实体
    cs += [prev, cur]
    assert check("bullish_engulfing", cs)
    assert not check("bearish_engulfing", cs)


def test_morning_star():
    cs = trend()
    p = cs[-1].close
    p1 = mk(p * 1.004, p)                    # 长阴
    p2 = mk(p * 0.999, p * 0.9993)           # 小实体星线
    p3 = mk(p * 0.9995, p * 1.003)           # 阳线收过第一根中点
    cs += [p1, p2, p3]
    assert check("morning_star", cs)
    assert not check("evening_star", cs)


def test_doji_no_trend_needed():
    cs = flat()
    cs.append(mk(100.0, 100.0, h=100.5, l=99.5))
    assert check("doji", cs)


def test_three_white_soldiers():
    cs = trend(step=0.004)
    cs += [mk(100.0, 101.0, h=101.1, l=99.9),
           mk(100.5, 101.5, h=101.6, l=100.4),
           mk(101.0, 102.0, h=102.1, l=100.9)]
    assert check("three_white_soldiers", cs)
    assert not check("three_black_crows", cs)


def test_registry_complete():
    expect = {
        "hammer", "hanging_man", "inverted_hammer", "shooting_star", "doji",
        "dragonfly_doji", "gravestone_doji", "bullish_engulfing", "bearish_engulfing",
        "piercing", "dark_cloud", "bullish_harami", "bearish_harami",
        "tweezer_bottom", "tweezer_top", "morning_star", "evening_star",
        "three_white_soldiers", "three_black_crows",
    }
    assert set(all_patterns()) == expect
