"""Binance force-order stream and liquidation-cascade detector."""
from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict, deque
from typing import Callable

import websockets

WS_URL = "wss://fstream.binance.com/market/stream"


class LiquidationStream:
    def __init__(
        self,
        symbols: list[str],
        on_event: Callable[[str, dict], None] | None = None,
        proxy: str | None = None,
        max_age_sec: int = 600,
    ):
        self.symbols = [s.lower() for s in symbols]
        self.on_event = on_event
        self.proxy = proxy
        self.max_age_ms = max_age_sec * 1000
        self.events: dict[str, deque] = {
            symbol: deque(maxlen=200) for symbol in self.symbols
        }
        self.last_message_ms = 0
        self.connected = False
        self.cooldown_until: dict[str, int] = {}

    async def run(self) -> None:
        streams = "/".join(f"{symbol}@forceOrder" for symbol in self.symbols)
        url = f"{WS_URL}?streams={streams}"
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(
                    url, ping_interval=20, ping_timeout=20,
                    proxy=self.proxy or True, max_size=2**22,
                ) as ws:
                    self.connected = True
                    backoff = 1.0
                    async for raw in ws:
                        self.last_message_ms = int(time.time() * 1000)
                        self._handle(raw)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.connected = False
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def _handle(self, raw: str | bytes) -> None:
        try:
            message = json.loads(raw)
            data = message.get("data") or message
            order = data.get("o") or {}
            symbol = str(order.get("s") or data.get("s") or "").lower()
            if not symbol:
                return
            price = float(order.get("ap") or order.get("p") or 0)
            qty = float(order.get("q") or order.get("z") or 0)
            event = {
                "symbol": symbol,
                "side": str(order.get("S") or ""),
                "price": price,
                "qty": qty,
                "notional": price * qty,
                "ts": int(order.get("T") or data.get("E") or time.time() * 1000),
            }
            bucket = self.events.setdefault(symbol, deque(maxlen=200))
            bucket.append(event)
            if self.on_event:
                self.on_event(symbol, event)
        except (TypeError, ValueError, KeyError):
            return

    def cascade(self, symbol: str, now_ms: int | None = None, window_sec: int = 300,
                min_events: int = 5, min_notional: float = 1_000_000,
                cooldown_sec: int = 900) -> dict:
        now_ms = now_ms or int(time.time() * 1000)
        bucket = self.events.get(symbol.lower(), deque())
        while bucket and now_ms - bucket[0]["ts"] > self.max_age_ms:
            bucket.popleft()
        recent = [event for event in bucket if now_ms - event["ts"] <= window_sec * 1000]
        notional = sum(event["notional"] for event in recent)
        active = len(recent) >= min_events or notional >= min_notional
        if active and recent:
            self.cooldown_until[symbol.lower()] = recent[-1]["ts"] + cooldown_sec * 1000
        cooldown = now_ms < self.cooldown_until.get(symbol.lower(), 0)
        sell_notional = sum(e["notional"] for e in recent if e["side"] == "SELL")
        buy_notional = sum(e["notional"] for e in recent if e["side"] == "BUY")
        return {
            "active": active,
            "blocked": active or cooldown,
            "cooldown": cooldown,
            "count": len(recent),
            "notional": notional,
            "longLiquidations": sell_notional,
            "shortLiquidations": buy_notional,
            "lastTs": recent[-1]["ts"] if recent else 0,
        }
