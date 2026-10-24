from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Callable

import websockets

# 币安 2026-03 起 WS 分路由（/public /market /private）：aggTrade 属 /market，
# 不带路由前缀的旧连接只推 /public 类流——表现为握手成功但永远不推数据。
WS_URL = "wss://fstream.binance.com/market/stream"


class BinanceFuturesStream:
    """币安 USDT-M 合约公共行情流（aggTrade 逐笔，免 key）。

    自动重连（指数退避），记录推送延迟（本地收包时刻 - 交易所成交时刻）。
    proxy 参数: None=按 websockets 库默认读 WSS_PROXY/HTTPS_PROXY 环境变量;
    字符串=显式代理（websockets>=14 支持 socks5:// 与 http://）。
    """

    def __init__(
        self,
        symbols: list[str],
        on_trade: Callable[[str, float, float, int, int], None],
        proxy: str | None = None,
        stall_sec: float = 8.0,
    ):
        self.symbols = [s.lower() for s in symbols]
        self.on_trade = on_trade
        self.proxy = proxy
        self.stall_sec = stall_sec
        self.lags: deque[int] = deque(maxlen=5000)
        self._connected = asyncio.Event()
        self._ws = None
        self.last_trade_ms = 0
        self.connect_ms = 0
        self.stall_count = 0
        self.demo_requested = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def lag_stats(self) -> dict:
        if not self.lags:
            return {}
        ls = sorted(self.lags)
        return {
            "avg": sum(ls) / len(ls),
            "p50": ls[len(ls) // 2],
            "max": ls[-1],
            "n": len(ls),
        }

    async def run(self) -> None:
        streams = "/".join(f"{s}@aggTrade" for s in self.symbols)
        url = f"{WS_URL}?streams={streams}"
        backoff = 1.0
        watchdog = asyncio.create_task(self._watchdog())
        try:
            while True:
                try:
                    async with websockets.connect(
                        url,
                        ping_interval=20,
                        ping_timeout=20,
                        proxy=self.proxy or True,
                        max_size=2**22,
                    ) as ws:
                        backoff = 1.0
                        self._ws = ws
                        self.connect_ms = int(time.time() * 1000)
                        self._connected.set()
                        print(f"[feed] 已连接 {url.split('?')[0]} streams={len(self.symbols)}")
                        async for raw in ws:
                            self._handle(raw)
                except (websockets.ConnectionClosed, OSError, asyncio.CancelledError) as e:
                    if isinstance(e, asyncio.CancelledError):
                        raise
                    self._connected.clear()
                    self._ws = None
                    if self.demo_requested.is_set():
                        return
                    print(f"[feed] 连接断开: {type(e).__name__}: {e}，{backoff:.0f}s 后重连")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30)
        finally:
            watchdog.cancel()
            self._connected.clear()

    async def _watchdog(self) -> None:
        """握手成功但数据帧停流（被墙/代理分流直连的典型症状）→ 强制重连；
        连续 stall 次数超限 → 请求降级演示模式。"""
        while True:
            await asyncio.sleep(2)
            if not self._connected.is_set() or self.demo_requested.is_set():
                continue
            anchor = max(self.last_trade_ms, self.connect_ms)
            if anchor and time.time() * 1000 - anchor > self.stall_sec * 1000:
                self.stall_count += 1
                self.last_trade_ms = 0
                self.connect_ms = 0
                if self.stall_count >= 2:
                    self.demo_requested.set()
                    print("[feed] 连接反复无数据，触发降级为演示模式")
                ws, self._ws = self._ws, None
                if ws:
                    try:
                        await ws.close()
                    except Exception:
                        pass

    def _handle(self, raw: str | bytes) -> None:
        recv_ms = int(time.time() * 1000)
        try:
            msg = json.loads(raw)
            d = msg["data"]
            if d.get("e") != "aggTrade":
                return
            symbol = d["s"].lower()
            price = float(d["p"])
            qty = float(d["q"])
            trade_ms = int(d["T"])
            self.last_trade_ms = recv_ms
            self.lags.append(recv_ms - trade_ms)
            self.on_trade(symbol, price, qty, trade_ms, recv_ms)
        except Exception:
            return
