from __future__ import annotations

import time

from core.candle import Candle, INTERVAL_MS
from patterns.base import Pattern, PatternHit, get_enabled

WINDOW = 60          # 传给形态的K线窗口（形态本体 + 趋势上下文）
DEDUPE_MAX = 20000
DEDUPE_TTL_MS = 2 * 60 * 60 * 1000
LIVE_MIN_RATIO = 0.92    # 盘中预警只在K线完成度≥92%时判定（此前形态未成型，误报率高）
MIN_RANGE_PCT = 0.0003   # 死寂K线过滤：振幅低于价格0.03%的K线不做判定


class PatternEngine:
    """订阅K线事件，跑所有已启用形态，去重后吐 PatternHit。

    两种触发：
      live      —— 盘中每笔成交都检查（仅 live=True 的形态）；
      confirmed —— 收线瞬间检查全部形态。
    同一根K线命中多个形态时（定义天然重叠，如平头顶部常伴随看跌吞没），
    只报优先级最高的一个，且每根K线每种模式只报一次。
    """

    def __init__(self, enabled, notifier, live_preview: bool = True,
                 trend_bars: int = 10, trend_threshold: float = 0.002):
        self.patterns: list[Pattern] = enabled
        self.notifier = notifier
        self.live_preview = live_preview
        self.trend_bars = trend_bars
        self.trend_threshold = trend_threshold
        self.detect_interval = None        # 仅检测该周期（None=全部）；由前端设置动态下发
        self._seen: dict[tuple, int] = {}

    def _dedupe_ok(self, key: tuple) -> bool:
        now = time.time() * 1000
        if len(self._seen) > DEDUPE_MAX:
            self._seen = {k: t for k, t in self._seen.items() if now - t < DEDUPE_TTL_MS}
        if key in self._seen:
            return False
        self._seen[key] = now
        return True

    def on_candle(self, symbol: str, interval: str, candle: Candle,
                  closed: bool, history: list[Candle]) -> None:
        if self.detect_interval and interval != self.detect_interval:
            return                          # 非当前检测周期，跳过形态判定
        r = candle.high - candle.low
        if candle.close > 0 and r / candle.close < MIN_RANGE_PCT:
            return                          # 死寂K线（振幅过小），形态无意义
        if not closed:
            iv_ms = INTERVAL_MS[interval]
            elapsed = time.time() * 1000 - candle.open_time
            if elapsed < LIVE_MIN_RATIO * iv_ms:
                return                      # K线尚未走到尾声，形态未成型，不报盘中预警
        mode = "confirmed" if closed else "live"
        window = history[-WINDOW:]
        if not closed and (not window or window[-1].open_time != candle.open_time):
            window = window + [candle]
        if len(window) < 3:
            return
        candidates = []
        for p in self.patterns:
            if not closed and not (self.live_preview and p.live):
                continue
            if len(window) < p.min_bars + 3:      # 形态本体 + 最低限度趋势上下文
                continue
            try:
                if p.check(window):
                    candidates.append(p)
            except Exception:
                continue
        if not candidates:
            return
        # 同一根K线多个形态重叠时只报优先级最高的一个（平数取注册顺序靠前者）
        best = max(candidates, key=lambda p: (p.priority, p.min_bars))
        if not self._dedupe_ok((symbol, interval, candle.open_time, mode)):
            return
        self.notifier(PatternHit(
            symbol=symbol, interval=interval, pattern=best.key,
            name_zh=best.name_zh, direction=best.direction, mode=mode,
            price=candle.close, bar_open_time=candle.open_time,
            ts=int(time.time() * 1000),
        ))
