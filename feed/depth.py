from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Callable

import websockets

# 币安 2026-03 起 WS 分路由：depth 属 /public，与 aggTrade(/market) 不同路由
DEPTH_URL = "wss://fstream.binance.com/public/stream"


class DepthStream:
    """币安 USDT-M 合约订单簿深度流（partial book depth，免 key）。

    每 symbol 一个连接，只保留最近一次快照（前端画深度图用）。
    默认 20 档、500ms 推送，够画深度曲线且流量很小。
    """

    def __init__(
        self,
        symbols: list[str],
        on_depth: Callable[[str, dict], None],
        proxy: str | None = None,
        levels: int = 20,
        speed_ms: int = 500,
    ):
        self.symbols = [s.lower() for s in symbols]
        self.on_depth = on_depth
        self.proxy = proxy
        self.levels = levels
        self.speed_ms = speed_ms
        self.latest: dict[str, dict] = {}
        self.last_msg_ms: dict[str, int] = {}
        self._tasks: list[asyncio.Task] = []

    async def run(self) -> None:
        """每个币一个长连接；单币断线只重连该币，不影响其它。"""
        self._tasks = [asyncio.create_task(self._one(s)) for s in self.symbols]
        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            for t in self._tasks:
                t.cancel()
            raise

    async def _one(self, symbol: str) -> None:
        url = f"{DEPTH_URL}?streams={symbol}@depth{self.levels}@{self.speed_ms}ms"
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(
                    url, ping_interval=20, ping_timeout=20,
                    proxy=self.proxy or True, max_size=2**22,
                ) as ws:
                    backoff = 1.0
                    started = time.monotonic()
                    self.last_msg_ms[symbol] = int(time.time() * 1000)

                    async def watchdog():
                        while True:
                            await asyncio.sleep(5)
                            last = self.last_msg_ms.get(symbol, 0)
                            if time.time() * 1000 - last > 15_000:
                                await ws.close()
                                return
                            if time.monotonic() - started > 23 * 3600:
                                await ws.close()
                                return

                    watch_task = asyncio.create_task(watchdog())
                    try:
                        async for raw in ws:
                            self._handle(symbol, raw)
                    finally:
                        watch_task.cancel()
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def _handle(self, symbol: str, raw) -> None:
        try:
            msg = json.loads(raw)
            d = msg.get("data") or msg
            bids = d.get("bids") or d.get("b") or []
            asks = d.get("asks") or d.get("a") or []
            if not bids and not asks:
                return
            snap = {
                "symbol": symbol,
                # [[price, qty], ...] 按价格排序，都是交易所真实挂单
                "bids": [[float(p), float(q)] for p, q in bids],
                "asks": [[float(p), float(q)] for p, q in asks],
                "ts": int(time.time() * 1000),
            }
            self.latest[symbol] = snap
            self.last_msg_ms[symbol] = int(time.time() * 1000)
            self.on_depth(symbol, snap)
        except Exception:
            return

    def is_healthy(self, symbol: str, stale_sec: float = 15.0) -> bool:
        last = self.last_msg_ms.get(symbol.lower(), 0)
        return bool(last and time.time() * 1000 - last <= stale_sec * 1000)
