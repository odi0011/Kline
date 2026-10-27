from __future__ import annotations

import argparse
import asyncio
import os
import sys
import threading
import uuid
from collections import defaultdict
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import yaml

import patterns.builtin    # noqa: F401  导入即注册内置形态（单根/双根/三根）
import patterns.structure  # noqa: F401  导入即注册结构形态（头肩/三重/双顶底/三角/楔形）
import patterns.base as pbase
from core.aggregator import KlineAggregator
from core.engine import PatternEngine
from core.candle import INTERVAL_MS
from feed.backfill import fetch_klines
from feed.demo import DemoSource
from feed.depth import DepthStream
from feed.news import fetch_all as fetch_news
from feed.source import BinanceFuturesStream
from notify.notifier import Notifier
from patterns.base import get_enabled
from analysis.structure import analyze as analyze_structure
from quant import QuantEngine
from server.hub import BroadcastHub
from storage import Storage
from accounts import fetch_account

MASK_SEC = "••••••••"
MASK_KEY = "····"


# 空白部署（全新机器、仓库里没有 config.yaml）时的默认配置。
# 没有它 load_config 会 FileNotFoundError，服务起不来——这是"部署空白项目就崩"的根因。
DEFAULT_SYMBOLS = [
    "btcusdt", "tslausdt", "xauusdt", "xagusdt",
    "ethusdt", "bnbusdt", "solusdt", "xrpusdt", "dogeusdt", "adausdt",
]
DEFAULT_CONFIG: dict = {
    "web_port": 8000,
    "ws_port": 8765,
    "symbols": list(DEFAULT_SYMBOLS),
    "intervals": ["1m", "3m"],
    "feed": {"mode": "auto", "proxy": "", "backfill_bars": 300},
    "patterns": {"enabled": "all", "live_preview": True},
    "storage": {"minutes": 30, "eval_minutes": 10},
    "accounts": [],
    "quant": {"enabled": True},
    "notify": {},
}


def load_config(path: str) -> dict:
    """读配置。文件缺失时自动生成一份默认配置并落盘，保证空白部署开箱即用。"""
    if not os.path.exists(path):
        print(f"[config] 未找到 {path}，自动生成默认配置（{len(DEFAULT_SYMBOLS)} 个币种）")
        try:
            with open(path, "w", encoding="utf-8") as f:
                yaml.safe_dump(DEFAULT_CONFIG, f, allow_unicode=True, sort_keys=False)
        except Exception as e:
            print(f"[config] 默认配置写入失败（将使用内存配置运行）: {e}")
        return {k: (v.copy() if isinstance(v, (dict, list)) else v)
                for k, v in DEFAULT_CONFIG.items()}
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    # 补齐缺失的关键项（老配置/手工精简配置也能跑）
    for k, v in DEFAULT_CONFIG.items():
        if k not in cfg or cfg[k] in (None, "", [], {}):
            cfg[k] = v.copy() if isinstance(v, (dict, list)) else v
            print(f"[config] 缺失项 {k} 已用默认值补齐")
    return cfg


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

    symbols = [str(x).lower() for x in (cfg.get("symbols") or DEFAULT_SYMBOLS)]
    intervals = [str(x) for x in (cfg.get("intervals") or ["1m", "3m"])]
    if not symbols:
        symbols = list(DEFAULT_SYMBOLS)
    if not intervals:
        intervals = ["1m", "3m"]
    print(f"[config] 监控 {len(symbols)} 个币种 / 周期 {intervals}")

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
    # 旧配置没有 id：补一个稳定 ID（权益曲线按 id 存，改名不会丢历史）。
    if accts_box["list"] and not accts_box["list"][0].get("id"):
        accts_box["list"][0]["id"] = uuid.uuid4().hex[:12]
        cfg["accounts"] = accts_box["list"]
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
    # 认领历史采样：把按账户名（旧版）或旧 ID 存下的数据搬到当前 ID 上。
    # 每次都跑（一次 GROUP BY + 少量迁移），这样改名发生在旧版本、当时没留下
    # id 关联的情况也能自动修好，曲线不会因为改名"重置"。
    if accts_box["list"]:
        acc0 = accts_box["list"][0]
        try:
            moved = storage.migrate_equity([acc0.get("name", "")], acc0["id"])
            if moved:
                print(f"[equity] 迁移 {moved} 条按名称存的历史采样 -> {acc0['name']}")
            adopted = storage.adopt_orphan_equity(acc0["id"], [acc0.get("name", "")])
            if adopted:
                print(f"[equity] 认领孤儿历史 {adopted} 条 -> {acc0['name']}")
        except Exception as e:
            print(f"[equity] 历史迁移失败: {e}")
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
            "accounts": [{"id": a.get("id", ""), "name": a.get("name", ""), "icon": a.get("icon", ""),
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
                # 沿用原账户 ID：权益曲线按 id 存，改名不会丢历史。
                # 前端会带回 id；没带回时按同名/原唯一账户继承，最后才新生成。
                old = prev.get(name) or prev_one or {}
                acct_id = (str(a.get("id") or "").strip()
                           or str(old.get("id") or "") or uuid.uuid4().hex[:12])
                clean.append({"id": acct_id, "name": name, "api_key": key, "api_secret": secret,
                              "icon": str(a.get("icon", ""))[:300000]})
            accts_box["list"] = clean
            accts_box["refresh"] = True          # 触发账户数据立即重新拉取
            cfg["accounts"] = clean
            # 旧数据可能按账户名存（升级前）或按旧 ID 存，统一搬到新 ID 上，
            # 否则改名/升级后查不到历史，曲线看起来像被"重置"。
            for a in clean:
                migrate_from = []
                for old in prev.values():
                    old_id = str(old.get("id") or "")
                    if old_id and old_id != a["id"]:
                        migrate_from.append(old_id)
                    if old.get("name") and old["name"] != a["name"]:
                        migrate_from.append(old["name"])
                # 兼容升级前只存在自己名字下的情况
                if a["name"] not in migrate_from:
                    migrate_from.append(a["name"])
                try:
                    moved = storage.migrate_equity(migrate_from, a["id"])
                    if moved:
                        print(f"[equity] 迁移 {moved} 条历史采样 -> {a['name']}")
                except Exception as e:
                    print(f"[equity] 历史迁移失败: {e}")
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
    # 假数据防护：demo 模式下引擎拒绝一切新开仓（形态来自随机游走，没有交易意义）
    quant.feed_is_live_fn = lambda: hub.mode != "demo"
    quant.pattern_stats_fn = storage.pattern_stats
    # 决策流水落库：引擎线程内同步写 MySQL（连接已加锁，开销可忽略）
    quant.storage_fn = storage.save_quant_event
    quant.trade_storage_fn = storage.save_quant_trade

    # 指标计算用的K线源：优先检测周期，回落到第一个周期
    def _bars_for(symbol: str):
        iv = engine.detect_interval or iv0
        agg = aggs.get((symbol, iv)) or aggs.get((symbol, iv0))
        if not agg:
            return []
        hist = list(agg.history)
        if agg.current is not None:
            hist.append(agg.current)
        return hist[-200:]

    quant.bars_fn = _bars_for

    # 回撤熔断的数据源：当前账户的权益采样（最近 24 小时）
    def _equity_values():
        for a in accts_box["list"]:
            key = a.get("id") or a.get("name")
            try:
                rows = storage.equity_series(key, minutes=24 * 60, limit=2880)
                if rows:
                    return [r["bal"] for r in rows]
            except Exception:
                continue
        return []

    quant.equity_fn = _equity_values
    # 量化引擎在线程池中运行；广播用 call_soon_threadsafe 回到事件循环（线程安全）
    _loop = asyncio.get_running_loop()
    quant.broadcast_fn = lambda obj: _loop.call_soon_threadsafe(
        lambda: asyncio.create_task(hub.broadcast(obj)))
    hub.quant_fn = quant.snapshot
    # 量化交易范围跟随前端实际展示的币种：避免在用户没看K线的币上开仓。
    # 空列表不做限制（前端还没连上时保持原有行为）。
    def _set_quant_symbols(syms):
        valid = [s for s in syms if s in symbols]
        if valid:
            quant.symbol_filter = set(valid)
            print(f"[quant] 交易范围更新: {', '.join(valid)}")
    hub.on_quant_symbols = _set_quant_symbols
    # 决策流水查询：前端按账户拉历史（持久化，重启/刷新不清空）
    hub.quant_events_fn = storage.quant_events
    hub.trades_fn = storage.quant_trades

    # 结构分析（支撑压力/趋势线/FVG/订单块/结构形态几何）：收线时由 hub 触发
    def _analyze(sym, iv, candles):
        return analyze_structure(candles)

    hub.analysis_fn = _analyze

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
            # 启动即算一次结构，否则要等到本周期收线（最长几分钟）画面上才有线
            try:
                hub.structures[(sym, iv)] = analyze_structure(list(agg.history)[-160:])
            except Exception:
                pass
            print(f"[backfill] {sym} {iv}: 回补 {len(candles)} 根")
        except Exception as e:
            print(f"[backfill] {sym} {iv} 失败: {e}（空历史启动）")

    by_symbol: dict[str, list[KlineAggregator]] = defaultdict(list)
    for (sym, _), agg in aggs.items():
        by_symbol[sym].append(agg)

    def on_trade(symbol: str, price: float, qty: float, ts: int, _recv_ms: int,
                 buyer_maker: bool = False) -> None:
        for agg in by_symbol.get(symbol, ()):
            agg.on_trade(price, qty, ts, buyer_maker)

    # 订单簿深度（真实挂单）：独立 /public 路由，与 aggTrade 并行
    depth_box: dict = {"stream": None}

    def on_depth(symbol: str, snap: dict) -> None:
        hub.depths[symbol] = snap
        hub.depth_dirty.add(symbol)

    if not bcfg.get("depth_disabled", False):
        try:
            depth_box["stream"] = DepthStream(
                symbols, on_depth, proxy=proxy_cfg or True,
                levels=int(bcfg.get("depth_levels", 20)),
                speed_ms=int(bcfg.get("depth_speed_ms", 500)),
            )
        except Exception as e:
            print(f"[depth] 初始化失败: {e}")

    feed_box: dict = {}
    mode = (bcfg.get("mode") or "auto").lower()

    if mode == "demo":
        feed = DemoSource(symbols, on_trade)
        hub.mode = "demo"
    else:
        feed = BinanceFuturesStream(symbols, on_trade, proxy=proxy_cfg or True)
    feed_box["feed"] = feed

    # 深度流必须在 mode 确定之后再启动（demo 模式没有真实挂单）
    if mode != "demo" and depth_box["stream"]:
        asyncio.create_task(depth_box["stream"].run())
        print(f"[depth] 订单簿深度流已启动（{len(symbols)} 个币）")

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
        # 行情断开/停流一律重连，绝不自动切换演示数据：
        # 假行情会驱动形态引擎产生假信号 → 量化在测试盘下真单 + 污染胜率统计。
        # 演示模式只能来自 config 的 feed.mode: demo（显式选择）。
        await feed.run()

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
                    # 权益曲线按稳定 ID 存（改名不丢历史），没有 id 时退回旧行为用名称
                    eq_key = a.get("id") or a["name"]
                    row = {"name": a["name"], "id": a.get("id", ""), "icon": a.get("icon", ""),
                           "balance": d["balance"], "unrealized": d["unrealized"],
                           "available": d["available"], "positions": d.get("positions", [])}
                    try:
                        storage.save_equity(eq_key, d["ts"], d["balance"], d["unrealized"])
                    except Exception as e:
                        print(f"[accounts] 权益采样落库失败: {e}")
                    try:
                        row["series"] = storage.equity_series(eq_key)
                    except Exception:
                        row["series"] = []
                    results.append(row)
                except Exception as e:
                    row = {"name": a["name"], "id": a.get("id", ""), "icon": a.get("icon", ""),
                           "balance": None, "unrealized": None, "available": None,
                           "positions": [], "error": str(e)[:80]}
                    try:
                        row["series"] = storage.equity_series(a.get("id") or a["name"])
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

    # 历史回补的重试任务：启动时若某些币回补失败（网络抖动/代理未就绪），
    # 会一直是空历史、只能靠实时成交慢慢累积。这里每 90 秒补一次，直到够了为止。
    async def backfill_retry():
        await asyncio.sleep(20)
        while True:
            need = [(sym, iv, agg) for (sym, iv), agg in aggs.items()
                    if len(agg.history) < 60]
            for sym, iv, agg in need:
                try:
                    candles = await asyncio.to_thread(
                        fetch_klines, sym, iv, limit, proxy_cfg or None)
                    if not candles:
                        continue
                    # 清掉实时累积的少量K线再整体 seed，避免历史重复
                    agg.history.clear()
                    agg.current = None
                    agg.seed(candles)
                    keep_from = KlineAggregator.now_ms() - int(scfg.get("minutes", 30)) * 60_000
                    for c in candles:
                        if c.open_time >= keep_from:
                            storage.save_candle(sym, iv, c)
                    try:
                        hub.structures[(sym, iv)] = analyze_structure(
                            list(agg.history)[-160:])
                    except Exception:
                        pass
                    print(f"[backfill] 重试成功 {sym} {iv}: {len(candles)} 根")
                except Exception as e:
                    print(f"[backfill] 重试失败 {sym} {iv}: {e}")
            await asyncio.sleep(90)

    # 交易所公告（币安/OKX）：10 分钟拉一次，缓存给所有客户端
    async def news_poller():
        await asyncio.sleep(3)      # 等其它任务先起来
        while True:
            try:
                # 传入当前展示的币种：相关的新闻排前面（交易所公告 + 加密媒体 + 综合财经）
                shown = sorted(quant.symbol_filter or []) or symbols[:6]
                items = await asyncio.to_thread(
                    fetch_news, 10, proxy_cfg or None, shown, 80)
                if items:
                    hub.news = items
                    await hub.broadcast({"type": "news", "items": items})
                    srcs = {}
                    for it in items:
                        srcs[it.get("source", "?")] = srcs.get(it.get("source", "?"), 0) + 1
                    print(f"[news] 已更新 {len(items)} 条：" +
                          ", ".join(f"{k}×{v}" for k, v in sorted(srcs.items(), key=lambda x: -x[1])))
            except Exception as e:
                print(f"[news] 拉取失败: {e}")
            await asyncio.sleep(600)

    await asyncio.gather(supervisor(), sweep(), accounts_poller(),
                         news_poller(), backfill_retry())


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
