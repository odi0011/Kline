from __future__ import annotations

import argparse
import asyncio
import os
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
from quant import QuantEngine
from server.hub import BroadcastHub
from storage import Storage
from accounts import fetch_account

MASK_SEC = "••••••••"
MASK_KEY = "····"


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def start_static(dist_dir: str, port: int) -> None:
    class Quiet(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=dist_dir, **kwargs)

        def log_message(self, *args):
            pass

        def translate_path(self, path):
            # 畸形/恶意路径（含非法转义、NUL 字节等）会让标准实现抛 ValueError，
            # 这里兜底为根路径，避免异常日志污染并防止目录穿越
            try:
                p = super().translate_path(path)
            except Exception:
                return os.path.join(dist_dir, "index.html")
            base = os.path.abspath(dist_dir)
            if not os.path.abspath(p).startswith(base):
                return os.path.join(base, "index.html")
            return p

        def handle_one_request(self):
            try:
                super().handle_one_request()
            except (ValueError, AttributeError, ConnectionError):
                self.close_connection = True

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


async def run(cfg: dict, config_path: str = "config.yaml") -> None:
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
        if hit.mode == "confirmed":
            # 量化引擎里的交易所调用是同步 HTTP，放线程池执行，避免卡住K线主事件循环
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, quant.on_signal,
                                 {"symbol": hit.symbol, "direction": hit.direction,
                                  "pattern": hit.pattern, "name_zh": hit.name_zh})

    engine = PatternEngine(enabled, notify_with_lag,
                           live_preview=bool(pcfg.get("live_preview", True)))

    # 账户列表可变盒子：设置弹窗改后热生效；refresh 标志触发立即拉取。
    # 单账户设计：config 里多余的账户在启动时裁掉（页面只展示一个账户卡）。
    accts_box = {"list": [dict(a) for a in cfg.get("accounts", [])][:1], "refresh": False}
    if len(cfg.get("accounts", [])) > 1:
        cfg["accounts"] = accts_box["list"]
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
        print(f"[settings] 单账户模式：已裁剪为 1 个账户并写回配置")

    def save_config() -> None:
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
            print(f"[settings] 配置已写入 {config_path}")
        except Exception as e:
            print(f"[settings] 配置写入失败: {e}")

    def _mask_key(k: str) -> str:
        k = k or ""
        return (k[:6] + MASK_KEY + k[-4:]) if len(k) > 12 else (MASK_SEC if k else "")

    def current_settings() -> dict:
        # 凭据绝不出网：settings 会广播给所有 WS 客户端（端口在公网），
        # 只下发掩码；前端回传掩码时由 apply_settings 换回服务端保存的原值。
        return {
            "accounts": [{"name": a.get("name", ""), "icon": a.get("icon", ""),
                          "api_key": _mask_key(a.get("api_key", "")),
                          "api_secret": MASK_SEC if a.get("api_secret") else ""}
                         for a in accts_box["list"]],
            "patterns": {"enabled": [p.key for p in engine.patterns],
                         "live_preview": engine.live_preview,
                         "all": list(pbase.all_patterns().keys())},
            "quant": {"enabled": quant.enabled},
            "ui": cfg.get("ui", {}),
        }

    def apply_settings(s: dict) -> dict:
        """应用设置弹窗下发的变更：运行时热生效 + 持久化到 config.yaml。"""
        accounts = s.get("accounts")
        if isinstance(accounts, list):
            prev = {a.get("name"): a for a in accts_box["list"]}
            # 单账户：改名后名称对不上，掩码需回退到原唯一账户，否则密钥被当成空值丢弃
            prev_one = accts_box["list"][0] if len(accts_box["list"]) == 1 else None
            clean = []
            for a in accounts[:1]:                       # 只保留一个账户
                if not isinstance(a, dict):
                    continue
                name = str(a.get("name", "")).strip()[:40]
                if not name:
                    continue
                key = str(a.get("api_key", "")).strip()
                secret = str(a.get("api_secret", "")).strip()
                # 掩码表示"沿用服务端原值"（前端从不持有明文凭据）
                if MASK_KEY in key or key == MASK_SEC:
                    src = prev.get(name) or prev_one or {}
                    key = str(src.get("api_key", "") or "").strip()
                if MASK_SEC in secret:
                    src = prev.get(name) or prev_one or {}
                    secret = str(src.get("api_secret", "") or "").strip()
                if not key or not secret:
                    continue
                clean.append({"name": name, "api_key": key, "api_secret": secret,
                              "icon": str(a.get("icon", ""))[:300000]})
            accts_box["list"] = clean
            accts_box["refresh"] = True          # 触发账户数据立即重新拉取
            cfg["accounts"] = clean
        patterns = s.get("patterns")
        if isinstance(patterns, dict):
            enabled = patterns.get("enabled")
            if isinstance(enabled, list) and enabled:
                valid = [k for k in enabled if k in pbase.all_patterns()]
                if valid:
                    engine.patterns = get_enabled(valid)   # 运行时热替换启用形态
                    cfg.setdefault("patterns", {})["enabled"] = valid
            if "live_preview" in patterns:
                engine.live_preview = bool(patterns["live_preview"])
                cfg.setdefault("patterns", {})["live_preview"] = bool(patterns["live_preview"])
        if "quant" in s and isinstance(s["quant"], dict):
            quant.set_enabled(bool(s["quant"].get("enabled", True)))
            cfg.setdefault("quant", {})["enabled"] = quant.enabled
        ui = s.get("ui")
        if isinstance(ui, dict):
            cur = cfg.setdefault("ui", {})
            for k in ("themeColor", "font", "upDown", "upColor", "downColor"):
                if isinstance(ui.get(k), str) and ui[k]:
                    cur[k] = ui[k][:64]
        save_config()
        print("[settings] 设置已应用并持久化")
        return current_settings()

    hub.settings_fn = current_settings
    hub.on_settings = apply_settings

    # 量化引擎：确认信号驱动开仓、5s 持仓监控、5 分钟自审计、全程记录
    iv0 = intervals[0]

    def _price_fn(symbol):
        agg = aggs.get((symbol, iv0))
        if not agg:
            return None
        if agg.current is not None:
            return agg.current.close
        hist = list(agg.history)
        return hist[-1].close if hist else None

    qcfg = cfg.get("quant", {})
    quant = QuantEngine(accts_box, config_path, price_fn=_price_fn,
                        min_pattern_winrate=float(qcfg.get("min_pattern_winrate", 0.35)),
                        min_pattern_samples=int(qcfg.get("min_pattern_samples", 5)))
    quant.enabled = bool(qcfg.get("enabled", True))
    quant.pattern_stats_fn = storage.pattern_stats
    # 量化引擎在线程池中运行；广播用 call_soon_threadsafe 回到事件循环（线程安全）
    _loop = asyncio.get_running_loop()
    quant.broadcast_fn = lambda obj: _loop.call_soon_threadsafe(
        lambda: asyncio.create_task(hub.broadcast(obj)))
    hub.quant_fn = quant.snapshot

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
    if os.path.isdir(dist):
        start_static(dist, int(cfg.get("web_port", 8000)))
    else:
        print(f"[web] 未找到 {dist}，前端未构建（npm run build 后自动可用）")

    async def sweep():
        qtick = 0
        while True:
            await asyncio.sleep(0.5)
            now = KlineAggregator.now_ms()
            for agg in aggs.values():
                agg.sweep(now)
            try:
                await asyncio.to_thread(quant.manage)    # 止损/止盈监控（每 5 秒，线程池内不卡主循环）
                qtick += 1
                if qtick % 10 == 0:                  # 约 5 秒广播一次量化快照
                    asyncio.create_task(hub.broadcast({"type": "quant", "snapshot": quant.snapshot()}))
                await asyncio.to_thread(quant.audit)  # 5 分钟自审计（引擎内部自限流）
            except Exception as e:
                print(f"[quant] 运行异常: {e}")

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

    # 账户权益轮询（30s；设置变更时立即刷新），广播给前端。
    # 每次采样落库，广播时附带最近 8 小时的权益曲线（服务端持久化：
    # 前端刷新/换设备都能立刻看到真实历史，不必从空白开始累积）。
    async def accounts_poller():
        while True:
            waited = 0.0
            while waited < 30 and not accts_box.get("refresh"):
                await asyncio.sleep(0.5)
                waited += 0.5
            accts_box["refresh"] = False
            results = []
            for a in accts_box["list"]:
                try:
                    d = await asyncio.to_thread(fetch_account, a["api_key"], a["api_secret"])
                    row = {"name": a["name"], "icon": a.get("icon", ""),
                           "balance": d["balance"], "unrealized": d["unrealized"],
                           "available": d["available"], "positions": d.get("positions", [])}
                    try:
                        storage.save_equity(a["name"], d["ts"], d["balance"], d["unrealized"])
                    except Exception as e:
                        print(f"[accounts] 权益采样落库失败: {e}")
                    try:
                        row["series"] = storage.equity_series(a["name"])
                    except Exception:
                        row["series"] = []
                    results.append(row)
                except Exception as e:
                    row = {"name": a["name"], "icon": a.get("icon", ""),
                           "balance": None, "unrealized": None, "available": None,
                           "positions": [], "error": str(e)[:80]}
                    try:
                        row["series"] = storage.equity_series(a["name"])
                    except Exception:
                        row["series"] = []
                    results.append(row)
            try:
                hub.last_accounts = results
                await hub.broadcast({"type": "accounts", "accounts": results})
                print(f"[accounts] {len(results)} 个账户已广播: " +
                      ", ".join(f"{r['name']}={r['balance']}" for r in results))
            except Exception:
                pass

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
        asyncio.run(run(cfg, args.config))
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
