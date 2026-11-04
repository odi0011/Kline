from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
import threading
import uuid
from collections import defaultdict
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import yaml

import secrets_store
import patterns.builtin    # noqa: F401  导入即注册内置形态（单根/双根/三根）
import patterns.structure  # noqa: F401  导入即注册结构形态（头肩/三重/双顶底/三角/楔形）
import patterns.base as pbase
from core.aggregator import KlineAggregator
from core.engine import PatternEngine
from core.candle import INTERVAL_MS
from feed.backfill import fetch_klines
from feed.demo import DemoSource
from feed.depth import DepthStream
from feed.liquidation import LiquidationStream
from feed.market_data import (
    funding_snapshot, onboard_dates, open_interest_history, ticker_24h,
    usdt_perpetuals, max_leverage_map,
)
# 衍生品指标走带回退的入口：币安被地区限制时自动改用欧易
from feed.derivatives import derivatives_snapshot
from feed.giphy import download_image as giphy_download, search_stickers as giphy_search
from feed.news import fetch_all as fetch_news, fetch_article
from feed.news_ai import analyze as analyze_news
from feed.search import fetch_page, web_search
from feed.telegram import TelegramBot
from feed.source import BinanceFuturesStream
from decision.config import DecisionConfig
from decision.pool_audit import audit_universe
from notify.notifier import Notifier
from patterns.base import get_enabled
from analysis.structure import analyze as analyze_structure
from quant import QuantEngine
from server.hub import BroadcastHub
from storage import Storage
from accounts import fetch_account, protection_snapshot
from ai.agent import AgentManager
from ai.agent_tools import project_paper_positions, trades_view
from ai.sticker_cache import StickerCache
from ai.provider import (
    PROVIDER_PRESETS, CAPABILITIES, mask_cfg, merge_cfg,
    list_models as ai_list_models, test_model as ai_test_model, guess_model_meta,
)

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
    "quant": {"enabled": True, "decision": {}},
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


def write_config_file(cfg: dict, path: str) -> dict:
    """把配置写回磁盘：有加密密钥时密钥字段加密；写完收紧文件权限。

    返回实际落盘的内容（加密后的副本），便于调用方提示"仍有明文密钥"。
    加密失败（缺 cryptography/密钥）不阻断写配置：降级明文 + 明确告警。
    """
    payload = cfg
    try:
        payload = secrets_store.encrypt_config(cfg, path, create_key=False)
    except secrets_store.SecretsError as e:
        print(f"[config] 密钥加密失败，本次以明文写入（检查 KLINE_SECRET_KEY/secrets.key）: {e}")
    # 原子写：tmp -> fsync -> replace。配置里有密钥，半截文件会让下次启动
    # 直接解析失败（空配置文件比旧配置更糟）。
    tmp_path = f"{path}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(payload, f, allow_unicode=True, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
    try:
        secrets_store.harden_permissions(path)
    except Exception:
        pass
    return payload


def start_static(dist_dir: str, port: int) -> None:
    # 聊天图片附件放在配置目录下（不在 web/dist 里，构建会被覆盖）
    attach_dir = os.path.join(os.path.dirname(os.path.abspath("config.yaml")), "agent-attachments")

    class Quiet(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=dist_dir, **kwargs)

        def log_message(self, *args):
            pass

        def translate_path(self, path):
            # /agent-attachments/img/xxx.png → 配置目录下的同名文件，只允许读图片
            if path.startswith("/agent-attachments/"):
                sub = path[len("/agent-attachments/"):].split("?", 1)[0]
                base = os.path.abspath(attach_dir)
                target = os.path.abspath(os.path.join(base, sub))
                inside = target == base or target.startswith(base + os.sep)
                if not inside or not sub.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
                    return os.path.join(dist_dir, "__missing__")
                return target
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

    # ---- 主动发言：把系统事件推给 Agent，由它决定说不说 ----
    # agent 在下面才创建（它依赖 storage/quant）。这里放一个延迟绑定的
    # 分发器：定义早、调用晚，中间用 `_proactive_box` 传引用。
    _proactive_box: dict = {"agent": None, "loop": None}

    def _proactive_dispatch(kind: str, summary: str, symbol: str = "",
                            detail: dict | None = None, dedupe: str = "") -> None:
        """把一条系统事件投给 Agent。没有 agent / 没在跑就静默丢弃。

        必须用 call_soon_threadsafe：调用方可能在量化线程池里，
        直接 await 会跨线程操作事件循环。
        """
        agent_obj = _proactive_box.get("agent")
        loop = _proactive_box.get("loop")
        if agent_obj is None or loop is None or loop.is_closed():
            return
        try:
            from ai.proactive import ProactiveEvent
            event = ProactiveEvent(kind=kind, summary=summary, symbol=symbol,
                                   detail=detail or {}, dedupe_key=dedupe)

            def _fire():
                try:
                    asyncio.create_task(agent_obj.proactive(event))
                except Exception:
                    pass

            loop.call_soon_threadsafe(_fire)
        except Exception:
            pass


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
    qcfg = cfg.get("quant", {})
    decision_cfg = DecisionConfig.from_dict(qcfg.get("decision", {}))
    watch_intervals = [
        str(iv) for iv in (qcfg.get("watch_timeframes") or ["1h", "4h"])
        if str(iv) not in intervals and str(iv) in INTERVAL_MS
    ] if decision_cfg.watch_enabled else []
    print(f"[config] 监控 {len(symbols)} 个币种 / 周期 {intervals}")
    if watch_intervals:
        print(f"[config] 多周期盯盘周期（仅后端）: {watch_intervals}")

    aggs: dict[tuple, KlineAggregator] = {}
    watch_aggs: dict[tuple, KlineAggregator] = {}
    for sym in symbols:
        for iv in intervals:
            aggs[(sym, iv)] = KlineAggregator(sym, iv, None, None)
        for iv in watch_intervals:
            watch_aggs[(sym, iv)] = KlineAggregator(sym, iv, None, None)

    hub = BroadcastHub(aggs, symbols, intervals, port=int(cfg.get("ws_port", 8765)))
    # 全市场币种清单：前端「选择币种」菜单用它列出全部可交易 U 本位永续，
    # 不必先在 config.yaml 里预先配置。取不到时留空，前端只用 config 池。
    hub.market = list(symbols)

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
                      eval_threshold=float(scfg.get("eval_threshold", 0.0015)),
                      stats_days=decision_cfg.pattern_window_days,
                      stats_alpha=decision_cfg.pattern_winrate_alpha,
                      # 保留期（天；0 = 永久保留）。长期策略分析不能再 7 天就删光。
                      hit_days=int(scfg.get("hit_days", 90) or 0),
                      # 模拟仓平仓记录保留天数：0 = 永久保留（策略复盘要长期样本）
                      paper_keep_days=int(scfg.get("paper_keep_days", 0) or 0),
                      quant_trade_days=int(scfg.get("quant_trade_days", 365) or 0),
                      quant_event_days=int(scfg.get("quant_event_days", 30) or 0),
                      news_days=int(scfg.get("news_days", 7) or 0),
                      trace_days=int(scfg.get("trace_days", 30) or 0),
                      # K线长期归档（回测用）与 Agent 聊天长期归档：0 = 永久保留
                      candle_archive_days=int(scfg.get("candle_archive_days", 0) or 0),
                      agent_chat_days=int(scfg.get("agent_chat_days", 0) or 0))
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
                                  "pattern": hit.pattern, "name_zh": hit.name_zh,
                                  "geometry": hit.geometry})
            # 形态命中也是"可能值得主动说一句"的事件。
            # 方向和强度交给 Agent 自己判断要不要开口（闸门会给低概率），
            # 这里只如实把命中内容推过去。
            _proactive_dispatch(
                "pattern",
                f"{hit.symbol.upper()} {hit.interval} 命中{hit.name_zh or hit.pattern}"
                f"（{'看涨' if hit.direction == 'bullish' else '看跌' if hit.direction == 'bearish' else '中性'}）"
                f" @ {hit.price}",
                symbol=hit.symbol,
                detail={"pattern": hit.pattern, "nameZh": hit.name_zh,
                        "direction": hit.direction, "price": hit.price,
                        "interval": hit.interval},
                dedupe=f"pattern:{hit.symbol}:{hit.pattern}")

    engine = PatternEngine(enabled, notify_with_lag,
                           live_preview=bool(pcfg.get("live_preview", True)))

    # 账户列表可变盒子：设置弹窗改后热生效；refresh 标志触发立即拉取。
    # 单账户设计：config 里多余的账户在启动时裁掉（页面只展示一个账户卡）。
    accts_box = {"list": [dict(a) for a in cfg.get("accounts", [])][:1], "refresh": False}
    # 旧配置没有 id：补一个稳定 ID（权益曲线按 id 存，改名不会丢历史）。
    if accts_box["list"] and not accts_box["list"][0].get("id"):
        accts_box["list"][0]["id"] = uuid.uuid4().hex[:12]
        cfg["accounts"] = accts_box["list"]
        write_config_file(cfg, config_path)
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
        write_config_file(cfg, config_path)
        print(f"[settings] 单账户模式：已裁剪为 1 个账户并写回配置")

    def save_config() -> None:
        try:
            payload = write_config_file(cfg, config_path)
            print(f"[settings] 配置已写入 {config_path}")
            plain = secrets_store.audit_plaintext(payload) if isinstance(payload, dict) else []
            if plain:
                print(f"[settings] 安全提示：{', '.join(plain[:8])} 仍以明文保存；"
                      f"可运行 python tools/encrypt_config.py 加密，或改用环境变量覆盖")
        except Exception as e:
            print(f"[settings] 配置写入失败: {e}")

    def _mask_key(k: str) -> str:
        k = k or ""
        return (k[:6] + MASK_KEY + k[-4:]) if len(k) > 12 else (MASK_SEC if k else "")

    def _mask_telegram(t: dict) -> dict:
        """Telegram 配置：token 只给掩码（和 AI 密钥同样的处理），并附运行状态。"""
        token = str(t.get("token") or "")
        return {
            "enabled": bool(t.get("enabled")),
            "token": (token[:6] + MASK_KEY + token[-4:]) if len(token) > 12 else (MASK_SEC if token else ""),
            "hasToken": bool(token),
            "allowed_chats": [str(x) for x in (t.get("allowed_chats") or [])][:50],
            "status": (tg_box.get("bot").describe() if tg_box.get("bot")
                       else {"configured": bool(token), "running": False,
                             "bot": {}, "allowed": [], "error": ""}),
        }

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
            # AI 配置同样只下发掩码；模型清单本身不含密钥，可原样下发
            "ai": {"providers": PROVIDER_PRESETS, "capabilities": CAPABILITIES,
                   **mask_cfg(cfg.get("ai", {}) or {})},
            # Telegram：token 只回掩码，白名单和运行状态原样下发
            "telegram": _mask_telegram(cfg.get("telegram", {}) or {}),
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
        # Telegram：token 回传掩码/空 → 沿用原值；白名单整体替换
        telegram = s.get("telegram")
        if isinstance(telegram, dict):
            cur = dict(cfg.get("telegram") or {})
            raw = str(telegram.get("token") or "").strip()
            if raw and MASK_KEY not in raw and raw != MASK_SEC:
                cur["token"] = raw[:200]
            if "enabled" in telegram:
                cur["enabled"] = bool(telegram["enabled"])
            if isinstance(telegram.get("allowed_chats"), list):
                cur["allowed_chats"] = [str(x)[:32] for x in telegram["allowed_chats"][:50] if str(x).strip()]
            cfg["telegram"] = cur
        ai = s.get("ai")
        if isinstance(ai, dict):
            # 密钥走 merge（掩码/留空则沿用原值）；模型清单整体替换
            merged = merge_cfg(cfg.get("ai", {}) or {}, ai)
            if isinstance(ai.get("models"), list):
                models = []
                for m in ai["models"][:40]:
                    if not isinstance(m, dict) or not str(m.get("id") or "").strip():
                        continue
                    models.append({
                        "id": str(m["id"])[:120],
                        "label": str(m.get("label") or m["id"])[:80],
                        "icon": str(m.get("icon") or "")[:300000],
                        "caps": [c for c in (m.get("caps") or []) if c in ("text", "vision", "pdf", "video")],
                        "ctx": int(m.get("ctx") or 0),
                        "system": str(m.get("system") or "")[:4000],
                        "active": bool(m.get("active")),
                    })
                merged["models"] = models
            cfg["ai"] = merged
        save_config()
        print("[settings] 设置已应用并持久化")
        return current_settings()

    hub.settings_fn = current_settings
    hub.on_settings = apply_settings

    def _ai_helper(kind: str, a: str = "", b: str = "", c: str = "") -> dict:
        """AI 设置页的三个辅助动作：列模型 / 测连通 / 猜能力。

        参数约定（hub 侧按此传参）：
          list  → a=base, b=key
          test  → a=model_id, b=base, c=key
          guess → a=model_id
        密钥优先级：本次请求里带的 key > 服务端已存的 key，
        这样用户刚粘贴密钥就能直接「获取模型 / 测试」，不必先保存一次。
        """
        saved = cfg.get("ai", {}) or {}
        saved_base = str(saved.get("base") or "").strip()
        saved_key = str(saved.get("key") or "")

        def pick_key(raw: str) -> str:
            k = (raw or "").strip()
            return saved_key if (not k or MASK_SEC in k) else k

        if kind == "list":
            return ai_list_models((a or saved_base).strip(), pick_key(b), proxy_cfg or None)
        if kind == "test":
            return ai_test_model({"base": (b or saved_base).strip(), "key": pick_key(c)},
                                 a, proxy_cfg or None)
        if kind == "guess":
            return guess_model_meta(a or b)
        return {"ok": False, "error": f"未知动作 {kind}"}
    hub.ai_fn = _ai_helper

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

    quant = QuantEngine(accts_box, config_path, price_fn=_price_fn,
                        min_pattern_winrate=float(qcfg.get("min_pattern_winrate", 0.35)),
                        min_pattern_samples=int(qcfg.get("min_pattern_samples", 5)),
                        decision_config=decision_cfg,
                        # 没连上 MySQL 时传 None：模拟仓退回纯内存，
                        # 汇总口径才不会变成一张空表（降级要显式、可解释）
                        paper_store=storage if storage.enabled else None)
    _paper_info = dict(getattr(quant.paper, "restore_info", {}) or {})
    if _paper_info.get("positions") or _paper_info.get("closed"):
        print(f"[paper] 模拟仓已从 MySQL 恢复：未平仓 {_paper_info.get('positions', 0)} 个，"
              f"累计平仓 {_paper_info.get('closed', 0)} 条")
    quant.enabled = bool(qcfg.get("enabled", True))
    # 假数据防护：demo 模式下引擎拒绝一切新开仓（形态来自随机游走，没有交易意义）
    quant.feed_is_live_fn = lambda: hub.mode != "demo"
    quant.pattern_stats_fn = storage.pattern_stats
    hub.paper_refresh_fn = quant.paper_refresh
    # 计算流水落库：库里只留 1 分钟；页面刷新时取最近 20 秒填充
    quant.calc_storage_fn = storage.save_calc_rows
    quant.calc_read_fn = storage.calc_recent
    # 决策流水落库：引擎线程内同步写 MySQL（连接已加锁，开销可忽略）
    quant.storage_fn = storage.save_quant_event
    quant.trade_storage_fn = storage.save_quant_trade
    # 决策 trace / gate 拦截明细、风控与漂移保护状态：同样落库，重启不清零
    quant.trace_storage_fn = storage.save_decision_trace
    quant.trace_read_fn = storage.decision_traces
    quant.state_storage_fn = storage.save_quant_state
    quant.state_read_fn = storage.quant_state
    try:
        _restored = quant.restore_persistent_state() or {}
    except Exception as e:       # 恢复失败不能影响服务启动，按空状态继续
        _restored = {}
        print(f"[quant] 持久化状态恢复失败: {e}")
    if _restored.get("traces") or _restored.get("gates") or _restored.get("risk") or _restored.get("drift"):
        extra = []
        if _restored.get("risk"):
            extra.append("风控状态")
        if _restored.get("drift"):
            extra.append("漂移保护状态")
        print(f"[quant] 已从 MySQL 恢复：决策 trace {_restored.get('traces', 0)} 条、"
              f"gate 拦截 {_restored.get('gates', 0)} 条"
              + ("、" + "、".join(extra) if extra else ""))

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

    def _watch_bars_for(symbol: str):
        def read(iv: str, include_current: bool = False):
            agg = watch_aggs.get((symbol, iv)) or aggs.get((symbol, iv))
            if not agg:
                return []
            hist = list(agg.history)
            if include_current and agg.current is not None:
                hist.append(agg.current)
            return hist[-320:]
        entry_iv = engine.detect_interval or iv0
        return read(entry_iv, include_current=True), read("1h"), read("4h")

    quant.watch_bars_fn = _watch_bars_for
    quant.depth_fn = lambda sym: hub.depths.get(sym)

    # 交易所每个合约的真实杠杆上限（BTC 150x / TSLA 20x 这类差异来自分档表）
    leverage_box: dict = {"map": {}}

    def _max_leverage(sym: str):
        return leverage_box["map"].get(str(sym).lower())

    quant.max_leverage_fn = _max_leverage

    async def leverage_poller():
        """杠杆分档每天变一次即可；启动先拉，失败留空（前端显示"—"）。"""
        while True:
            try:
                data = await asyncio.to_thread(max_leverage_map, proxy_cfg or None)
                if data:
                    leverage_box["map"] = data
                    print(f"[leverage] 已加载 {len(data)} 个合约的杠杆上限")
            except Exception as e:
                print(f"[leverage] 杠杆分档拉取失败: {e}")
            await asyncio.sleep(6 * 3600)
    quant.news_fn = lambda: hub.news

    market_feature_cache: dict[str, tuple[int, dict]] = {}
    unhealthy_symbols: set[str] = set()

    def _market_data(symbol: str) -> dict:
        cached = market_feature_cache.get(symbol)
        if cached:
            return cached[1]
        return {}

    def _fetch_market_data(symbol: str) -> dict:
        out = {}
        try:
            oi = open_interest_history(symbol, period="5m", limit=30,
                                       proxy=proxy_cfg or None)
            if oi:
                out["oi"] = oi
        except Exception:
            pass
        try:
            funding = funding_snapshot(symbol, proxy=proxy_cfg or None)
            if funding:
                out["funding"] = funding
        except Exception:
            pass
        market_feature_cache[symbol] = (int(KlineAggregator.now_ms()), out)
        return out

    quant.market_data_fn = _market_data

    def _data_health(symbol: str | None = None) -> bool:
        if symbol and symbol in unhealthy_symbols:
            return False
        feed = feed_box.get("feed")
        if feed is None or not getattr(feed, "connected", False):
            return False
        last_ms = int(getattr(feed, "last_trade_ms", 0) or 0)
        if not last_ms:
            return False
        return (KlineAggregator.now_ms() - last_ms) <= decision_cfg.stale_trade_sec * 1000

    quant.data_health_fn = _data_health

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

    # ---- 运行时按需订阅：前端可选全市场 U 本位永续 ----
    # 配置里的 symbols 只是「启动即订阅」的初始集合；用户临时加看池外币种时，
    # 在这里补齐聚合器（历史回补 + 实时流 + 深度/强平），不改 config.yaml。
    _subs_lock = threading.Lock()

    def ensure_symbol(sym: str) -> str | None:
        """注册一个币种的全部聚合器；已存在则直接返回。"""
        sym = str(sym or "").lower()
        if not sym:
            return None
        with _subs_lock:
            if sym in symbols:
                return sym
            # 只接受真实的交易对形态，避免把任意字符串灌进订阅 URL
            if not re.fullmatch(r"[a-z0-9]{2,20}usdt", sym):
                return None
            symbols.append(sym)
            by_symbol[sym] = []
            for iv in intervals:
                agg = KlineAggregator(sym, iv, None, None)
                agg.on_update = on_evt
                agg.on_close = on_evt
                aggs[(sym, iv)] = agg
                by_symbol[sym].append(agg)
            for iv in watch_intervals:
                watch_aggs[(sym, iv)] = KlineAggregator(sym, iv, None, None)
            for agg in watch_aggs.values():
                agg.on_update = lambda *args, **kwargs: None
                agg.on_close = lambda *args, **kwargs: None
        hub.symbols = symbols
        hub.aggs = aggs
        print(f"[feed] 按需订阅 {sym}（当前 {len(symbols)} 个币）")
        # 历史回补 + 实时订阅 + 深度/强平：都放后台，别卡住 WS 消息处理
        asyncio.create_task(_warm_symbol(sym))
        return sym

    async def _warm_symbol(sym: str) -> None:
        for iv in intervals:
            agg = aggs.get((sym, iv))
            if not agg:
                continue
            try:
                candles = await asyncio.to_thread(fetch_klines, sym, iv, limit, proxy_cfg or None)
                if candles:
                    agg.seed(candles)
                    try:
                        storage.archive_candles(sym, iv, candles)
                    except Exception as e:
                        print(f"[archive] 按需 {sym} {iv} 归档失败: {e}")
                    if (sym, iv) in aggs:
                        try:
                            hub.structures[(sym, iv)] = analyze_structure(list(agg.history)[-160:])
                        except Exception:
                            pass
                    print(f"[backfill] 按需 {sym} {iv}: {len(candles)} 根")
            except Exception as e:
                print(f"[backfill] 按需 {sym} {iv} 失败: {e}")
        feed = feed_box.get("feed")
        if feed is not None and hasattr(feed, "subscribe"):
            feed.subscribe(symbols)
        if mode != "demo":
            if depth_box["stream"]:
                depth_box["stream"].subscribe(symbols)
            if liq_box["stream"]:
                liq_box["stream"].subscribe(symbols)
        # 补发历史：新卡片直接有K线，而不是从一根实时K线开始累积
        try:
            await hub.broadcast(json.loads(hub.symbol_snapshot(sym)))
        except Exception:
            pass

    # 量化交易范围跟随前端实际展示的币种：避免在用户没看K线的币上开仓。
    # 币种集合变化时唤醒衍生品轮询（避免新加的币等满一个周期）
    deriv_wake = asyncio.Event()

    # 空列表不做限制（前端还没连上时保持原有行为）。
    # 前端可选全市场币种，因此这里收到配置外的币时会走 ensure_symbol() 按需订阅。
    def _set_quant_symbols(syms):
        valid = [s for s in (ensure_symbol(x) for x in syms) if s]
        if valid:
            quant.symbol_filter = set(valid)
            print(f"[quant] 交易范围更新: {', '.join(valid)}")
            deriv_wake.set()        # 让衍生品轮询立刻补这些币的指标
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
            # 长期归档：滚动表只留 30 分钟，回测/复盘要完整历史。
            # 归档失败不能打断实时行情回调。
            try:
                storage.archive_candle(sym, iv, candle)
            except Exception as e:
                print(f"[archive] {sym} {iv} 归档失败: {e}")
            storage.eval_outcomes(sym, iv, INTERVAL_MS[iv], candle)
            storage.prune(KlineAggregator.now_ms())
        else:
            engine.on_candle(sym, iv, candle, False, history)
        hub.on_update(sym, iv, candle, closed, history)

    for agg in aggs.values():
        agg.on_update = on_evt
        agg.on_close = on_evt
    for agg in watch_aggs.values():
        agg.on_update = lambda *args, **kwargs: None
        agg.on_close = lambda *args, **kwargs: None


    bcfg = cfg.get("feed", {})
    proxy_cfg = (bcfg.get("proxy") or "").strip()

    def _agent_context() -> dict:
        snapshot = quant.snapshot()
        analysis = snapshot.get("analysis") or {}
        accounts = []
        for item in hub.last_accounts or []:
            accounts.append({key: item.get(key) for key in (
                "name", "balance", "unrealized", "available", "positions", "error")})
        # 模拟仓（15 分钟纸上交易）：与上面的账户/持仓/成交是两套数据。
        # 之前根本没接进 context，Agent 只能拿账户去答"模拟仓"，把实盘账户
        # 当成了模拟仓。这里单独投影一份不带K线的精简持仓。
        paper_rows = project_paper_positions(analysis.get("paper"))
        try:
            paper_summary = quant.paper.summary()
        except Exception:
            paper_summary = {}
        return {
            "ts": snapshot.get("ts"),
            "enabled": snapshot.get("enabled"),
            "symbols": symbols,
            "stats": snapshot.get("stats") or {},
            "market": analysis.get("symbols") or {},
            # 系统这一轮评分的真实特征输入（MACD/挤压/CVD/VSA/OBI/HTF/HMM/
            # 清算/新闻情绪）。页面上有、之前没给 Agent，它只能自己拿 K 线手算。
            "liveFeatures": analysis.get("liveFeatures") or {},
            # 衍生品指标（资金费率/OI/多空比/taker/大户持仓），来自交易所公开接口。
            "derivatives": dict(getattr(hub, "derivatives", {}) or {}),
            "positions": snapshot.get("positions") or [],
            "accounts": accounts,
            "trades": list(getattr(quant, "trades", [])[-20:]),
            # 模拟仓：未平仓的纸上仓位、按币种已实现战绩、最近平仓记录。
            # read_paper 专用；read_accounts 不会读这三项。
            "paper": paper_rows,
            "paperStats": analysis.get("paperStats") or {},
            "paperChecks": analysis.get("paperChecks") or {},
            "paperHistory": (analysis.get("paperHistory") or [])[-12:],
            "paperSummary": paper_summary,
            "positionPlans": analysis.get("positionPlans") or [],
            "drawdown": analysis.get("drawdown") or {},
            "drift": analysis.get("drift") or {},
            # 过滤器拦截记录与决策轨迹：用户问"刚才为什么没开单"要靠它，
            # 之前只有 drawdown/params，Agent 只能靠 events 猜。
            "gateLog": analysis.get("gateLog") or [],
            "decisionTraces": analysis.get("decisionTraces") or [],
            "evaluations": analysis.get("liveEvaluations") or {},
            "params": analysis.get("params") or {},
            # 形态胜率：前端形态卡片上显示的就是这份数据。
            # 之前没接进来，Agent 被问"哪种形态胜率最高"只能回"我没有这个工具"。
            "patternStats": storage.pattern_stats() if storage else {},
            "recentEvents": (snapshot.get("events") or [])[-40:],
            "news": [{key: item.get(key) for key in ("title", "source", "url", "ts", "desc")}
                     for item in hub.news[:30]],
            "health": {"mode": hub.mode, "lagP50": hub.lag_p50,
                       "accountsAvailable": len(hub.last_accounts or [])},
        }

    # 新闻正文：入库时/后台预取时抓取并落库，前端点开只读库（不再实时爬源站）。
    hub.news_lookup_fn = storage.news_full
    hub.news_ai_fn = storage.news_ai          # 正文入库时已生成，这里只读

    # 正文预取队列：去重 + 单线程串行 + 间隔，避免对源站高频请求触发 429。
    article_queue: dict[str, dict] = {}
    article_busy = set()

    def enqueue_article(url: str) -> None:
        url = str(url or "")[:512]
        if not url or url in article_busy or url in article_queue:
            return
        item = next((it for it in hub.news if it.get("url") == url), None)
        if item:
            article_queue[url] = item

    hub.news_enqueue_fn = enqueue_article

    agent = AgentManager(
        lambda: cfg.get("ai", {}) or {},
        lambda: proxy_cfg,
        _agent_context,
        hub.broadcast,
        os.path.join(os.path.dirname(os.path.abspath(config_path)), "agent-state.json"),
        # Agent 任务/对话进 MySQL（JSON 文件保留为兜底/首次迁移来源）；
        # 没连上 MySQL 时退回纯文件，行为与旧版一致。
        state_store=storage if storage.enabled else None,
    )
    # 贴纸落盘缓存：和聊天图片附件放同一棵目录，静态服务已经在对外提供。
    # 表情包库：图片落 agent-attachments/stickers/，元数据进 MySQL stickers 表。
    # 传 storage 进去，Agent 才能列出库里有什么（而不是盲猜情绪词）。
    sticker_cache = StickerCache(
        os.path.join(os.path.dirname(os.path.abspath(config_path)),
                     "agent-attachments", "stickers"),
        store=storage if storage.enabled else None)
    # 联网搜索 / 网页读取：由模型自己决定是否调用 web_search/read_web_page。
    # 默认无 key 走 DuckDuckGo/Bing HTML；有 KLINE_SERPER_KEY 时优先 Serper。
    # 代理沿用 feed.proxy（海外服务器可直连；本地开发可配 search.proxy 覆盖）。
    _search_cfg = cfg.get("search") or {}
    # 代理在下面两个 if 里都要用：搜索被关掉时也不能是未定义变量。
    _search_proxy = str(_search_cfg.get("proxy") or proxy_cfg or "").strip()
    if not _search_proxy:
        try:
            import urllib.request as _urlreq
            _proxies = _urlreq.getproxies() or {}
            _search_proxy = str(_proxies.get("https") or _proxies.get("http") or "").strip()
        except Exception:
            _search_proxy = ""

    def _search_number(key: str, default, cast=float):
        """搜索配置容错：写错类型/负数不能让服务启动失败，回退默认值。"""
        try:
            value = cast(_search_cfg.get(key, default))
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default

    if _search_cfg.get("enabled", True):
        _search_timeout = _search_number("timeout", 10.0)
        _search_count = max(1, min(10, int(_search_number("count", 5, int))))
        agent.search_default_count = _search_count
        agent.search_fn = lambda query, count=0: web_search(
            query, count or agent.search_default_count, _search_proxy, _search_timeout)
        agent.fetch_fn = lambda url: fetch_page(
            url, _search_proxy, _search_timeout)

    # 在线贴纸（GIPHY）：命中过的图直接落到 agent-attachments/stickers/，
    # 之后同一个情绪词只读本地文件——不打 API、不跨境拉图。
    # 没配 key 时这三个都不注入，Agent 只会用内置的三张图。
    _sticker_cfg = cfg.get("stickers") or {}
    _giphy_key = str(_sticker_cfg.get("giphy_key") or "").strip()
    if _giphy_key and _sticker_cfg.get("enabled", True):
        # 贴纸有自己的 timeout；写错/缺失时退回 8 秒，不能复用搜索的配置对象
        try:
            _sticker_timeout = float(_sticker_cfg.get("timeout") or 8.0)
        except (TypeError, ValueError):
            _sticker_timeout = 8.0
        if _sticker_timeout <= 0:
            _sticker_timeout = 8.0
        _sticker_proxy = str(_sticker_cfg.get("proxy") or _search_proxy or "").strip()

        def _sticker_search(query, count=1):
            return giphy_search(query, count, _giphy_key, _sticker_proxy, _sticker_timeout)

        def _sticker_download(url):
            return giphy_download(url, _sticker_proxy, _sticker_timeout)

        agent.sticker_search_fn = _sticker_search
        agent.sticker_download_fn = _sticker_download
        agent.sticker_cache = sticker_cache
        print(f"[stickers] GIPHY 在线贴纸已启用（缓存目录 {sticker_cache.root}）")
    # Agent 下单能力：两个回调分开注入——算单与执行必须能分别调用，
    # 否则模型会一步下完（真实资金操作必须先让用户看到那张单）。
    agent.order_plan_fn = quant.manual_order_plan
    agent.order_exec_fn = quant.manual_order_execute
    # 模拟仓平仓明细现查：快照里那份是全市场最近 12 条，问"BTC 最近 5 笔"
    # 会被别的币挤掉（线上实测 121 笔只读出 3 笔）。这里按币查库。
    agent.paper_history_fn = quant.paper.recent_trades

    # ---- Agent 的现查入口：交易所挂单 / 权益历史 / 全历史成交 ----
    # 这三个都不在 context 快照里（一个要打交易所、两个要查库），
    # 之前 Agent 完全没有，只能拿 20 条内存成交硬答，还会编"没有数据源"。
    def _agent_orders(symbol: str = "") -> dict:
        """交易所真实挂单（普通 + 算法条件单）与"有仓没保护"的裸奔清单。"""
        out = {"symbols": {}, "gaps": [], "errors": [], "ts": KlineAggregator.now_ms()}
        accts = [a for a in accts_box["list"] if a.get("api_key") and a.get("api_secret")]
        if not accts:
            raise RuntimeError("没有可交易的账户（未配置 API Key）")
        for acct in accts:
            try:
                positions = (next((r for r in (hub.last_accounts or [])
                                   if r.get("name") == acct["name"]), {})
                             or {}).get("positions") or []
                snap = protection_snapshot(acct["api_key"], acct["api_secret"], positions)
            except Exception as exc:
                out["errors"].append(f"{acct['name']}: {str(exc)[:100]}")
                continue
            for sym, rows in (snap.get("symbols") or {}).items():
                out["symbols"].setdefault(sym, []).extend(rows)
            out["gaps"].extend(snap.get("gaps") or [])
            out["errors"].extend(snap.get("errors") or [])
        out["gaps"] = sorted(set(out["gaps"]))
        return out

    def _agent_equity(days: int = 30, hours: int = 24) -> dict:
        """权益日档 + 最近 hours 小时采样。日档永久，采样只留 2 天。"""
        if not storage.enabled:
            raise RuntimeError("权益历史需要 MySQL，当前未连接")
        for acct in accts_box["list"]:
            key = acct.get("id") or acct["name"]
            daily = storage.equity_heatmap(key)
            series = storage.equity_series(key, minutes=max(1, hours) * 60,
                                          limit=max(120, hours * 12))
            if daily or series:
                return {"account": acct["name"], "daily": daily, "series": series}
        return {"account": None, "daily": [], "series": [],
                "reason": "还没有任何权益采样（服务刚启动或账户取数一直失败）"}

    def _agent_trades(days: int = 30, symbol: str = "", limit: int = 50) -> dict:
        """全历史成交 + 区间统计。days=0 表示全部历史。

        **不能只读 MySQL**：quant_trades 是较晚才接的（线上实测库里只有 3 笔，
        而 quant-trades.json 里有 260 笔、净 +110U）。早期成交只存在于本地
        流水文件里，只查库会把"全历史"答成 3 笔。合并去重逻辑在 trades_view。
        """
        accts = accts_box["list"]
        db_rows: list[dict] = []
        if storage.enabled:
            try:
                db_rows = storage.quant_trades(accts[0]["name"] if accts else "",
                                               limit=5000)
            except Exception:
                db_rows = []
        view = trades_view(db_rows, getattr(quant, "trades", None) or [],
                           [a["name"] for a in accts],
                           days=days, symbol=symbol, limit=limit,
                           now_ms=KlineAggregator.now_ms())
        view["account"] = accts[0]["name"] if accts else ""
        return view

    agent.data_fns = {"orders": _agent_orders, "equity": _agent_equity,
                      "trades": _agent_trades}
    # 主动发言的分发器现在有了真实的 agent 与事件循环
    _proactive_box["agent"] = agent
    try:
        _proactive_box["loop"] = asyncio.get_running_loop()
    except RuntimeError:
        _proactive_box["loop"] = None
    # 量化引擎的事件流（开仓/平仓/风控/异常）→ 主动发言候选。
    # 只挑**值得打扰用户**的那几类：审计/系统心跳之类的杂音不推。
    _PROACTIVE_KINDS = {"open", "close", "error", "slippage", "protection"}
    _orig_broadcast = quant.broadcast_fn

    def _broadcast_with_proactive(obj):
        try:
            if isinstance(obj, dict) and obj.get("type") == "quant_event":
                ev = obj.get("event") or {}
                kind = str(ev.get("kind") or "")
                if kind in _PROACTIVE_KINDS:
                    _proactive_dispatch(
                        "risk" if kind in ("error", "slippage", "protection") else "position",
                        str(ev.get("reason") or "")[:160],
                        symbol=str(ev.get("symbol") or ""),
                        detail={k: ev.get(k) for k in
                                ("pnl", "side", "qty", "entry", "exit",
                                 "slippageAtr", "apiCode") if ev.get(k) is not None},
                        dedupe=f"{kind}:{ev.get('symbol') or ''}:{str(ev.get('reason'))[:40]}")
        except Exception:
            pass
        return _orig_broadcast(obj)

    if _orig_broadcast is not None:
        quant.broadcast_fn = _broadcast_with_proactive
    hub.agent_state_fn = agent.state
    hub.agent_fn = agent.handle

    # Agent 读K线的入口：直接从聚合器取，不联网；interval 为空时用当前检测周期
    def _agent_bars(symbol: str, interval: str = ""):
        """给 Agent 读K线的入口。

        **不用 engine.detect_interval**：那个值会随行情在 1m/3m 之间来回切，
        同一句"BTC 现在怎么样"隔几秒问一次，拿到的可能是不同周期的K线，
        结论自然反转。模型没指定周期时，固定用配置里的第一个周期。
        """
        iv = interval or iv0
        agg = aggs.get((symbol, iv))
        if not agg:
            # 指定周期没建聚合器时退回默认周期，而不是直接给空
            agg = aggs.get((symbol, iv0))
        if not agg:
            return []
        hist = list(agg.history)
        if agg.current is not None:
            hist.append(agg.current)
        return hist

    agent.bars_fn = _agent_bars
    # 让 Agent 能区分"币不在监控列表"和"在列表但数据还没回来"
    agent.symbols_fn = lambda: list(symbols)
    agent.calc_read_fn = storage.calc_recent

    def _agent_advice(symbol: str) -> dict:
        """胶囊同款建议：直接复用 quant 的计算，保证两边结论永远一致。

        以前 Agent 只看原始指标自己判断，胶囊看系统评分，两边经常给出
        相反方向（胶囊做空、Agent 说做多），用户只会觉得系统坏了。

        附带 ts：advice 本身不带时间戳，但它是从 liveEvaluations 算出来的，
        把评估的时刻一起返回，模型才能判断"这个方向是几秒前的"。
        """
        key = str(symbol or "").lower()
        analysis = quant.snapshot().get("analysis") or {}
        advice = (analysis.get("advice") or {}).get(key)
        if not advice:
            return {}
        evaluation = (analysis.get("liveEvaluations") or {}).get(key) or {}
        if evaluation.get("ts"):
            advice = dict(advice)
            advice["ts"] = evaluation["ts"]
        return advice

    agent.advice_fn = _agent_advice

    # ---- Telegram Bot：与网页端共用同一个 AgentManager 会话 ----
    tg_box: dict = {"bot": None}

    async def _tg_reply(chat_id, text, attachments=None):
        # 同一个 Agent：网页与 Telegram 共享会话记录和上下文。
        # 附件走和网页端完全相同的通道（base64 → 多模态 / 文本内联）。
        return await agent.ask_and_wait(text, attachments=attachments)

    async def telegram_supervisor():
        """按配置启停轮询；设置页改 token/开关后 5 秒内生效。"""
        while True:
            try:
                tcfg = cfg.get("telegram") or {}
                enabled = bool(tcfg.get("enabled"))
                token = str(tcfg.get("token") or "").strip()
                bot = tg_box.get("bot")
                # token 变了或不再需要 → 停掉旧实例
                if bot is not None and (bot.token != token or not enabled):
                    await bot.stop()
                    tg_box["bot"] = None
                    bot = None
                if enabled and token and bot is None:
                    bot = TelegramBot(token, tcfg.get("allowed_chats") or [],
                                      _tg_reply, proxy_cfg or None)
                    tg_box["bot"] = bot
                    info = await asyncio.to_thread(bot.verify)
                    if info.get("ok"):
                        bot.start()
                        print(f"[telegram] 已连接 @{info['bot'].get('username')}"
                              f"（白名单 {len(bot.allowed)} 个会话）")
                    else:
                        print(f"[telegram] token 校验失败: {info.get('error')}")
            except Exception as e:
                print(f"[telegram] 监督任务异常: {e}")
            await asyncio.sleep(5)

    limit = int(bcfg.get("backfill_bars", 300))
    for (sym, iv), agg in {**aggs, **watch_aggs}.items():
        try:
            candles = await asyncio.to_thread(fetch_klines, sym, iv, limit, proxy_cfg or None)
            agg.seed(candles)
            try:
                storage.archive_candles(sym, iv, candles)
            except Exception as e:
                print(f"[archive] {sym} {iv} 归档失败: {e}")
            keep_from = KlineAggregator.now_ms() - int(scfg.get("minutes", 30)) * 60_000
            for c in candles:
                if c.open_time >= keep_from:
                    storage.save_candle(sym, iv, c)
            # 启动即算一次结构，否则要等到本周期收线（最长几分钟）画面上才有线
            if (sym, iv) in aggs:
                try:
                    hub.structures[(sym, iv)] = analyze_structure(list(agg.history)[-160:])
                except Exception:
                    pass
            print(f"[backfill] {sym} {iv}: 回补 {len(candles)} 根")
        except Exception as e:
            print(f"[backfill] {sym} {iv} 失败: {e}（空历史启动）")

    by_symbol: dict[str, list[KlineAggregator]] = defaultdict(list)
    for (sym, _), agg in {**aggs, **watch_aggs}.items():
        by_symbol[sym].append(agg)

    def on_trade(symbol: str, price: float, qty: float, ts: int, _recv_ms: int,
                 buyer_maker: bool = False) -> None:
        for agg in by_symbol.get(symbol, ()):
            agg.on_trade(price, qty, ts, buyer_maker)

    # 订单簿深度（真实挂单）：独立 /public 路由，与 aggTrade 并行
    depth_box: dict = {"stream": None}
    liq_box: dict = {"stream": None}

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

    if not bcfg.get("liquidation_disabled", False):
        try:
            liq_box["stream"] = LiquidationStream(
                symbols, proxy=proxy_cfg or True,
            )
        except Exception as e:
            print(f"[liquidation] 初始化失败: {e}")

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
    if mode != "demo" and liq_box["stream"]:
        asyncio.create_task(liq_box["stream"].run())
        print(f"[liquidation] 强平事件流已启动（{len(symbols)} 个币）")

    if liq_box["stream"]:
        quant.liquidation_fn = lambda sym: liq_box["stream"].cascade(
            sym,
            window_sec=decision_cfg.liquidation_window_sec,
            min_events=decision_cfg.liquidation_min_events,
            min_notional=decision_cfg.liquidation_min_notional,
            cooldown_sec=decision_cfg.liquidation_cooldown_sec,
        )
    else:
        quant.liquidation_fn = lambda sym: {"active": False}

    await hub.start()
    await agent.start()

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
            for agg in [*aggs.values(), *watch_aggs.values()]:
                agg.sweep(now)
            try:
                # 行情延迟 p50：前端状态条与 /stats 广播都读 hub.lag_p50，
                # 之前只初始化成 None、从未赋值（审计发现的死字段）。
                feed_obj = feed_box.get("feed")
                if feed_obj is not None:
                    try:
                        lag_stats = feed_obj.lag_stats() or {}
                        hub.lag_p50 = int(lag_stats["p50"]) if lag_stats.get("p50") is not None else None
                    except Exception:
                        pass
                await asyncio.to_thread(quant.manage)    # 止损/止盈监控（每 5 秒，线程池内不卡主循环）
                qtick += 1
                if qtick % 10 == 0:                  # 约 5 秒广播一次量化快照
                    # 广播走精简版：调试字段（decisionTraces/calcStream/
                    # latestTraces/gateLog）不进浏览器，省掉 ~190KB/次
                    asyncio.create_task(hub.broadcast(
                        {"type": "quant", "snapshot": hub.slim_quant(quant.snapshot())}))
                await asyncio.to_thread(quant.audit)  # 5 分钟自审计（引擎内部自限流）
            except Exception as e:
                print(f"[quant] 运行异常: {e}")

    async def supervisor():
        # 行情断开/停流一律重连，绝不自动切换演示数据：
        # 假行情会驱动形态引擎产生假信号 → 量化在账户上真下单 + 污染胜率统计。
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
                        row["equityHistory"] = storage.equity_heatmap(eq_key)
                    except Exception:
                        row["series"] = []
                        row["equityHistory"] = []
                    results.append(row)
                except Exception as e:
                    row = {"name": a["name"], "id": a.get("id", ""), "icon": a.get("icon", ""),
                           "balance": None, "unrealized": None, "available": None,
                           "positions": [], "error": str(e)[:80]}
                    try:
                        row["series"] = storage.equity_series(a.get("id") or a["name"])
                        row["equityHistory"] = storage.equity_heatmap(a.get("id") or a["name"])
                    except Exception:
                        row["series"] = []
                        row["equityHistory"] = []
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
            now = KlineAggregator.now_ms()
            all_aggs = {**aggs, **watch_aggs}

            def needs_backfill(sym: str, iv: str, agg: KlineAggregator) -> bool:
                if len(agg.history) < 60:
                    return True
                expected_prev = now - now % INTERVAL_MS[iv] - INTERVAL_MS[iv]
                return agg.history[-1].open_time < expected_prev - 2 * INTERVAL_MS[iv]

            need = [(sym, iv, agg) for (sym, iv), agg in all_aggs.items()
                    if needs_backfill(sym, iv, agg)]
            for sym, iv, agg in need:
                unhealthy_symbols.add(sym)
                try:
                    candles = await asyncio.to_thread(
                        fetch_klines, sym, iv, limit, proxy_cfg or None)
                    if not candles:
                        continue
                    # 清掉实时累积的少量K线再整体 seed，避免历史重复
                    agg.history.clear()
                    agg.current = None
                    agg.seed(candles)
                    try:
                        storage.archive_candles(sym, iv, candles)
                    except Exception as e:
                        print(f"[archive] 重试 {sym} {iv} 归档失败: {e}")
                    keep_from = KlineAggregator.now_ms() - int(scfg.get("minutes", 30)) * 60_000
                    for c in candles:
                        if c.open_time >= keep_from:
                            storage.save_candle(sym, iv, c)
                    if (sym, iv) in aggs:
                        try:
                            hub.structures[(sym, iv)] = analyze_structure(
                                list(agg.history)[-160:])
                        except Exception:
                            pass
                    if not any(needs_backfill(s, i, a)
                               for (s, i), a in all_aggs.items() if s == sym):
                        unhealthy_symbols.discard(sym)
                    print(f"[backfill] 重试成功 {sym} {iv}: {len(candles)} 根")
                except Exception as e:
                    print(f"[backfill] 重试失败 {sym} {iv}: {e}")
            await asyncio.sleep(90)

    # 新闻：RSS 每 60 秒拉一次（策略的情绪分直接吃这批数据，10 分钟太钝），
    # 入库时连带保存已有正文；缺正文的交给后台预取队列慢慢补。
    async def news_poller():
        await asyncio.sleep(3)      # 等其它任务先起来
        # 启动时先把库里的存档补进 hub：重启后前端不会空着等第一次 RSS
        try:
            recent = await asyncio.to_thread(storage.news_recent, 300)
            if recent:
                hub.news = recent
                print(f"[news] 从存档恢复 {len(recent)} 条")
        except Exception as e:
            print(f"[news] 存档读取失败: {e}")
        while True:
            try:
                # 传入当前展示的币种：相关的新闻排前面（交易所公告 + 加密媒体 + 综合财经）
                shown = sorted(quant.symbol_filter or []) or symbols[:6]
                items = await asyncio.to_thread(
                    fetch_news, 10, proxy_cfg or None, shown, 80)
                if items:
                    hub.news = items
                    await hub.broadcast({"type": "news", "items": items})
                    saved = 0
                    try:
                        saved = await asyncio.to_thread(storage.save_news, items)
                    except Exception as e:
                        print(f"[news] 存档失败: {e}")
                    # RSS 里没带全文的，排进预取队列
                    for it in items:
                        if not it.get("content"):
                            enqueue_article(it.get("url"))
                    srcs = {}
                    for it in items:
                        srcs[it.get("source", "?")] = srcs.get(it.get("source", "?"), 0) + 1
                    with_content = sum(1 for it in items if it.get("content"))
                    print(f"[news] 已更新 {len(items)} 条（入库 {saved}，含全文 {with_content}）：" +
                          ", ".join(f"{k}×{v}" for k, v in sorted(srcs.items(), key=lambda x: -x[1])))
            except Exception as e:
                print(f"[news] 拉取失败: {e}")
            await asyncio.sleep(60)

    # 新闻 AI 分析：正文一入库就自动跑一次，结果落库；点开弹窗直接读。
    ai_lock = asyncio.Lock()

    async def _analyze_news(item: dict) -> None:
        url = str(item.get("url") or "")[:512]
        if not url:
            return
        async with ai_lock:
            try:
                res = await asyncio.to_thread(
                    analyze_news, item, cfg.get("ai", {}) or {}, proxy_cfg or None)
                if res.get("ok"):
                    await asyncio.to_thread(storage.save_news_ai, url, res["text"], "done")
                    print(f"[news] AI 分析已生成 {item.get('source', '?')}: {url[:50]}")
                else:
                    # 落 failed 而不是 pending：避免同一条反复重试烧 token
                    await asyncio.to_thread(storage.save_news_ai, url,
                                            f"分析失败：{res.get('error', '')}", "failed")
            except Exception as e:
                print(f"[news] AI 分析异常: {e}")
                try:
                    await asyncio.to_thread(storage.save_news_ai, url, f"分析异常：{e}", "failed")
                except Exception:
                    pass

    async def news_ai_poller():
        """补偿：已有正文但还没分析的新闻（启动前遗留、上次失败的除外）。"""
        await asyncio.sleep(25)
        while True:
            try:
                rows = await asyncio.to_thread(storage.news_ai_pending, 4)
                for row in rows:
                    await _analyze_news(row)
            except Exception as e:
                print(f"[news] AI 分析队列失败: {e}")
            await asyncio.sleep(120)

    async def article_prefetcher():
        """后台抓正文：一次一条、间隔 4 秒，失败退避，绝不并发轰炸源站。

        RSS 自带全文的源不用抓；只有确实缺正文的才发请求，且同一 URL
        只试一次（记录在 fetched_ts，30 分钟内不再重试）。
        """
        await asyncio.sleep(20)
        while True:
            if not article_queue:
                try:
                    for row in await asyncio.to_thread(storage.news_pending, 8):
                        article_queue.setdefault(row["url"], row)
                except Exception:
                    pass
            if not article_queue:
                await asyncio.sleep(15)
                continue
            url, item = article_queue.popitem()
            if url in article_busy:
                continue
            article_busy.add(url)
            limited = False
            try:
                res = await asyncio.to_thread(fetch_article, item, proxy_cfg or None)
                content = res.get("content") or ""
                error = str(res.get("error") or "")
                if res.get("ok") and content and not res.get("partial"):
                    await asyncio.to_thread(storage.save_news_content, url, content, False)
                    print(f"[news] 正文已入库 {item.get('source', '?')}: {url[:60]}")
                    # 正文一入库就自动分析，用户点开直接看结果，不用点按钮
                    ai_item = {**item, "content": content}
                    asyncio.create_task(_analyze_news(ai_item))
                else:
                    limited = "429" in error or "限流" in error
                    # 只拿到摘要/失败：也回写一次，避免 30 分钟内反复重试
                    await asyncio.to_thread(storage.save_news_content, url, content, True)
            except Exception as e:
                print(f"[news] 正文预取失败: {e}")
                limited = "429" in str(e)
            finally:
                article_busy.discard(url)
            # 被限流就整体冷却一轮：先清空队列，等一段时间再从库里挑待办
            if limited:
                article_queue.clear()
                print("[news] 源站限流，正文预取冷却 5 分钟")
                await asyncio.sleep(300)
            await asyncio.sleep(4)

    async def pool_auditor():
        """Daily advisory audit; never changes the live symbol pool."""
        if not decision_cfg.pool_audit_enabled:
            return
        await asyncio.sleep(120)
        while True:
            try:
                tickers = {}
                for sym in symbols:
                    item = await asyncio.to_thread(
                        ticker_24h, sym, proxy_cfg or None)
                    if item:
                        tickers[sym] = item
                onboard = await asyncio.to_thread(
                    onboard_dates, symbols, proxy_cfg or None)
                bars_by_symbol = {}
                for sym in symbols:
                    agg = watch_aggs.get((sym, "4h")) or aggs.get((sym, iv0))
                    if agg:
                        bars_by_symbol[sym] = list(agg.history)[-300:]
                report = audit_universe(
                    symbols, decision_cfg,
                    bars_by_symbol=bars_by_symbol,
                    depth_by_symbol=hub.depths,
                    tickers=tickers,
                    onboard_by_symbol=onboard,
                    now_ms=KlineAggregator.now_ms(),
                )
                quant.last_pool_audit = report
                if report["downgrade"]:
                    print("[pool] 降级建议: " + ", ".join(
                        f"{row['symbol']}({'/'.join(row['reasons'])})"
                        for row in report["downgrade"]))
            except Exception as e:
                print(f"[pool] 审计失败: {e}")
            await asyncio.sleep(86400)

    async def market_list_poller():
        """市池清单：启动即取一次（前端菜单要马上有），之后每小时刷新一次。"""
        while True:
            try:
                listed = await asyncio.to_thread(usdt_perpetuals, proxy_cfg or None)
                if listed:
                    hub.market = sorted(set(listed) | set(symbols))
                    await hub.broadcast({"type": "market", "market": hub.market})
                    print(f"[market] 可交易 U 本位永续 {len(hub.market)} 个")
            except Exception as e:
                print(f"[market] 市池获取失败: {e}")
            await asyncio.sleep(3600)

    # 衍生品指标（多空比 / 大户持仓 / 主动买卖 / OI / 资金费率）：
    # 币安 App 的「量化策略」是登录态专有接口，网页端拿不到；这里改用
    # /futures/data 公开接口，给同一市场里可验证的仓位与成交结构。
    async def derivatives_poller():
        await asyncio.sleep(6)
        while True:
            # 每轮重读 symbol_filter：用户新加的币要立刻有指标，
            # 否则它会在最长一个周期里显示「等待数据」。
            scope = list(quant.symbol_filter or symbols)
            # 每次都刷新全量：只补 missing 会让"这轮失败"的币被永久跳过，
            # 前端就一直停在"等待数据"。
            active = scope
            try:
                rows = await asyncio.gather(*[
                    asyncio.to_thread(derivatives_snapshot, sym, "5m", proxy_cfg or None)
                    for sym in active
                ], return_exceptions=True)
                snap = {}
                failures = []
                for sym, row in zip(active, rows):
                    if isinstance(row, dict) and row.get("price") is not None:
                        snap[sym] = row
                    else:
                        failures.append(f"{sym}:{type(row).__name__ if isinstance(row, Exception) else '无数据'}")
                if snap:
                    merged = {**hub.derivatives, **snap}
                    hub.derivatives = {k: v for k, v in merged.items() if k in set(scope)} or merged
                    hub.derivatives_ts = KlineAggregator.now_ms()
                    hub.derivatives_error = ""
                    await hub.broadcast({"type": "derivatives", "rows": hub.derivatives,
                                         "ts": hub.derivatives_ts, "error": ""})
                    print(f"[deriv] 衍生品指标已更新 {len(hub.derivatives)} 个币")
                if failures:
                    # 把失败原因一并下发：线上只看"等待数据"没法排查
                    hub.derivatives_error = f"{len(failures)} 个币取数失败：{failures[0]}"
                    await hub.broadcast({"type": "derivatives", "rows": hub.derivatives,
                                         "ts": hub.derivatives_ts,
                                         "error": hub.derivatives_error})
                    print(f"[deriv] 部分失败 {len(failures)} 个: {failures[:3]}")
            except Exception as e:
                hub.derivatives_error = f"指标刷新失败: {e}"
                print(f"[deriv] 指标刷新失败: {e}")
                try:
                    await hub.broadcast({"type": "derivatives", "rows": hub.derivatives,
                                         "ts": hub.derivatives_ts, "error": hub.derivatives_error})
                except Exception:
                    pass
            # 币种变化时立刻醒来补数据，否则等满 60 秒
            try:
                await asyncio.wait_for(deriv_wake.wait(), timeout=60)
                deriv_wake.clear()
            except asyncio.TimeoutError:
                pass

    async def market_feature_poller():
        """Prefetch OI/funding without blocking the quant decision lock."""
        await asyncio.sleep(4)
        while True:
            active = list(quant.symbol_filter or symbols)
            try:
                await asyncio.gather(*[
                    asyncio.to_thread(_fetch_market_data, sym) for sym in active
                ])
            except Exception as e:
                print(f"[market] 衍生品快照刷新失败: {e}")
            await asyncio.sleep(60)

    async def chatter_poller():
        """他自己找话说的节奏：不是固定间隔，而是带抖动的随机浮动。

        用户原话："定时太死板了，要的是随机性"。
        固定 60 分钟 会让每条消息都在整点附近出现，一眼看出是机器；
        这里每次醒来后重新抽一个 35~95 分钟的间隔，再叠加概率闸门
        （chatter 类事件基础概率 0.35），所以真正开口的时刻不可预测。

        要不要说、说什么全交给 Agent：可能去查新闻聊币圈八卦，
        可能拿今天那笔交易出来炫耀，也可能什么都不说。
        """
        await asyncio.sleep(300)          # 启动先别急着说话
        while True:
            interval = random.uniform(35 * 60, 95 * 60)
            await asyncio.sleep(interval)
            try:
                _proactive_dispatch(
                    "chatter",
                    "你自己想聊点什么（可以查新闻、聊币圈八卦、聊今天的交易），"
                    "不想说就不说",
                    dedupe=f"chatter:{int(KlineAggregator.now_ms() // 3600_000)}")
            except Exception:
                pass

    try:
        await asyncio.gather(supervisor(), sweep(), accounts_poller(),
                             news_poller(), article_prefetcher(), news_ai_poller(),
                             telegram_supervisor(),
                             backfill_retry(), pool_auditor(), chatter_poller(),
                             market_list_poller(), leverage_poller(),
                             derivatives_poller(), market_feature_poller())
    finally:
        await agent.stop()


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
    # 明文审计必须看**磁盘上的原始配置**：解密后的 cfg 里每个字段都是明文，
    # 拿它去审计会永远报"仍是明文"，日志提示就失去意义了。
    _plain = secrets_store.audit_plaintext(cfg)
    # 密钥：配置文件里的 enc:v1:... 解密；环境变量优先覆盖（KLINE_* 系列）。
    # 解不开就拒绝启动——带着错误密钥去连交易所/数据库比停服务更危险。
    try:
        cfg = secrets_store.decrypt_config(cfg, args.config)
    except secrets_store.SecretsError as e:
        print(f"[config] 密钥解密失败，拒绝启动：{e}")
        raise SystemExit(2) from None
    if _plain:
        print(f"[config] 安全提示：{', '.join(_plain[:8])} 仍是明文；"
              f"运行 python tools/encrypt_config.py 可加密（或设置对应环境变量覆盖）")
    else:
        print("[config] 密钥项均已加密或在环境变量中提供")
    try:
        secrets_store.harden_permissions(args.config)
    except Exception:
        pass
    try:
        asyncio.run(run(cfg, args.config))
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
