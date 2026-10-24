from __future__ import annotations

from dataclasses import dataclass, replace

INTERVAL_MS = {
    "1s": 1_000, "5s": 5_000, "15s": 15_000, "30s": 30_000,
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000,
    "4h": 14_400_000, "1d": 86_400_000,
}


def interval_ms(interval: str) -> int:
    try:
        return INTERVAL_MS[interval]
    except KeyError:
        raise ValueError(f"不支持的周期: {interval}，可选 {sorted(INTERVAL_MS)}") from None


@dataclass
class Candle:
    open_time: int      # ms, 开桶时间（对齐 interval）
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    trades: int = 0
    interval: str = ""
    closed: bool = False

    @property
    def close_time(self) -> int:
        return self.open_time + INTERVAL_MS[self.interval] - 1


def new_bucket(ts: int, iv_ms: int) -> int:
    return ts - ts % iv_ms


def apply_trade(c: Candle, price: float, qty: float) -> None:
    c.high = max(c.high, price)
    c.low = min(c.low, price)
    c.close = price
    c.volume += qty
    c.trades += 1
