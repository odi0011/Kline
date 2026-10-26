from __future__ import annotations

from collections import deque
from typing import Callable

from .candle import Candle, apply_trade, interval_ms, new_bucket

HISTORY_LEN = 300


class KlineAggregator:
    """逐笔成交驱动的K线聚合器。

    事件驱动：每笔成交立即更新当前K线（on_update），桶切换瞬间判定上一根收线（on_close），
    不依赖定时器轮询，收线延迟 = 首笔下一桶成交的到达时刻。
    """

    def __init__(
        self,
        symbol: str,
        interval: str,
        on_update: Callable[[str, str, Candle, bool, list[Candle]], None],
        on_close: Callable[[str, str, Candle, bool, list[Candle]], None],
        history: list[Candle] | None = None,
    ):
        self.symbol = symbol
        self.interval = interval
        self.iv_ms = interval_ms(interval)
        self.on_update = on_update
        self.on_close = on_close
        self.history: deque[Candle] = deque(history or [], maxlen=HISTORY_LEN)
        self.current: Candle | None = None

    def seed(self, candles: list[Candle]) -> None:
        """回补历史K线。最后一根若尚未过收线时间，作为当前进行中的K线。"""
        if not candles:
            return
        last = candles[-1]
        if last.open_time == new_bucket(self.now_ms(), self.iv_ms):
            last.closed = False
            self.current = last
            self.history.extend(candles[:-1])
        else:
            self.current = None
            self.history.extend(candles)

    def on_trade(self, price: float, qty: float, ts: int, buyer_maker: bool = False) -> None:
        bucket = new_bucket(ts, self.iv_ms)
        cur = self.current
        if cur is None or bucket != cur.open_time:
            if cur is not None:
                self._finalize(cur)
            cur = Candle(bucket, price, price, price, price, interval=self.interval)
            self.current = cur
        apply_trade(cur, price, qty, buyer_maker)
        self.on_update(self.symbol, self.interval, cur, False, list(self.history))

    def sweep(self, now_ms: int, grace_ms: int = 2000) -> None:
        """无成交兜底：当前桶已超时（grace）则强制收线，避免死寂市场K线悬空。"""
        cur = self.current
        if cur is not None and now_ms > cur.open_time + self.iv_ms + grace_ms:
            self._finalize(cur)
            self.current = None

    def _finalize(self, c: Candle) -> None:
        c.closed = True
        self.history.append(c)
        self.on_close(self.symbol, self.interval, c, True, list(self.history))

    @staticmethod
    def now_ms() -> int:
        import time
        return int(time.time() * 1000)
