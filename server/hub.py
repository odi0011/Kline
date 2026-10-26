"""WS 广播服务：把实时K线、形态命中、运行状态推给浏览器。"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import websockets

FLUSH_SEC = 0.2
HISTORY_BARS = 200

# 端口暴露在公网，扫描器/误连的 HTTP 客户端会反复触发握手异常。
# 这些是预期内的客户端问题，只留一行摘要，不写 traceback，否则真正的故障会被刷没。
_EXPECTED_WS_ERRORS = (
    "InvalidUpgrade", "InvalidMessage", "InvalidStatus", "InvalidHandshake",
    "NegotiationError", "ConnectionClosed", "ConnectionClosedOK", "ConnectionClosedError",
)


class _WSErrorNoiseFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        if exc is None or type(exc).__name__ not in _EXPECTED_WS_ERRORS:
            return True
        record.exc_info = None
        record.exc_text = None
        record.msg = f"{record.msg} ({type(exc).__name__})"
        return True


def _is_expected_ws_error(exc: BaseException) -> bool:
    return type(exc).__name__ in _EXPECTED_WS_ERRORS


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
        self.settings_fn = None           # 当前可编辑设置（main 注入）
        self.on_settings = None           # 客户端下发设置时的回调：应用+持久化，返回新设置
        self.quant_fn = None              # 量化快照（main 注入）
        self.quant_events_fn = None       # 决策流水查询（main 注入）：(acct, limit) -> list
        self.analysis_fn = None           # 结构分析（main 注入）：(sym, iv, candles) -> dict
        self.structures: dict[tuple, dict] = {}   # (sym, iv) -> 最近一次结构分析

    # ---- 聚合器回调（与 PatternEngine 并联） ----

    def on_update(self, symbol, interval, candle, closed, history):
        self.latest[(symbol, interval)] = self._bar(candle)
        self.dirty.add((symbol, interval))
        # 结构分析只在收线时重算（每根K线一次，避免逐笔重算）
        if closed and self.analysis_fn:
            try:
                self.structures[(symbol, interval)] = self.analysis_fn(
                    symbol, interval, history[-160:])
            except Exception:
                pass

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

    @staticmethod
    def _same_origin(ws) -> bool:
        """写操作只接受同源浏览器连接（防第三方页面跨站改配置/停量化）。
        无 Origin（非浏览器客户端）或无法解析时放行——真正的身份认证不在本层。"""
        try:
            req = getattr(ws, "request", None)
            headers = req.headers if req is not None else {}
            origin = headers.get("Origin")
            host = headers.get("Host", "")
            if not origin:
                return True
            from urllib.parse import urlsplit
            o = urlsplit(origin)
            o_host = (o.hostname or "").lower()
            h_host = (host.split(":")[0] or "").lower()
            return bool(o_host) and o_host == h_host
        except Exception:
            return True

    async def start(self) -> None:
        logger = logging.getLogger("websockets.server")
        if not any(isinstance(f, _WSErrorNoiseFilter) for f in logger.filters):
            logger.addFilter(_WSErrorNoiseFilter())
        server = await websockets.serve(
            self._handler, "0.0.0.0", self.port, max_size=2**22, logger=logger,
        )
        print(f"[ws] 行情推送 ws://127.0.0.1:{self.port}")
        asyncio.create_task(self._flusher())
        return server

    def _snapshot(self) -> str:
        history = {}
        structures = {}
        for sym in self.symbols:
            history[sym] = {}
            structures[sym] = {}
            for iv in self.intervals:
                agg = self.aggs.get((sym, iv))
                bars = [self._bar(c) for c in list(agg.history)[-HISTORY_BARS:]] if agg else []
                if agg and agg.current is not None:
                    bars.append(self._bar(agg.current))
                history[sym][iv] = bars
                st = self.structures.get((sym, iv))
                if st:
                    structures[sym][iv] = st
        return json.dumps({
            "type": "init", "mode": self.mode, "symbols": self.symbols,
            "intervals": self.intervals, "history": history, "structures": structures,
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
            if self.settings_fn:
                await ws.send(json.dumps({"type": "settings", "settings": self.settings_fn()},
                                         separators=(",", ":")))
            if self.quant_fn:
                await ws.send(json.dumps({"type": "quant", "snapshot": self.quant_fn()},
                                         separators=(",", ":")))
            if self.quant_events_fn:
                try:
                    acct = self.last_accounts[0]["name"] if self.last_accounts else ""
                    await ws.send(json.dumps({
                        "type": "quant_events",
                        "events": self.quant_events_fn(acct, 2000),
                    }, separators=(",", ":")))
                except Exception:
                    pass
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                    mtype = msg.get("type")
                    if mtype == "set_detect_interval" and self.on_detect:
                        if self._same_origin(ws):
                            self.on_detect(msg.get("interval"))
                    elif mtype == "set_settings" and self.on_settings:
                        if not self._same_origin(ws):
                            print("[settings] 拒绝跨源 set_settings")
                            continue
                        print(f"[settings] 收到 set_settings: {str(msg.get('settings'))[:150]}")
                        new_settings = self.on_settings(msg.get("settings") or {})
                        await ws.send(json.dumps({"type": "settings", "settings": new_settings},
                                                 separators=(",", ":")))
                    elif mtype == "query_quant_events" and self.quant_events_fn:
                        acct = str(msg.get("account") or "")
                        limit = int(msg.get("limit") or 2000)
                        await ws.send(json.dumps({
                            "type": "quant_events", "events": self.quant_events_fn(acct, limit),
                        }, separators=(",", ":")))
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
        except Exception as e:
            if _is_expected_ws_error(e):
                print(f"[ws] 客户端连接结束: {type(e).__name__}")
            else:
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
                        st = self.structures.get(key)
                        if st:
                            await self._broadcast({
                                "type": "structure", "symbol": key[0],
                                "interval": key[1], "structure": st,
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
