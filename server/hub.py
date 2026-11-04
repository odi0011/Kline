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
        self.market: list[str] = []       # 币安全部可交易 U 本位永续（前端币种菜单用）
        self.port = port
        self.clients: set = set()
        self.latest: dict[tuple, list] = {}
        self.dirty: set = set()
        # 结构分析待推送集合：只在收线重算后加入，避免每笔 tick 都重发整份结构
        self._structure_dirty: set = set()
        self.hit_queue: list[dict] = []
        self.mode = "binance"
        self.lag_p50 = None
        self._flush_count = 0
        self.on_detect = None             # 客户端下发检测周期时的回调（main 注入）
        self.on_quant_symbols = None      # 客户端下发量化交易范围时的回调（main 注入）
        self.stats_fn = None              # 形态统计（storage.pattern_stats）
        self.symbol_stats_fn = None       # 按币种统计（storage.symbol_stats）
        self.query_fn = None              # bar 记录查询（storage）
        self.last_accounts = None         # 最近一轮账户（实盘）数据（main 轮询写入）
        self.stats_dirty = False
        self.settings_fn = None           # 当前可编辑设置（main 注入）
        self.on_settings = None           # 客户端下发设置时的回调：应用+持久化，返回新设置
        self.ai_fn = None                 # AI 辅助（main 注入）：("list"|"test"|"guess", arg1, arg2) -> dict
        self.agent_state_fn = None        # ODDD 任务与会话快照（main 注入）
        self.agent_fn = None              # ODDD 异步动作：(action, payload) -> dict
        self.news_article_fn = None        # 旧接口：按需抓正文（已不使用，保留兼容）
        self.news_lookup_fn = None         # url -> 存档（含正文）
        self.news_enqueue_fn = None        # url -> None：把缺正文的新闻排进后台预取队列
        self.news_ai_fn = None             # url -> {text,state,ts}：已生成的 AI 分析
        self._article_cache: dict = {}
        self._article_slots = asyncio.Semaphore(2)
        self.quant_fn = None              # 量化快照（main 注入）
        self.quant_events_fn = None       # 决策流水查询（main 注入）：(acct, limit) -> list
        self.paper_refresh_fn = None      # 模拟仓手动刷新（main 注入）：symbol -> position
        self.analysis_fn = None           # 结构分析（main 注入）：(sym, iv, candles) -> dict
        self.depths: dict[str, dict] = {}      # symbol -> 订单簿深度快照（真实挂单）
        self.depth_dirty: set[str] = set()     # 待广播的深度
        self.risk_fn = None                    # 账户风险敞口（main 注入）：() -> dict
        self.trades_fn = None                  # 仓位历史查询（main 注入）：(acct, limit) -> list
        self.news: list[dict] = []             # 交易所公告缓存（main 定时刷新）
        self.structures: dict[tuple, dict] = {}   # (sym, iv) -> 最近一次结构分析
        self.derivatives: dict[str, dict] = {}    # symbol -> 币安衍生品指标快照
        self.derivatives_ts = 0                   # 该批快照的采集时刻
        self.derivatives_error = ""               # 最近一次取数失败原因（前端显示，便于排查）

    # ---- 聚合器回调（与 PatternEngine 并联） ----

    def on_update(self, symbol, interval, candle, closed, history):
        self.latest[(symbol, interval)] = self._bar(candle)
        self.dirty.add((symbol, interval))
        # 结构分析只在收线时重算（每根K线一次，避免逐笔重算）。
        # 重算后把 key 标记为"待推送"：结构只在收线时变，绝不能跟着每笔
        # tick 一起下发——那是整份形态/趋势线数据（~1.4KB），10 秒能把
        # 270KB 灌给每个客户端，是页面卡顿的元凶之一。
        if closed and self.analysis_fn:
            try:
                self.structures[(symbol, interval)] = self.analysis_fn(
                    symbol, interval, history[-160:])
                self._structure_dirty.add((symbol, interval))
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
        # [time, open, high, low, close, volume, buyVol, sellVol]
        # 后两个是主动买 / 主动卖成交量，供"买卖力量"面板使用
        return [c.open_time // 1000, c.open, c.high, c.low, c.close, round(c.volume, 4),
                round(getattr(c, "buy_vol", 0.0), 4), round(getattr(c, "sell_vol", 0.0), 4)]

    # 前端不读、但体积最大的三个字段（实测占快照 278KB 里的 ~137KB）：
    #   decisionTraces 89KB / latestTraces 33KB / gateLog 14.5KB
    # 它们是调试与回溯用的，前端没有任何地方读，每 5 秒全量推纯属浪费。
    # 注意 calcStream（68KB）必须保留：计算流水面板靠它实时滚动。
    _WIRE_ANALYSIS = ("symbols", "liveFeatures", "liveEvaluations", "liveScoreHistory",
                      "calcStream", "advice", "paper", "paperStats", "paperBySymbol", "positionPlans",
                      "params", "drawdown", "drift")
    _WIRE_TOP = ("enabled", "ts", "stats", "events", "positions", "trades", "accounts",
                 "unsupported")

    @classmethod
    def slim_quant(cls, snap: dict) -> dict:
        """广播用的量化快照：去掉调试字段，只留前端要画的。

        quant.snapshot() 仍返回完整版本（Agent 的 read_calc_trace /
        read_market 需要那些字段），这里只在"发给浏览器"这条路上裁剪。
        """
        if not isinstance(snap, dict):
            return snap
        out = {k: snap[k] for k in cls._WIRE_TOP if k in snap}
        analysis = snap.get("analysis")
        if isinstance(analysis, dict):
            out["analysis"] = {k: analysis[k] for k in cls._WIRE_ANALYSIS if k in analysis}
        elif "analysis" in snap:
            out["analysis"] = analysis      # 形状异常时原样带过去，不过度清洗
        return out

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

    async def _news_article(self, url: str) -> dict:
        """新闻正文 + 已生成的 AI 分析。

        分析在正文入库时就已经跑好并落库，这里只读，不触发新的模型调用。
        """
        # 只接受当前来源列表里的 URL：客户端不能拿任意字符串来探库。
        item = next((it for it in self.news if it.get("url") == url), None)
        if item is None:
            return {"ok": False, "error": "此新闻已不在当前来源列表，请刷新"}
        full = None
        if self.news_lookup_fn:
            try:
                full = await asyncio.to_thread(self.news_lookup_fn, url)
            except Exception:
                full = None
        if not full and item:
            full = {"content": item.get("content"), "desc": item.get("desc"),
                    "partial": item.get("contentPartial") is not False,
                    "title": item.get("title"), "source": item.get("source")}
        if not full:
            return {"ok": False, "error": "此新闻尚未存档，请稍后刷新"}

        analysis = None
        if self.news_ai_fn:
            try:
                analysis = await asyncio.to_thread(self.news_ai_fn, url)
            except Exception:
                analysis = None

        content = full.get("content")
        summary = full.get("desc") or ""
        if content:
            return {"ok": True, "content": content, "partial": bool(full.get("partial")),
                    "analysis": analysis,
                    "error": "" if not full.get("partial") else "仅摘要 / 部分正文，并非全文"}

        # 只有摘要：让后台队列补抓一次（不在本请求里联网）
        if self.news_enqueue_fn:
            try:
                self.news_enqueue_fn(url)
            except Exception:
                pass
        return {"ok": bool(summary), "content": summary, "partial": True,
                "pending": True, "analysis": analysis,
                "error": "正文仍在后台抓取，当前显示 RSS 摘要" if summary else "正文抓取中，请稍后重试"}

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
            "market": self.market, "intervals": self.intervals,
            "history": history, "structures": structures,
        }, separators=(",", ":"))

    def symbol_snapshot(self, sym: str) -> str:
        """单个币种的历史快照。运行时新增订阅后补发给所有客户端，
        让新卡片直接有历史K线，而不是从一根实时K线开始累积。"""
        history = {}
        structures = {}
        for iv in self.intervals:
            agg = self.aggs.get((sym, iv))
            bars = [self._bar(c) for c in list(agg.history)[-HISTORY_BARS:]] if agg else []
            if agg and agg.current is not None:
                bars.append(self._bar(agg.current))
            history[iv] = bars
            st = self.structures.get((sym, iv))
            if st:
                structures[iv] = st
        return json.dumps({
            "type": "bars", "symbol": sym, "intervals": self.intervals,
            "history": history, "structures": structures, "market": self.market,
        }, separators=(",", ":"))

    async def _handler(self, ws) -> None:
        self.clients.add(ws)
        last_article = 0.0
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
            if self.depths:
                await ws.send(json.dumps({"type": "depths", "depths": self.depths},
                                         separators=(",", ":")))
            if self.settings_fn:
                await ws.send(json.dumps({"type": "settings", "settings": self.settings_fn()},
                                         separators=(",", ":")))
            if self.quant_fn:
                await ws.send(json.dumps(
                    {"type": "quant", "snapshot": self.slim_quant(self.quant_fn())},
                    separators=(",", ":")))
            if self.agent_state_fn:
                await ws.send(json.dumps({"type": "agent_state", **self.agent_state_fn()},
                                         separators=(",", ":")))
            if self.trades_fn:
                try:
                    acct = self.last_accounts[0]["name"] if self.last_accounts else ""
                    await ws.send(json.dumps({
                        "type": "quant_trades", "trades": self.trades_fn(acct, 300),
                    }, separators=(",", ":")))
                except Exception:
                    pass
            if self.derivatives:
                await ws.send(json.dumps({"type": "derivatives", "rows": self.derivatives,
                                          "ts": self.derivatives_ts,
                                          "error": self.derivatives_error},
                                         separators=(",", ":")))
            if self.news:
                await ws.send(json.dumps({"type": "news", "items": self.news},
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
                request_id = ""
                try:
                    msg = json.loads(raw)
                    if not isinstance(msg, dict):
                        continue
                    mtype = msg.get("type")
                    request_id = str(msg.get("requestId") or "")[:80]
                    if mtype in ("agent_action", "ai_list_models", "ai_test_model", "ai_guess_meta"):
                        if not self._same_origin(ws):
                            await ws.send(json.dumps({"type": "agent_ack", "requestId": request_id,
                                                     "result": {"ok": False, "error": "拒绝跨源请求"}},
                                                    separators=(",", ":")))
                            continue
                    if mtype == "set_detect_interval" and self.on_detect:
                        if self._same_origin(ws):
                            self.on_detect(msg.get("interval"))
                    elif mtype == "set_quant_symbols" and self.on_quant_symbols:
                        if self._same_origin(ws):
                            syms = msg.get("symbols")
                            if isinstance(syms, list):
                                self.on_quant_symbols([str(x).lower() for x in syms if x])
                    elif mtype == "set_settings" and self.on_settings:
                        if not self._same_origin(ws):
                            print("[settings] 拒绝跨源 set_settings")
                            continue
                        settings = msg.get("settings") or {}
                        if not isinstance(settings, dict):
                            continue
                        print("[settings] 收到设置更新")
                        new_settings = self.on_settings(settings)
                        await ws.send(json.dumps({"type": "settings", "settings": new_settings},
                                                 separators=(",", ":")))
                    elif mtype == "ai_list_models" and self.ai_fn:
                        # 用服务端保存的密钥去问 /models（配合前端刚填的端点）
                        res = await asyncio.to_thread(
                            self.ai_fn, "list", msg.get("base") or "", msg.get("key") or "")
                        await ws.send(json.dumps({"type": "ai_models", "result": res},
                                                 separators=(",", ":")))
                    elif mtype == "ai_test_model" and self.ai_fn:
                        # 带上当前草稿的 base/key：用户没保存也能直接测
                        mid = msg.get("model") or ""
                        res = await asyncio.to_thread(
                            self.ai_fn, "test", mid,
                            msg.get("base") or "", msg.get("key") or "")
                        await ws.send(json.dumps(
                            {"type": "ai_test", "result": {**res, "id": mid}},
                            separators=(",", ":")))
                    elif mtype == "ai_guess_meta" and self.ai_fn:
                        res = await asyncio.to_thread(
                            self.ai_fn, "guess", msg.get("model") or "")
                        await ws.send(json.dumps({"type": "ai_meta", "result": res},
                                                 separators=(",", ":")))
                    elif mtype == "agent_action" and self.agent_fn:
                        action = str(msg.get("action") or "")
                        payload = msg.get("payload") or {}
                        if not isinstance(payload, dict):
                            res = {"ok": False, "error": "任务参数无效"}
                        else:
                            res = await self.agent_fn(action, payload)
                        await ws.send(json.dumps({"type": "agent_ack", "action": action,
                                                 "requestId": request_id, "result": res}, separators=(",", ":")))
                    elif mtype == "query_news_article":
                        # 只读存档：正文在新闻入库时/后台预取时就已抓取并落库。
                        # 这里绝不访问源站 —— 每次点开都去爬正是 HTTP 429 的根因。
                        url = str(msg.get("url") or "")[:512]
                        result = await self._news_article(url)
                        await ws.send(json.dumps({"type": "news_article", "requestId": request_id,
                                                 "result": result}, separators=(",", ":")))
                    elif mtype == "query_quant_trades" and self.trades_fn:
                        acct = str(msg.get("account") or "")
                        limit = int(msg.get("limit") or 300)
                        await ws.send(json.dumps({
                            "type": "quant_trades", "trades": self.trades_fn(acct, limit),
                        }, separators=(",", ":")))
                    elif mtype == "query_news":
                        await ws.send(json.dumps({"type": "news", "items": self.news},
                                                 separators=(",", ":")))
                    elif mtype == "query_depths":
                        await ws.send(json.dumps({
                            "type": "depths", "depths": self.depths,
                        }, separators=(",", ":")))
                    elif mtype == "paper_refresh" and self.paper_refresh_fn:
                        sym = str(msg.get("symbol") or "").lower()[:24]
                        result = await asyncio.to_thread(self.paper_refresh_fn, sym)
                        await ws.send(json.dumps({
                            "type": "paper_refreshed", "symbol": sym, "position": result,
                        }, separators=(",", ":")))
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
                    if request_id:
                        await ws.send(json.dumps({"type": "request_error", "requestId": request_id,
                                                 "result": {"ok": False, "error": "服务端处理失败，请重试"}},
                                                separators=(",", ":")))
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
                if self.depth_dirty and self.depths:
                    # 深度每 500ms 一推，合并成一条消息按 flush 周期下发（约 200ms 一次）
                    self.depth_dirty.clear()
                    await self._broadcast({"type": "depths", "depths": self.depths})
                if self.dirty:
                    dirty, self.dirty = self.dirty, set()
                    for key in dirty:
                        bar = self.latest.get(key)
                        if bar:
                            await self._broadcast({
                                "type": "candle", "symbol": key[0],
                                "interval": key[1], "bar": bar,
                            })
                # 结构分析单独一批：只在收线重算后下发（每根K线一次），
                # 不再跟着每笔 tick 重发整份形态/趋势线数据。
                if self._structure_dirty:
                    struct_dirty, self._structure_dirty = self._structure_dirty, set()
                    for key in struct_dirty:
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
