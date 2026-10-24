from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from core.candle import Candle

BULLISH = "bullish"    # 看涨
BEARISH = "bearish"    # 看跌
NEUTRAL = "neutral"    # 中性


@dataclass
class PatternHit:
    symbol: str
    interval: str
    pattern: str
    name_zh: str
    direction: str
    mode: str            # live=盘中预警 / confirmed=收线确认
    price: float
    bar_open_time: int
    ts: int              # 本地检出时间 ms


# ============ K线原语（写新形态直接用这些函数） ============

def body(c: Candle) -> float:
    return abs(c.close - c.open)


def upper_wick(c: Candle) -> float:
    return c.high - max(c.open, c.close)


def lower_wick(c: Candle) -> float:
    return min(c.open, c.close) - c.low


def rng(c: Candle) -> float:
    return c.high - c.low


def is_bull(c: Candle) -> bool:
    return c.close > c.open


def is_bear(c: Candle) -> bool:
    return c.close < c.open


def body_top(c: Candle) -> float:
    return max(c.open, c.close)


def body_bottom(c: Candle) -> float:
    return min(c.open, c.close)


TREND_BARS = 10
TREND_THRESHOLD = 0.002


def trend_of(candles: list[Candle], n: int | None = None, thr: float | None = None) -> str:
    """用最近 n 根的涨跌幅判趋势，数据不足返回 flat。参数由 config 覆盖。"""
    n = n or TREND_BARS
    thr = TREND_THRESHOLD if thr is None else thr
    w = candles[-n:]
    if len(w) < 3:
        return "flat"
    base = w[0].open
    if base <= 0:
        return "flat"
    chg = (w[-1].close - base) / base
    if chg > thr:
        return "up"
    if chg < -thr:
        return "down"
    return "flat"


# ============ 形态基类与注册表 ============

_REGISTRY: dict[str, "Pattern"] = {}


def register(cls):
    p = cls()
    if not p.key:
        raise ValueError(f"{cls.__name__} 缺少 key")
    _REGISTRY[p.key] = p
    return cls


def all_patterns() -> dict[str, "Pattern"]:
    return dict(_REGISTRY)


def get_enabled(enabled: str | list[str]) -> list["Pattern"]:
    if enabled == "all":
        return list(_REGISTRY.values())
    return [_REGISTRY[k] for k in enabled if k in _REGISTRY]


class Pattern(ABC):
    """一个形态 = 对最近若干根K线（含上下文趋势）的布尔判定。

    子类只需设置四个类属性并实现 check()：
      key/name_zh/direction/min_bars；live=False 表示只在收线后判定。
    priority：同根K线命中多个形态时，引擎只报优先级最高的一个
    （结构越具体/信号越强优先级越高，如吞没 > 平头 > 孕线）。
    check(candles) 中 candles[-1] 是形态的最后一根，往前的作为上下文
    （trend_of(candles[:-k]) 取形态之前的趋势，窗口不够时自动返回 flat）。
    """

    key: str = ""
    name_zh: str = ""
    direction: str = NEUTRAL
    min_bars: int = 1
    priority: int = 0
    live: bool = True

    @abstractmethod
    def check(self, candles: list[Candle]) -> bool: ...
