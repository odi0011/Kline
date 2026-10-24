from __future__ import annotations

import argparse
import asyncio
import sys
import threading
from collections import defaultdict
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import yaml

import patterns.builtin  # noqa: F401  导入即注册内置形态
import patterns.base as pbase
from core.aggregator import KlineAggregator
from core.engine import PatternEngine
from core.candle import INTERVAL_MS
from feed.backfill import fetch_klines
from feed.demo import DemoSource
from feed.source import BinanceFuturesStream
from notify.notifier import Notifier
from patterns.base import get_enabled
from server.hub import BroadcastHub
from storage import Storage
from accounts import fetch_account

def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def start_static(dist_dir: str, port: int) -> None:
    class Quiet(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=dist_dir, **kwargs)

        def log_message(self, *args):
            pass

        def end_headers(self):
            # index/icons 不缓存（部署后普通刷新即见新版）；带内容哈希的 assets 永久缓存
            if self.path.startswith("/assets/"):
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            else:
                self.send_header("Cache-Control", "no-cache")
            super().end_headers()

    httpd = ThreadingHTTPServer(("0.0.0.0", port), Quiet)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"[web] 页面 http://127.0.0.1:{port}")


async def run(cfg: dict) -> None:
    notifier = Notifier(cfg.get("notify", {}), cfg.get("csv_log"))

    pcfg = cfg.get("patterns", {})
    pbase.TREND_BARS = int(pcfg.get("trend_bars", 10))
    pbase.TREND_THRESHOLD = float(pcfg.get("trend_threshold", 0.002))
    enabled = get_enabled(pcfg.get("enabled", "all"))
    print(f"[engine] 已启用形态 {len(enabled)} 个")

    symbols = cfg["symbols"]
    intervals = cfg["intervals"]

    aggs: dict[tuple, KlineAggregator] = {}
    for sym in symbols:
        for iv in intervals:
            aggs[(sym, iv)] = KlineAggregator(sym, iv, None, None)

    hub = BroadcastHub(aggs, symbols, intervals, port=int(cfg.get("ws_port", 8765)))

    def _set_detect(iv):
        iv = iv if iv in intervals else None
        engine.detect_interval = iv
        if iv:
            print(f"[engine] 形态检测周期 -> {iv}")

    hub.on_detect = _set_detect

    # 存储层（MySQL）：K线滚动保留 + 形态命中记录/胜率
    scfg = cfg.get("storage", {})
    storage = Storage(cfg.get("mysql", {}), keep_minutes=int(scfg.get("minutes", 30)),
                      eval_minutes=int(scfg.get("eval_minutes", 10)),
                      eval_threshold=float(scfg.get("eval_threshold", 0.0015)))
    hub.stats_fn = storage.pattern_stats
    hub.symbol_stats_fn = storage.symbol_stats
    hub.query_fn = storage

    def notify_with_lag(hit):
        st = feed_box["feed"].lag_stats() if "feed" in feed_box else {}
        lag = int(st["p50"]) if st else None
        notifier(hit, lag)
        hub.on_hit(hit)
        storage.save_hit(hit)
        hub.stats_dirty = True          # 触发 pattern_stats 广播

    engine = PatternEngine(enabled, notify_with_lag,
                           live_preview=bool(pcfg.get("live_preview", True)))

    def on_evt(sym, iv, candle, closed, history):
        if closed:
            engine.on_candle(sym, iv, candle, True, history)
            storage.save_candle(sym, iv, candle)
            storage.eval_outcomes(sym, iv, INTERVAL_MS[iv], candle)
            storage.prune(KlineAggregator.now_ms())
        else:
            engine.on_candle(sym, iv, candle, False, history)
        hub.on_update(sym, iv, candle, closed, history)

    for agg in aggs.values():
        agg.on_update = on_evt
        agg.on_close = on_evt

    bcfg = cfg.get("feed", {})
    proxy_cfg = (bcfg.get("proxy") or "").strip()
    limit = int(bcfg.get("backfill_bars", 300))
    for (sym, iv), agg in aggs.items():
        try:
            candles = await asyncio.to_thread(fetch_klines, sym, iv, limit, proxy_cfg or None)
            agg.seed(candles)
            keep_from = KlineAggregator.now_ms() - int(scfg.get("minutes", 30)) * 60_000
            for c in candles:
                if c.open_time >= keep_from:
                    storage.save_candle(sym, iv, c)
            print(f"[backfill] {sym} {iv}: 回补 {len(candles)} 根")
        except Exception as e:
            print(f"[backfill] {sym} {iv} 失败: {e}（空历史启动）")

    by_symbol: dict[str, list[KlineAggregator]] = defaultdict(list)
    for (sym, _), agg in aggs.items():
        by_symbol[sym].append(agg)

    def on_trade(symbol: str, price: float, qty: float, ts: int, _recv_ms: int) -> None:
        for agg in by_symbol.get(symbol, ()):
            agg.on_trade(price, qty, ts)

    feed_box: dict = {}
    mode = (bcfg.get("mode") or "auto").lower()

    if mode == "demo":
        feed = DemoSource(symbols, on_trade)
        hub.mode = "demo"
    else:
        feed = BinanceFuturesStream(symbols, on_trade, proxy=proxy_cfg or True)
    feed_box["feed"] = feed

    await hub.start()

    dist = "web/dist"
    import os
    if os.path.isdir(dist):
        start_static(dist, int(cfg.get("web_port", 8000)))
    else:
        print(f"[web] 未找到 {dist}，前端未构建（npm run build 后自动可用）")

    async def sweep():
        while True:
            await asyncio.sleep(0.5)
            now = KlineAggregator.now_ms()
            for agg in aggs.values():
                agg.sweep(now)

    async def supervisor():
        if mode == "demo":
            await feed.run()
            return
        feed_task = asyncio.create_task(feed.run())
        await feed.demo_requested.wait()
        print("[feed] 已切换到演示模式（页面顶部会提示）")
        hub.mode = "demo"
        feed_box["feed"] = DemoSource(symbols, on_trade)
        await feed_box["feed"].run()

    print("K线形态识别器已启动，Ctrl+C 退出")

    # 测试盘账户权益轮询（30s），广播给前端
    async def accounts_poller():
        accts = cfg.get("accounts", [])
        if not accts:
            return
        while True:
            results = []
            for a in accts:
                try:
                    d = await asyncio.to_thread(fetch_account, a["api_key"], a["api_secret"])
                    results.append({"name": a["name"], "icon": a.get("icon", ""),
                                    "balance": d["balance"], "unrealized": d["unrealized"],
                                    "available": d["available"], "positions": d.get("positions", [])})
                except Exception as e:
                    results.append({"name": a["name"], "icon": a.get("icon", ""),
                                    "balance": None, "unrealized": None, "available": None,
                                    "positions": [], "error": str(e)[:80]})
            try:
                hub.last_accounts = results
                await hub.broadcast({"type": "accounts", "accounts": results})
                print(f"[accounts] {len(results)} 个测试盘账户已广播: " +
                      ", ".join(f"{r['name']}={r['balance']}" for r in results))
            except Exception:
                pass
            await asyncio.sleep(30)

    await asyncio.gather(supervisor(), sweep(), accounts_poller())


def main() -> None:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="币安合约K线形态识别器（网页版）")
    ap.add_argument("-c", "--config", default="config.yaml")
    args = ap.parse_args()
    cfg = load_config(args.config)
    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
