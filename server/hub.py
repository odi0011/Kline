"""WS 广播服务：把实时K线、形态命中、运行状态推给浏览器。"""
from __future__ import annotations

import asyncio
import json
import time

import websockets

FLUSH_SEC = 0.2
HISTORY_BARS = 200


class BroadcastHub:
    def __init__(self, aggs: dict, symbols: list[str], intervals: list[str], port: int = 8765):
        self.aggs = aggs                  # (symbol, interval) -> KlineAggregator
        self.symbols = symbols
        self.intervals = intervals
        self.port = port
        self.clients: set = set()
        self.latest: dict[tuple, list] = {}
        self.dirty: set = set()
        self.hit_queue: list[dict] = []
        self.mode = "binance"
        self.lag_p50 = None
        self._flush_count = 0
        self.on_detect = None             # 客户端下发检测周期时的回调（main 注入）
        self.stats_fn = None              # 形态统计（storage.pattern_stats）
        self.symbol_stats_fn = None       # 按币种统计（storage.symbol_stats）
        self.query_fn = None              # bar 记录查询（storage）
        self.last_accounts = None         # 最近一轮测试盘账户数据（main 轮询写入）
        self.stats_dirty = False

    # ---- 聚合器回调（与 PatternEngine 并联） ----

    def on_update(self, symbol, interval, candle, closed, history):
        self.latest[(symbol, interval)] = self._bar(candle)
        self.dirty.add((symbol, interval))

    on_close = on_update

    def on_hit(self, hit) -> None:
        self.hit_queue.append({
            "type": "hit", "symbol": hit.symbol, "interval": hit.interval,
            "pattern": hit.pattern, "name_zh": hit.name_zh,
            "direction": hit.direction, "mode": hit.mode,
            "price": hit.price, "ts": hit.ts,
        })

    @staticmethod
    def _bar(c) -> list:
        return [c.open_time // 1000, c.open, c.high, c.low, c.close, round(c.volume, 4)]

    # ---- WS 服务 ----

    async def start(self) -> None:
        server = await websockets.serve(self._handler, "0.0.0.0", self.port, max_size=2**22)
        print(f"[ws] 行情推送 ws://127.0.0.1:{self.port}")
        asyncio.create_task(self._flusher())
        return server

    def _snapshot(self) -> str:
        history = {}
        for sym in self.symbols:
            history[sym] = {}
            for iv in self.intervals:
                agg = self.aggs.get((sym, iv))
                bars = [self._bar(c) for c in list(agg.history)[-HISTORY_BARS:]] if agg else []
                if agg and agg.current is not None:
                    bars.append(self._bar(agg.current))
                history[sym][iv] = bars
        return json.dumps({
            "type": "init", "mode": self.mode, "symbols": self.symbols,
            "intervals": self.intervals, "history": history,
        }, separators=(",", ":"))

    async def _handler(self, ws) -> None:
        self.clients.add(ws)
        print(f"[ws] 客户端接入 ({len(self.clients)} 个在线)")
        try:
            await ws.send(self._snapshot())
            if self.stats_fn:
                await ws.send(json.dumps({"type": "pattern_stats", "stats": self.stats_fn()},
                                         separators=(",", ":")))
            if self.symbol_stats_fn:
                await ws.send(json.dumps({"type": "symbol_stats", "stats": self.symbol_stats_fn()},
                                         separators=(",", ":")))
            if self.last_accounts:
                await ws.send(json.dumps({"type": "accounts", "accounts": self.last_accounts},
                                         separators=(",", ":")))
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                    mtype = msg.get("type")
                    if mtype == "set_detect_interval" and self.on_detect:
                        self.on_detect(msg.get("interval"))
                    elif mtype == "query_bar" and self.query_fn:
                        sym, iv = msg.get("symbol"), msg.get("interval")
                        ot = int(msg.get("open_time") or 0)
                        await ws.send(json.dumps({
                            "type": "bar_detail", "symbol": sym, "interval": iv, "open_time": ot,
                            "hits": self.query_fn.hits_for_bar(sym, iv, ot),
                            "candle": self.query_fn.candle_record(sym, iv, ot),
                        }, separators=(",", ":")))
                except Exception:
                    import traceback
                    traceback.print_exc()
        except Exception:
            import traceback
            print("[ws] 连接异常关闭:")
            traceback.print_exc()
        finally:
            self.clients.discard(ws)
            print(f"[ws] 客户端断开 ({len(self.clients)} 个在线)")

    async def _flusher(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_SEC)
            try:
                if self.stats_dirty and self.stats_fn:
                    self.stats_dirty = False
                    await self._broadcast({"type": "pattern_stats", "stats": self.stats_fn()})
                    if self.symbol_stats_fn:
                        await self._broadcast({"type": "symbol_stats", "stats": self.symbol_stats_fn()})
                for msg in self.hit_queue:
                    await self._broadcast(msg)
                self.hit_queue.clear()
                if self.dirty:
                    dirty, self.dirty = self.dirty, set()
                    for key in dirty:
                        bar = self.latest.get(key)
                        if bar:
                            await self._broadcast({
                                "type": "candle", "symbol": key[0],
                                "interval": key[1], "bar": bar,
                            })
                self._flush_count += 1
                if self._flush_count % 25 == 0:   # 约 5 秒一次状态
                    await self._broadcast({
                        "type": "stats", "mode": self.mode,
                        "lag_p50": self.lag_p50, "clients": len(self.clients),
                    })
            except Exception as e:
                print(f"[ws] flush 异常: {e}")

    async def broadcast(self, obj: dict) -> None:
        await self._broadcast(obj)

    async def _broadcast(self, obj: dict) -> None:
        if not self.clients:
            return
        data = json.dumps(obj, separators=(",", ":"))
        results = await asyncio.gather(
            *(c.send(data) for c in list(self.clients)), return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                pass
