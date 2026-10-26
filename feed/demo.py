"""演示数据源：币安连不上/断流时兜底，随机游走生成逐笔成交，保证页面始终有图。"""
from __future__ import annotations

import asyncio
import random
import time

DEFAULT_SEEDS = {
    "btcusdt": 81000.0, "ethusdt": 3100.0, "bnbusdt": 740.0, "solusdt": 150.0,
}


class DemoSource:
    def __init__(self, symbols, on_trade, seeds: dict | None = None):
        self.symbols = [s.lower() for s in symbols]
        self.on_trade = on_trade
        self.prices = {s: (seeds or DEFAULT_SEEDS).get(s, 100.0) for s in self.symbols}

    @property
    def connected(self) -> bool:
        return True

    def lag_stats(self) -> dict:
        return {"avg": 3, "p50": 3, "max": 5, "n": 1}

    async def run(self) -> None:
        print("[feed] 演示模式：随机游走数据（非真实行情）")
        while True:
            await asyncio.sleep(0.05)
            now_ms = int(time.time() * 1000)
            for sym in self.symbols:
                if random.random() > 0.65:
                    continue
                p = self.prices[sym]
                p = max(p * (1 + random.gauss(0, 0.00008)), 0.01)
                self.prices[sym] = p
                qty = round(random.expovariate(2.0) + 0.001, 4)
                self.on_trade(sym, round(p, 2), qty, now_ms, now_ms, random.random() < 0.5)
