"""量化交易引擎：形态信号 → 多周期确认 → 共鸣评分 → 统一风控 → 执行/保护。

Decision states are explicit and replayable:
  · 4h EMA gate + at least two independent 1h confirmations
  · MACD/Keltner/CVD/VSA/OBI/OI/funding/news sentiment contribute to a
    regime-weighted confluence score; safety vetoes remain separate
  · daily loss/profit lock, loss streak, portfolio heat, correlation buckets,
    risk-budget sizing, Kelly cap, rate/notional limits and drift de-risking
  · exchange-held STOP_MARKET/TAKE_PROFIT_MARKET orders are reconciled every
    five minutes; software checks remain as a second layer
  · every fill records entry/exit slippage, score breakdown and the plan used

No UI state lives here. All new factors and switching logic are backend-only.
"""
from __future__ import annotations

import functools
import copy
import json
import math
import os
import re
import threading
import time
from collections import deque

from accounts import (
    signed_request, open_orders, cancel_order, query_order, cancel_all_open_orders,
    place_protective_order, cancel_algo_orders,
)
from analysis import indicators as IND
from analysis import risk_metrics as RISK
from decision.advice import build_advice
from decision.config import DecisionConfig
from decision.drift import ADWIN
from decision.paper import PaperBook
from decision.features import (
    cvd_metrics, funding_context, macd, open_interest_bias,
    order_book_imbalance, returns as return_series, squeeze_state,
    vsa_metrics,
)
from decision.risk import PositionRiskInput, RiskEngine, RiskInput, TradeRecord
from decision.regime import hmm_regime
from decision.scoring import score_signal
from decision.watch import build_watch_plan, ema_direction
from feed.sentiment import aggregate_news_sentiment

BASE = "https://testnet.binancefuture.com"


def _serialized(fn):
    """引擎操作串行化：on_signal/manage/audit 里的交易所调用在线程池中执行，
    可能并发进入；一把粗锁保证状态互斥，同时不阻塞 K线 主事件循环。"""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._op_lock:
            return fn(self, *args, **kwargs)
    return wrapper

# 止盈止损标定历经三版（每版都是真实成交样本驱动）：
#   v1 SL-0.35%/TP+0.75%：9 笔胜率 33%，止损被正常波动扫出
#   v2 SL-0.50%/TP+0.75%：19 笔胜率 47%，但 0 笔触及止盈
#   v3 SL-0.50%/TP+0.35%：20 笔仍然 0 笔触及止盈，全部超时出场（笔均 -0.16）
# 结论：3m 周期 45 分钟窗口内，+0.35% 仍然偏高。实测 20 笔中曾出现过的最大浮盈多在
# +0.10~0.23% 区间，故止盈下调到 +0.18%（约 3m 振幅中位的 2 倍，可稳定触及）。
SL_PCT = -0.005      # 止损 -0.50%（保持，避免被正常波动扫出）
TP_PCT = 0.0018      # 止盈 +0.18%（可触及区间，配合超时兜底）
MAX_HOLD_MIN = 60    # 超时平仓：持仓超过 60 分钟未触发止盈止损则市价了结，释放槽位
# 移动止盈（解决"浮盈变浮亏"）：34 笔样本中 24 笔超时出场合计 -4.2，
# 多数曾有小额浮盈但回落到超时平仓时已转亏。规则：
#   · 浮盈 ≥ TRAIL_ARM（+0.10%）后启用保护
#   · 从最高浮盈回撤 ≥ TRAIL_GAP（0.06%）即平仓，锁定剩余利润
#   · 未达 +0.10% 的仓位不启用（避免正常波动频繁触发）
TRAIL_ARM = 0.0010   # 启用移动止盈的浮盈门槛 +0.10%
TRAIL_GAP = 0.0006   # 从峰值回撤多少平仓 0.06%
MARGIN_PCT = 0.05    # 单仓保证金占可用余额比例
MARGIN_MIN = 10.0    # 单仓保证金下限（USDT）
MAX_POSITIONS = 4    # 每账户最大同时持仓数
MAX_SAME_DIR = 3     # 同方向最大持仓数（防止信号聚集导致方向过度集中）
COOLDOWN_SEC = 900   # 同币种冷却 15 分钟
AUDIT_SEC = 300      # 自审计间隔 5 分钟

# ---- ATR 自适应止损止盈（替代固定百分比） ----
# 固定 SL-0.5%/TP+0.18% 的问题是：低波动时止盈摸不到、高波动时止损被噪声扫掉。
# 改成按 ATR 倍数给距离，再夹到 [min,max] 百分比区间兜底（ATR 异常时不会失控）。
USE_ATR_STOPS = True
ATR_SL_MULT = 1.5        # 止损 = 1.5 × ATR
ATR_TP_MULT = 2.5        # 止盈 = 2.5 × ATR（盈亏比 1.67）
SL_MIN_PCT = 0.004       # 止损距离下限 0.40%
SL_MAX_PCT = 0.020       # 止损距离上限 2.0%
TP_MIN_PCT = 0.0018      # 止盈距离下限 0.18%
TP_MAX_PCT = 0.030       # 止盈距离上限 3.0%

# ---- 回撤熔断 ----
DD_LIMIT_PCT = 0.05      # 账户从峰值回撤超过 5% → 暂停开仓
DD_PAUSE_MIN = 30        # 暂停多久（分钟）后自动尝试恢复
DD_LOOKBACK_MS = 24 * 3600_000   # 回撤统计窗口：24 小时

# ---- 市场状态过滤（第二层，形态之外） ----
USE_MARKET_GATE = True
GATE_MAX_LOG = 40        # 保留最近的拦截理由条数（给前端展示）
# 计算流水保留条数。5 秒一轮 × 5 币 × 约 15 行 ≈ 75 行/轮，
# 3000 条够翻好几轮历史；只在内存里，不落库。
CALC_MAX_ROWS = 3000
# 方向切换的最小分差。低于这个值维持上一轮方向——否则每 5 秒
# 一重算、谁高 0.1 分就翻，用户跟着做就是来回交手续费。
HYSTERESIS_GAP = 8.0
# 计算流水里分项的单字标签（列很窄，放不下英文）
SHORT_TAG = {"trend": "势", "momentum": "能", "volume": "量", "structure": "构"}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _as_int(value, default: int = 0) -> int:
    """尽力转 int（落库/恢复的容错转换，坏值回退默认值，不抛异常）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _atomic_write_json(path: str, data) -> bool:
    """原子写本地 JSON（审计 P1：直接 open("w") 在崩溃时会留下半截文件）。

    同目录 `.tmp` → json.dump → flush + fsync → os.replace 覆盖正式文件。
    任何异常都吞掉并返回 False，绝不抛给调用方（与原来的 try/except pass 语义一致）。
    """
    tmp_path = f"{path}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        return True
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return False


class QuantEngine:
    def __init__(self, accts_box: dict, config_path: str, price_fn=None,
                 min_pattern_winrate: float = 0.35, min_pattern_samples: int = 5,
                 decision_config: DecisionConfig | None = None,
                 paper_store=None):
        self.accts_box = accts_box          # main 的账户盒子（list/refresh）
        self.config_path = config_path
        self.price_fn = price_fn            # symbol -> 最新价（来自聚合器）
        self.enabled = True
        self.decision_cfg = decision_config or DecisionConfig()
        self.risk = RiskEngine(self.decision_cfg)
        # 信号质量门：历史胜率过低的形态不再入场（与形态统计系统联动）
        self.pattern_stats_fn = None        # main 注入：storage.pattern_stats
        self.min_pattern_winrate = min_pattern_winrate
        self.min_pattern_samples = min_pattern_samples
        self._op_lock = threading.RLock()   # 引擎操作互斥（配合线程池卸载）
        self.state: dict[str, dict] = {}    # name -> {positions:{sym:{}}, cooldown:{sym:ts}, trades:[], seq:int}
        self.events: deque = deque(maxlen=60)   # 决策/事件流水（广播给前端）
        self.stats = {"signals": 0, "decisions": 0, "orders": 0, "closes": 0, "errors": 0, "wins": 0, "losses": 0}
        self.last_audit_ms = 0
        self.broadcast_fn = None           # main 注入：asyncio.create_task(hub.broadcast(obj))
        self.storage_fn = None             # main 注入：storage.save_quant_event（事件落库）
        self.trade_storage_fn = None       # main 注入：storage.save_quant_trade（仓位历史落库）
        self.bars_fn = None                # main 注入：symbol -> list[Candle]（指标计算用）
        self.equity_fn = None              # main 注入：() -> list[float]（回撤熔断用）
        # 决策 trace/gate 与风控/漂移状态持久化（main 注入 storage；None = 纯内存）
        self.trace_storage_fn = None       # main 注入：storage.save_decision_trace(trace, kind)
        self.trace_read_fn = None          # main 注入：storage.decision_traces(limit, kind)，newest-first
        self.state_storage_fn = None       # main 注入：storage.save_quant_state(name, data)
        self.state_read_fn = None          # main 注入：storage.quant_state(name)
        self._last_state_save = 0          # 上次风控/漂移状态落库时刻（节流用）
        self.indicators: dict[str, dict] = {}   # symbol -> 市场状态（每轮刷新）
        self.live_features: dict[str, dict] = {}   # symbol -> 实时因子快照
        self.live_evaluations: dict[str, dict] = {}
        self.live_score_history: dict[str, deque] = {}
        self.gate_log: deque = deque(maxlen=GATE_MAX_LOG)  # 最近的过滤器拦截记录
        # 计算流水：每一轮每个币的**每一条**算式都记一行，供前端实时滚动。
        # 只保留最近若干条（内存环形缓冲，不落库）——这是运算轨迹，
        # 不是业务数据，重算就能得到。
        self.calc_stream: deque = deque(maxlen=CALC_MAX_ROWS)
        # 模拟仓：每个监控币跑一个 15 分钟的纸上交易，平仓即重开。
        # 仓位与平仓记录都落 MySQL（paper_positions / paper_trades）：
        # 重启后从库里恢复，统计口径也不再是"进程启动以来"。
        # main 注入 storage；单元测试传 None 时退回纯内存。
        self.paper = PaperBook(store=paper_store)
        self.calc_storage_fn = None     # main 注入：rows -> None（写库，只留 1 分钟）
        self.calc_read_fn = None        # main 注入：(seconds) -> rows（冷启动补最近数据）
        self.dd_state = {"paused_until": 0, "current": 0.0, "max": 0.0, "peak": 0.0}
        # 交易范围：只处理前端实际展示的币种（None/空 = 不限制）。
        # 否则引擎会在用户没看K线的币上开仓，持仓出现"凭空冒出来"的仓位。
        self.symbol_filter: set[str] | None = None
        self._err_dedupe: dict[str, int] = {}
        self._mark_cache: dict[str, tuple] = {}
        self._unsupported: set[str] = set()   # 未开通合约权限的币种（如 TradFi 需签协议）
        # 行情是否真实：main 注入 lambda，返回 False 时拒绝一切新开仓。
        # 演示/模拟数据上的形态是随机噪声，绝不能变成真实委托。
        self.feed_is_live_fn = None
        self._lock = threading.Lock()
        self._warned_dupes = False
        self._step_sizes: dict[str, float] = {}
        self._tick_sizes: dict[str, float] = {}
        self._log_path = os.path.join(os.path.dirname(os.path.abspath(config_path)), "quant.log")
        self._trades_path = os.path.join(os.path.dirname(os.path.abspath(config_path)), "quant-trades.json")
        self._pos_path = os.path.join(os.path.dirname(os.path.abspath(config_path)), "quant-positions.json")
        self._unsupported_path = os.path.join(os.path.dirname(os.path.abspath(config_path)), "quant-unsupported.json")
        self._manual_path = os.path.join(os.path.dirname(os.path.abspath(config_path)), "quant-manual.json")
        self._manual: set[tuple[str, str]] = set()   # (account, symbol) 手动仓位：引擎不干预止盈止损
        self._manual_mtime = 0.0
        self.max_leverage_fn = None        # symbol -> 交易所该合约的最高杠杆
        self.depth_fn = None               # symbol -> depth snapshot
        self.market_data_fn = None         # symbol -> {oi, funding}
        self.news_fn = None                # () -> current news items
        self.liquidation_fn = None         # symbol -> cascade snapshot
        self.watch_bars_fn = None          # symbol -> (entry, h1, h4)
        self.data_health_fn = None         # () -> bool
        self._balance_cache: dict[str, tuple[float, float, int]] = {}
        self._hmm_cache: dict[tuple[str, int], dict] = {}
        self._recent_entry_slippage: deque = deque(maxlen=3)
        self._rest_pause_until = 0
        self._permission_pause_until = 0
        self.decision_traces: deque = deque(maxlen=24)
        self.latest_traces: dict[str, dict] = {}
        self._trace_seq = 0
        self._drift_detectors: dict[str, dict[str, ADWIN]] = {}
        self._drift_last_alert: dict[tuple[str, str], int] = {}
        self._drift_last_global_alert = 0
        self._drift_caution_until = 0
        self._drift_shadow_until = 0
        self._vol_drift = ADWIN(self.decision_cfg.drift_delta)
        self._vol_last_bar: dict[str, int] = {}
        self._last_reconcile = 0        # 上次与交易所对账的时刻（毫秒）
        self._load_trades()
        self._seed_risk_history()
        self._load_positions()
        self._load_unsupported()
        self._event("system", "量化引擎已启动，开始盯盘", enabled=True)

    # ---------- 记录 ----------

    def _event(self, kind: str, reason: str = "", **extra):
        ev = {"ts": _now_ms(), "kind": kind, "reason": reason, **extra}
        self.events.append(ev)
        # 错误限流：同一原因 1 分钟内只落盘一次，避免异常循环刷爆日志（事件流仍保留）
        if kind == "error":
            key = reason[:60]
            last = self._err_dedupe.get(key, 0)
            if ev["ts"] - last < 60000:
                return ev
            self._err_dedupe[key] = ev["ts"]
        try:
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except Exception:
            pass
        # 持久化到 MySQL（按账户隔离，重启/刷新不清空）；main 注入 storage
        if self.storage_fn:
            try:
                ev["id"] = self.storage_fn(ev)
            except Exception:
                pass
        # 单条实时推送：前端据此逐条入场；不要等 5 秒快照（那样会同批涌入一堆）
        if self.broadcast_fn and ev.get("id"):
            try:
                self.broadcast_fn({"type": "quant_event", "event": ev})
            except Exception:
                pass
        return ev

    def _trace_start(self, hit: dict, price: float | None) -> dict:
        self._trace_seq += 1
        now = _now_ms()
        trace = {
            "id": f"tr-{now}-{self._trace_seq}",
            "ts": now,
            "updatedAt": now,
            "symbol": str(hit.get("symbol") or "").lower(),
            "pattern": hit.get("pattern"),
            "nameZh": hit.get("name_zh"),
            "direction": hit.get("direction"),
            "price": float(price) if price else None,
            "status": "running",
            "statusLabel": "计算中",
            "account": "",
            "stages": [],
            "factors": {},
            "score": None,
            "watch": None,
            "risk": None,
            "execution": None,
        }
        return trace

    def _trace_stage(self, trace: dict | None, stage_id: str, label: str,
                     status: str, summary: str = "", **data) -> None:
        if trace is None:
            return
        stage = {
            "id": stage_id, "label": label, "status": status,
            "summary": summary, "ts": _now_ms(), **data,
        }
        stages = trace.setdefault("stages", [])
        for index, existing in enumerate(stages):
            if existing.get("id") == stage_id:
                stages[index] = stage
                break
        else:
            stages.append(stage)
        trace["updatedAt"] = stage["ts"]
        trace["status"] = "running" if status == "running" else trace.get("status", "running")

    def _finish_trace(self, trace: dict | None, status: str, label: str,
                      summary: str = "", **data) -> None:
        if trace is None:
            return
        trace.update({
            "status": status, "statusLabel": label,
            "updatedAt": _now_ms(), "summary": summary, **data,
        })
        self.decision_traces.append(trace)
        self.latest_traces[trace["symbol"]] = trace
        # 决策流水落库（审计 P0：重启后 trace 不能只剩内存）。storage 负责 json 序列化。
        if self.trace_storage_fn:
            try:
                self.trace_storage_fn(trace, "trace")
            except Exception:
                pass

    def _append_gate(self, entry: dict) -> None:
        """记录一条过滤器拦截：内存环形缓冲 + 可选落库（重启后仍能回看拦截原因）。"""
        self.gate_log.append(entry)
        if self.trace_storage_fn:
            try:
                self.trace_storage_fn(entry, "gate")
            except Exception:
                pass

    # ---------- 跨重启状态：trace / gate / 风控 / 漂移 ----------

    def restore_persistent_state(self) -> dict:
        """启动时从 storage 恢复决策流水与风控/漂移状态（每段独立容错，绝不抛）。"""
        restored = {"traces": 0, "gates": 0, "risk": False, "drift": False}
        if self.trace_read_fn:
            # 决策 trace：storage 返回 newest-first，reversed 后按时间顺序回填
            try:
                rows = self.trace_read_fn(24, "trace")
                if isinstance(rows, (list, tuple)):
                    for trace in reversed(list(rows)):
                        if not isinstance(trace, dict):
                            continue
                        self.decision_traces.append(trace)
                        restored["traces"] += 1
                        symbol = trace.get("symbol")
                        if isinstance(symbol, str) and symbol:
                            # 按时间顺序覆盖，最终保留最新一条
                            self.latest_traces[symbol] = trace
            except Exception:
                pass
            # gate 拦截记录：同样 newest-first → reversed 后回填
            try:
                rows = self.trace_read_fn(GATE_MAX_LOG, "gate")
                if isinstance(rows, (list, tuple)):
                    for entry in reversed(list(rows)):
                        if isinstance(entry, dict):
                            self.gate_log.append(entry)
                            restored["gates"] += 1
            except Exception:
                pass
        if self.state_read_fn:
            try:
                data = self.state_read_fn("risk")
                if isinstance(data, dict):
                    restored["risk"] = self.risk.restore(data) > 0
            except Exception:
                pass
            try:
                data = self.state_read_fn("drift")
                if isinstance(data, dict):
                    self._drift_caution_until = max(
                        0, _as_int(data.get("cautionUntil"), 0))
                    self._drift_shadow_until = max(
                        0, _as_int(data.get("shadowUntil"), 0))
                    restored["drift"] = True
            except Exception:
                pass
        return restored

    def save_persistent_state(self, force: bool = False) -> None:
        """把风控/漂移状态落库；非 force 时 60 秒节流（manage 每 5 秒一轮）。

        成交（_record_trade）和漂移事件（_handle_drift）用 force 立即落库，量很小。
        """
        if not self.state_storage_fn:
            return
        now = _now_ms()
        if not force and now - self._last_state_save < 60_000:
            return
        try:
            self.state_storage_fn("risk", self.risk.snapshot())
        except Exception:
            pass
        try:
            self.state_storage_fn("drift", {
                "cautionUntil": max(0, _as_int(self._drift_caution_until, 0)),
                "shadowUntil": max(0, _as_int(self._drift_shadow_until, 0)),
            })
        except Exception:
            pass
        self._last_state_save = now

    def _load_trades(self):
        self.trades: list[dict] = []
        try:
            if os.path.exists(self._trades_path):
                with open(self._trades_path, encoding="utf-8") as f:
                    self.trades = json.load(f)
        except Exception as e:
            self._event("error", f"交易历史读取失败: {e}")

    def _seed_risk_history(self) -> None:
        grouped: dict[str, list[TradeRecord]] = {}
        for trade in self.trades[-500:]:
            account = str(trade.get("account") or "")
            if not account:
                continue
            grouped.setdefault(account, []).append(TradeRecord(
                ts=int(trade.get("ts") or 0),
                pnl=float(trade.get("pnl") or 0),
                symbol=str(trade.get("symbol") or ""),
                side=str(trade.get("side") or ""),
                reason=str(trade.get("reason") or ""),
                pattern=str(trade.get("pattern") or ""),
            ))
        now = _now_ms()
        for account, records in grouped.items():
            self.risk.seed_trades(account, records, now)

    def _drift_for(self, account: str) -> dict[str, ADWIN]:
        if account not in self._drift_detectors:
            self._drift_detectors[account] = {
                "winrate": ADWIN(self.decision_cfg.drift_delta),
                "normalizedPnl": ADWIN(self.decision_cfg.drift_delta),
            }
        return self._drift_detectors[account]

    def _handle_drift(self, account: str, metric: str, detector: ADWIN) -> None:
        if not self.decision_cfg.drift_enabled:
            return
        now = _now_ms()
        key = (account, metric)
        if now - self._drift_last_alert.get(key, 0) < 3_600_000:
            return
        if now - self._drift_last_global_alert < 30 * 60_000:
            return
        self._drift_last_alert[key] = now
        self._drift_last_global_alert = now
        detail = detector.last_split or {}
        if now >= self._drift_caution_until:
            self._drift_caution_until = now + self.decision_cfg.drift_caution_hours * 3600_000
            level = "caution"
        else:
            self._drift_shadow_until = now + self.decision_cfg.drift_shadow_hours * 3600_000
            level = "shadow"
        self._event(
            "drift",
            f"检测到 {metric} 机制漂移（{level}）："
            f"{detail.get('mean0')} → {detail.get('mean1')}",
            account=account, metric=metric, driftLevel=level, **detail,
        )
        # 降险窗口变更立即落库（caution/shadow 跨重启必须保留）
        self.save_persistent_state(force=True)

    def _observe_trade_drift(self, account: str, trade: TradeRecord,
                             risk_multiple: float) -> None:
        detector = self._drift_for(account)
        win = 1.0 if trade.pnl > 0 else 0.0
        if detector["winrate"].add(win):
            self._handle_drift(account, "winrate", detector["winrate"])
        if detector["normalizedPnl"].add(risk_multiple):
            self._handle_drift(account, "normalizedPnl", detector["normalizedPnl"])

    def _record_trade(self, trade: dict):
        self.trades.append(trade)
        # 原子写：崩溃/断电不会把 quant-trades.json 写成半截（异常在函数内吞掉）
        _atomic_write_json(self._trades_path, self.trades[-500:])
        # 落库（仓位历史，按账户保留 7 天）；返回 id 后单条广播给前端
        if self.trade_storage_fn:
            try:
                tid = self.trade_storage_fn(trade)
                if tid:
                    trade["id"] = tid
                    if self.broadcast_fn:
                        self.broadcast_fn({"type": "quant_trade", "trade": {**trade, "id": tid}})
            except Exception:
                pass
        self.risk.record_trade(str(trade.get("account") or ""), TradeRecord(
            ts=int(trade.get("ts") or _now_ms()),
            pnl=float(trade.get("pnl") or 0),
            symbol=str(trade.get("symbol") or ""),
            side=str(trade.get("side") or ""),
            reason=str(trade.get("reason") or ""),
            pattern=str(trade.get("pattern") or ""),
        ))
        # 成交改变风控状态（连亏/日盈亏/冷却），立即落库不等 manage 节流
        self.save_persistent_state(force=True)
        atr_at_entry = float(trade.get("atrAtEntry") or 0)
        risk_multiple = (
            float(trade.get("pnl") or 0) /
            max(1e-9, atr_at_entry * float(trade.get("qty") or 0))
        )
        self._observe_trade_drift(str(trade.get("account") or ""), TradeRecord(
            ts=int(trade.get("ts") or _now_ms()),
            pnl=float(trade.get("pnl") or 0),
            symbol=str(trade.get("symbol") or ""),
            side=str(trade.get("side") or ""),
            reason=str(trade.get("reason") or ""),
            pattern=str(trade.get("pattern") or ""),
        ), risk_multiple)

    def _save_positions(self):
        """未平仓持仓落盘：重启/部署后恢复 entry 价与开仓时间，避免依赖审计补记。"""
        data = {name: st["positions"] for name, st in self.state.items() if st.get("positions")}
        _atomic_write_json(self._pos_path, data)

    def _load_positions(self):
        try:
            if not os.path.exists(self._pos_path):
                return
            with open(self._pos_path, encoding="utf-8") as f:
                data = json.load(f)
            for name, poss in (data or {}).items():
                if isinstance(poss, dict):
                    st = self._st(name)
                    st["positions"] = poss
            n = sum(len(v.get("positions", {})) for v in self.state.values())
            if n:
                self._event("system", f"已恢复上次未平的 {n} 个仓位，继续监控")
        except Exception as e:
            self._event("error", f"持仓状态恢复失败: {e}")

    def reconcile_positions(self) -> None:
        """和交易所对账：本地有、交易所没有的仓位一律清掉。

        历史问题：持仓按「账户名」存盘，账户改名后旧名字下的记录永远恢复
        不出来也删不掉，引擎会一直以为有名无实的仓位在跑（表现为 AI 说
        有 3 笔空单、账户卡却是空仓、权益也不动）。这里以交易所为准。
        """
        tradable = {a.get("name"): a for a in self._tradable_accounts()}
        if not tradable:
            return
        removed = []
        for name in list(self.state.keys()):
            positions = self._st(name)["positions"]
            if not positions:
                continue
            account = tradable.get(name)
            if account is None:
                # 该账户在配置里已不存在（改名/删号）：这些仓位无法再被管理
                removed.extend(f"{name}:{sym}" for sym in positions)
                self.state[name]["positions"] = {}
                continue
            for symbol in list(positions.keys()):
                live = self._live_amt(account["api_key"], account["api_secret"], symbol)
                if live is None:
                    continue                      # 查不到就别乱删
                if abs(live) < 1e-12:
                    positions.pop(symbol, None)
                    removed.append(f"{name}:{symbol}")
        if removed:
            self._event("system",
                        f"对账清理 {len(removed)} 个交易所已不存在的仓位："
                        + ", ".join(removed[:6]))
            try:
                self._save_positions()
            except Exception:
                pass

    # ---------- 手动仓位注册表（文件热加载：外部脚本开仓后引擎自动放行） ----------

    def _manual_reload(self) -> None:
        try:
            mtime = os.path.getmtime(self._manual_path)
        except OSError:
            if self._manual:
                self._manual = set()
                self._manual_mtime = 0
            return
        if mtime == self._manual_mtime:
            return
        self._manual_mtime = mtime
        try:
            with open(self._manual_path, encoding="utf-8") as f:
                self._manual = {tuple(x.split("|", 1)) for x in (json.load(f) or [])}
            self._event("system", f"手动仓位登记已更新：共 {len(self._manual)} 个（引擎不管这些仓的止盈止损）",
                        manual=sorted(f"{a}/{s}" for a, s in self._manual))
        except Exception as e:
            self._event("error", f"手动仓位注册表读取失败: {e}")

    # ---------- 交易所辅助 ----------

    def _tradable_accounts(self) -> list:
        """返回参与交易的账户：按 API Key 去重（同一 key = 同一账户，
        多个别名会让同一信号重复下单，导致叠加仓位与平仓错乱）。"""
        seen = {}
        out = []
        dup_names = []
        for a in self.accts_box["list"]:
            key = (a.get("api_key") or "").strip()
            if not key:
                continue
            if key in seen:
                dup_names.append(a.get("name"))
                continue
            seen[key] = a.get("name")
            out.append(a)
        if dup_names and not self._warned_dupes:
            self._warned_dupes = True
            self._event("system",
                        f"{'、'.join(dup_names)} 和主账户是同一个 API Key，已跳过（一个账户只交易一份）")
        return out

    def _acc(self, name):
        for a in self.accts_box["list"]:
            if a.get("name") == name:
                return a
        return None

    def _primary_account(self) -> str:
        """当前唯一账户名（单账户模式）。用于给"信号入口"这类尚未分派到账户
        的事件补归属，保证决策流水按账户筛得到。"""
        accs = self._tradable_accounts()
        return accs[0].get("name", "") if accs else ""

    def _step_size(self, api_key: str, api_secret: str, symbol: str) -> float:
        if symbol in self._step_sizes:
            return self._step_sizes[symbol]
        step = 0.001
        try:
            info = signed_request(api_key, api_secret, "/fapi/v1/exchangeInfo")
            for s in info.get("symbols", []):
                if s.get("symbol") == symbol.upper():
                    for f in s.get("filters", []):
                        if f.get("filterType") == "LOT_SIZE":
                            step = float(f.get("stepSize", 0.001))
                            break
                    break
        except Exception as e:
            self._event("error", f"exchangeInfo 获取失败({symbol}): {e}")
        self._step_sizes[symbol] = step
        return step

    def _quantize(self, qty: float, step: float) -> float:
        """按 stepSize 向下取整，并消除浮点尾差（10.45/0.01 → 10.450000000000001 会被交易所拒绝）。"""
        if step <= 0:
            return qty
        # 用整数运算避免浮点累积误差：先算步数（加微小 epsilon 防止 1044.9999 被 floor 成 1044）
        steps = int(math.floor(qty / step + 1e-9))
        decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
        return round(steps * step, decimals)

    def _tick_size(self, api_key: str, api_secret: str, symbol: str) -> float:
        if symbol in self._tick_sizes:
            return self._tick_sizes[symbol]
        tick = 0.0
        try:
            info = signed_request(api_key, api_secret, "/fapi/v1/exchangeInfo")
            for row in info.get("symbols", []):
                if row.get("symbol") != symbol.upper():
                    continue
                for item in row.get("filters", []):
                    if item.get("filterType") == "PRICE_FILTER":
                        tick = float(item.get("tickSize", 0) or 0)
                        break
                break
        except Exception as e:
            self._event("error", f"exchangeInfo 价格步长获取失败({symbol}): {e}",
                        symbol=symbol)
        self._tick_sizes[symbol] = tick
        return tick

    def _place_order(self, api_key, api_secret, symbol, side, qty, reduce_only=False,
                     order_type: str = "MARKET", price: float | None = None,
                     time_in_force: str | None = None) -> dict:
        params = {"symbol": symbol.upper(), "side": side, "type": order_type,
                  "quantity": qty, "newOrderRespType": "RESULT"}
        if order_type != "MARKET":
            if price is None:
                raise ValueError("价格单必须提供 price")
            params["price"] = price
            params["timeInForce"] = time_in_force or "GTC"
        if reduce_only:
            params["reduceOnly"] = "true"
        # 下单必须 POST（GET 会被路由到查询订单接口，报 -1102）
        try:
            r = signed_request(api_key, api_secret, "/fapi/v1/order",
                               params, method="POST")
        except Exception as e:
            self._classify_api_error(e, symbol)
            raise
        self.stats["orders"] += 1
        return r

    def _classify_api_error(self, error: Exception, symbol: str) -> None:
        message = str(error)
        now = _now_ms()
        if "-1003" in message:
            self._rest_pause_until = max(self._rest_pause_until, now + 30 * 60_000)
            self._event("error", "交易所 REST 限频，全局暂停开仓 30 分钟",
                        symbol=symbol, apiCode="-1003")
        elif "-2015" in message or "-2014" in message:
            self._permission_pause_until = max(
                self._permission_pause_until, now + 30 * 60_000)
            self._event("error", "API Key/权限异常，全局暂停开仓并等待人工检查",
                        symbol=symbol, apiCode="-2015/-2014")
        elif "-1021" in message:
            self._event("error", "交易所时间戳越窗，请检查本机时钟/代理链路",
                        symbol=symbol, apiCode="-1021")
        elif "-4131" in message:
            self._event("error", "超出交易所仓位风险限额，需下调名义上限",
                        symbol=symbol, apiCode="-4131")

    def _mark_unsupported(self, symbol: str, err: Exception) -> bool:
        """识别"该合约未开通"类错误（TradFi 永续需先签协议 -4411 或 -4131），
        标记后不再对该币种下单，避免反复失败刷日志。返回是否属于此类。"""
        msg = str(err)
        if any(code in msg for code in ("-4411", "-4131")) or "tradfi" in msg.lower():
            self._unsupported.add(symbol)
            self._save_unsupported()
            self._event("system",
                        f"{symbol} 未开通合约权限（TradFi 协议未签署），已加入跳过列表不再尝试",
                        symbol=symbol)
            return True
        return False

    def _save_manual(self):
        _atomic_write_json(self._manual_path, sorted(f"{a}|{s}" for a, s in self._manual))

    def _save_unsupported(self):
        _atomic_write_json(self._unsupported_path, sorted(self._unsupported))

    def _load_unsupported(self):
        try:
            if os.path.exists(self._unsupported_path):
                with open(self._unsupported_path, encoding="utf-8") as f:
                    self._unsupported = set(json.load(f) or [])
                if self._unsupported:
                    self._event("system", f"这些币种之前下单失败过，已自动跳过：{'、'.join(sorted(s.upper() for s in self._unsupported))}")
        except Exception:
            pass

    # ---------- 状态 ----------

    def _st(self, name: str) -> dict:
        with self._lock:
            if name not in self.state:
                self.state[name] = {"positions": {}, "cooldown": {}, "seq": 0}
            return self.state[name]

    # ---------- 市场状态（指标 + 状态判定） ----------

    def refresh_indicators(self) -> None:
        """为每个持仓/候选币种刷新全部技术指标。由 manage 周期调用（5 秒一次）。"""
        if not self.bars_fn:
            return
        syms = set()
        for name in self.state:
            syms.update(self._st(name)["positions"].keys())
        # 前端正在展示的币必须全部纳入：否则「开了 4 个币只显示 3 个」——
        # 没持仓、最近也没有信号的那个币永远算不出指标。
        if self.symbol_filter:
            syms.update(self.symbol_filter)
        # 也把最近有过信号的币纳入（未展示币的持仓风控仍需指标）
        for ev in list(self.events)[-20:]:
            s = ev.get("symbol")
            if s:
                syms.add(s)
        for sym in list(syms):
            try:
                cs = self.bars_fn(sym)
                if cs and len(cs) >= 30:
                    state = IND.market_state(cs)
                    self.indicators[sym] = state
                    features = self._build_features(sym, cs, state, "bullish")
                    self.live_features[sym] = {
                        "macd": features.get("macd"),
                        "squeeze": features.get("squeeze"),
                        "cvd": features.get("cvd"),
                        "vsaBull": features.get("vsaBull"),
                        "vsaBear": features.get("vsaBear"),
                        "obi": features.get("obi"),
                        "oi": features.get("oi"),
                        "funding": features.get("funding"),
                        "sentiment": features.get("sentiment"),
                        "htf": features.get("htf"),
                        "hmm": features.get("hmm"),
                        "liquidation": features.get("liquidation"),
                    }
                    if self.decision_cfg.scoring_enabled:
                        # ---- 评分口径：每根K线只算一次 ----
                        # cs 含未收盘的当前K线，盘中价一抖 RSI/Z/ADX 就跟着变，
                        # 同一根 3m 里评分会被重算 36 次（线上实测 54→77 来回跳，
                        # 开仓线 55，越线纯看运气）。平滑只能压低高频抖动，
                        # 压不住"这根K线本身还没走完"这件事。
                        # 所以评分改用**上一根已收盘K线**的指标：同一根K线内
                        # 分数完全不动，跨根才更新。页面上的指标条仍用盘中值
                        # （那是"现在"），但"结论"必须基于已完成的事实。
                        closed = [bar for bar in cs if getattr(bar, "closed", False)]
                        if len(closed) >= 30:
                            score_state = IND.market_state(closed)
                            score_features = self._build_features(sym, closed, score_state, "bullish")
                        else:
                            # 还没攒够收盘K线（刚启动）：只能用盘中值，但要标明
                            score_state, score_features = state, features
                        bull = score_signal("bullish", score_state, score_features,
                                            self.decision_cfg)
                        bear = score_signal("bearish", score_state, score_features,
                                            self.decision_cfg)
                        # 方向要**带滞后**，不能每 5 秒按分数大小硬翻：
                        # bull 50.1 / bear 50.0 就翻成做多，下一秒反过来——
                        # 用户按这个开仓，手续费就能把本金磨光。
                        # 规则：新方向必须比当前方向高出 HYSTERESIS_GAP 分才切换，
                        # 否则保持上一轮方向（信号不动，用户的操作也不会被晃）。
                        raw_dir = "bullish" if bull.score >= bear.score else "bearish"
                        prev = (self.live_evaluations.get(sym) or {}).get("direction")
                        if prev in ("bullish", "bearish") and prev != raw_dir:
                            cur = bull.score if prev == "bullish" else bear.score
                            alt = bear.score if prev == "bullish" else bull.score
                            if abs(alt - cur) < HYSTERESIS_GAP:
                                direction = prev      # 优势不明显，维持原方向
                            else:
                                direction = raw_dir
                        else:
                            direction = raw_dir
                        side_score = bull if direction == "bullish" else bear
                        # ---- 评分平滑 ----
                        # 指标用的是**未收盘**K线（盘中价在动），同一根 3m 里
                        # 评分会被重算 36 次。线上实测：TSLA 60 秒内从 54 到 77
                        # 来回跳，而开仓线是 55——越线完全看这一秒有没有插针。
                        # 这里对**分数和分离度**各自做 EMA：新值占 alpha，
                        # 剩下保留上一轮。方向另有一套滞后（HYSTERESIS_GAP），
                        # 两者叠加后不会出现"分数偶尔冒一下就直接开仓"。
                        raw_score = float(side_score.score)
                        raw_opposite = float(side_score.opposite_score)
                        raw_gap = float(side_score.gap)
                        alpha = float(getattr(self.decision_cfg, "score_smooth_alpha", 0.0) or 0.0)
                        prev_eval = self.live_evaluations.get(sym) or {}
                        smooth = {"alpha": round(alpha, 3), "rawScore": round(raw_score, 2)}
                        if (0.0 < alpha < 1.0
                                and prev_eval.get("direction") == direction
                                and isinstance(prev_eval.get("score"), (int, float))):
                            score_s = prev_eval["score"] + (raw_score - prev_eval["score"]) * alpha
                            gap_s = (prev_eval.get("gap", raw_gap)
                                     + (raw_gap - prev_eval.get("gap", raw_gap)) * alpha)
                            opposite_s = (prev_eval.get("oppositeScore", raw_opposite)
                                          + (raw_opposite
                                             - prev_eval.get("oppositeScore", raw_opposite)) * alpha)
                            smooth["smoothed"] = True
                        else:
                            # 方向刚变 / 第一轮 / 关闭平滑：直接用原值，
                            # 不能拿另一个方向的分数跟本方向做平均
                            score_s, gap_s, opposite_s = raw_score, raw_gap, raw_opposite
                            smooth["smoothed"] = False
                        score_s = round(score_s, 2)
                        gap_s = round(gap_s, 2)
                        opposite_s = round(opposite_s, 2)
                        # 档位/否决要按**平滑后**的分数重判，否则会出现
                        # "分数是平滑值、但 grade 还是原始值算的"这种自相矛盾
                        grade_s = side_score.grade
                        if not side_score.vetoes or len(side_score.vetoes) <= 0:
                            if score_s >= self.decision_cfg.strong_score and gap_s >= self.decision_cfg.strong_score_gap:
                                grade_s = "strong"
                            elif score_s >= self.decision_cfg.minimum_score and gap_s >= self.decision_cfg.minimum_score_gap:
                                grade_s = "medium"
                            elif score_s >= 40:
                                grade_s = "weak"
                            else:
                                grade_s = "none"
                        self.live_evaluations[sym] = {
                            "ts": _now_ms(),
                            "direction": direction,
                            # 本轮是否因为"分差不够"而维持了上一轮方向
                            "latched": direction != raw_dir,
                            "rawDirection": raw_dir,
                            "score": score_s,
                            "oppositeScore": opposite_s,
                            "gap": gap_s,
                            "grade": grade_s,
                            "confidence": round(min(1.0, max(0.0, score_s / 100.0)), 4),
                            "breakdown": side_score.breakdown,
                            "reasons": side_score.reasons,
                            "vetoes": side_score.vetoes,
                            "bull": bull.score,
                            "bear": bear.score,
                            # 平滑前的原始分，让人能看出"差多少"而不是被抹平
                            "smoothing": smooth,
                            # 评分基于哪根K线：同一根内不重算，"结论"才有意义
                            "basis": "closedBar" if (len(closed) >= 30) else "liveBar",
                            "basisBarTime": int(getattr((closed or cs)[-1], "open_time", 0) or 0),
                        }
                        history = self.live_score_history.setdefault(
                            sym, deque(maxlen=24))
                        history.append({"ts": _now_ms(), "score": score_s})
                        # 把这一轮的每条算式推进流水（内存环形缓冲，不落库）
                        self._push_calc_rows(sym, state, side_score, direction, history)
                        # 模拟仓：每轮推进一次（开仓 / 更新盈亏 / 判离场）
                        try:
                            ctx = self._paper_context(sym)
                            if ctx:
                                self.paper.tick(sym, ctx)
                        except Exception:
                            pass
                    if self.decision_cfg.drift_enabled and state.get("roc") is not None:
                        bucket = int(getattr(cs[-1], "open_time", 0) or 0)
                        if self._vol_last_bar.get(sym) != bucket:
                            self._vol_last_bar[sym] = bucket
                            if self._vol_drift.add(abs(float(state["roc"]))):
                                self._handle_drift("_market_", "volatility",
                                                   self._vol_drift)
            except Exception:
                continue
        self._refresh_drawdown()

    def _push_calc_rows(self, sym: str, state: dict, score, direction: str,
                        history: deque) -> None:
        """把一次评估拆成多条细粒度的计算行，推进实时流水。

        设计要点：
          · **按时间排，不按币分组**——前端要的是一条连续的计算流，
            而不是每个币一段摘要。每行自带 symbol，渲染时用小图标表示。
          · 每条都是真实算式（谁乘谁、谁减谁），不是给人看的摘要。
          · 只进内存 deque，不落库：这是运算轨迹，重算即可得到。
        """
        ts = _now_ms()
        b = score.breakdown or {}
        side = b.get("bear") if direction == "bearish" else b.get("bull")
        oppo = b.get("bull") if direction == "bearish" else b.get("bear")
        w = b.get("weights") or {}
        rows: list[dict] = []

        def add(kind: str, tag: str, expr: str, value, tone: str = "") -> None:
            rows.append({"ts": ts, "sym": sym, "kind": kind, "tag": tag,
                         "expr": expr, "value": value, "tone": tone})

        total = side.get("total") if side else None
        opposite = oppo.get("total") if oppo else None
        dir_label = "空" if direction == "bearish" else "多"
        tone = "down" if direction == "bearish" else "up"

        # 1) 原始输入：每个分项算出来的依据
        add("in", "rsi", "RSI", round(state.get("rsi") or 0, 1) if state.get("rsi") is not None else None)
        add("in", "adx", "ADX", round(state.get("adx") or 0, 1) if state.get("adx") is not None else None)
        if state.get("plusDI") is not None and state.get("minusDI") is not None:
            add("in", "di", "+DI−−DI",
                round(float(state["plusDI"]) - float(state["minusDI"]), 2))
        if state.get("atrPct") is not None:
            add("in", "atr", "ATR位", round(float(state["atrPct"]), 1))
        if state.get("roc") is not None:
            add("in", "roc", "ROC%", round(float(state["roc"]) * 100, 2))
        if state.get("bwPct") is not None:
            add("in", "bw", "带宽%", round(float(state["bwPct"]), 2))

        # 2) 每个分项 × 权重
        for key, label in (("trend", "趋势"), ("momentum", "动能"),
                           ("volume", "量能"), ("structure", "结构")):
            part = (side or {}).get(key)
            weight = w.get(key)
            if part is None or weight is None:
                continue
            # tag 用单字，窄列也放得下；中文名不重复写进算式
            add("w", SHORT_TAG.get(key, key), f"{part:.1f}×{weight:.2f}",
                round(float(part) * float(weight), 2))

        # 3) 合计 ÷ 权重和
        if w:
            add("sum", "wsum", "Σw", round(sum(float(x) for x in w.values()), 2))
        if total is not None:
            add("sum", "total", f"{dir_label}÷Σw", round(float(total), 2), tone)

        # 4) 分离度
        if total is not None and opposite is not None:
            add("gap", "gap", f"{total:.2f} − {opposite:.2f}",
                round(float(total) - float(opposite), 2))

        # 5) 评分轨迹的稳定性与估算胜率
        scores = [float(x.get("score")) for x in history if x.get("score") is not None]
        if len(scores) > 1:
            mean = sum(scores) / len(scores)
            sd = (sum((x - mean) ** 2 for x in scores) / len(scores)) ** 0.5
            add("sigma", "sd", f"σ(n={len(scores)})", round(sd, 2))
            stability = max(0.0, min(100.0, 100 - sd * 5))
            if total is not None:
                est = (float(total) / 100) * (stability / 100) * 100
                add("est", "est", f"{total:.0f}%×稳{stability:.0f}%", round(est), tone)

        # 6) 否决理由（有才记）
        for veto in score.vetoes or []:
            add("veto", "veto", str(veto)[:40], None, "warn")

        # 尾行：本轮结论，方便在长流里定位
        add("verdict", "verdict", f"{dir_label} {score.grade}",
            round(float(score.score), 2), tone)

        self.calc_stream.extend(rows)
        # 落库：页面刷新时用它填充最近 20 秒，不用从 0 开始；
        # Agent 也能查到"刚才在算什么"。库里只留 1 分钟。
        if self.calc_storage_fn:
            try:
                self.calc_storage_fn(rows)
            except Exception:
                pass        # 存不进去不能让评估中断

    def _refresh_drawdown(self) -> None:
        """回撤熔断状态：从权益采样算当前/最大回撤，超阈值则暂停开仓一段时间。"""
        if not self.equity_fn:
            return
        try:
            vals = self.equity_fn()
            if not vals or len(vals) < 2:
                return
            # 用 risk_metrics 的版本：除了 current/max/peak，还给出回撤持续
            # 多少个采样点——"回撤 1% 已经持续三小时"和"刚回撤 1%"是两件事。
            dd = RISK.drawdown(vals) or IND.drawdown(vals)
            if not dd:
                return
            self.dd_state["current"] = round(dd["current"], 3)
            self.dd_state["max"] = round(dd["max"], 3)
            self.dd_state["peak"] = round(dd["peak"], 2)
            if "currentBars" in dd:
                self.dd_state["currentBars"] = int(dd["currentBars"])
                self.dd_state["maxBars"] = int(dd["maxBars"])
                self.dd_state["samples"] = int(dd["samples"])
            now = _now_ms()
            # 触发熔断：当前回撤超过阈值，且之前没在暂停期
            limit = self.decision_cfg.drawdown_limit_pct
            pause_min = self.decision_cfg.drawdown_pause_min
            if dd["current"] / 100 <= -limit and now >= self.dd_state["paused_until"]:
                self.dd_state["paused_until"] = now + pause_min * 60_000
                self._event("skip",
                            f"账户回撤 {dd['current']:.2f}% 触及熔断线 "
                            f"{-limit * 100:.0f}%，暂停开仓 {pause_min} 分钟")
        except Exception:
            pass

    def _tail_profile(self) -> dict:
        """已平仓成交的尾部风险画像（CVaR / 波动率 / 最差笔）。

        数据源用 paper_trades + quant_trades 的收益率，**只看真实产生过的
        收益样本**——用估计值算 CVaR 是自欺欺人。样本不足 20 笔时各指标
        为 None，只报 samples，让人知道"还不足以下结论"。
        """
        returns: list[float] = []
        try:
            for record in self.paper.history_list():
                if not isinstance(record, dict):
                    continue
                pct = record.get("pnlPct")
                if pct is None:
                    continue
                returns.append(float(pct) / 100.0)
        except Exception:
            pass
        if not returns:
            for record in (self.trades or [])[-200:]:
                if not isinstance(record, dict):
                    continue
                pct = record.get("movePct")
                if pct is None:
                    continue
                returns.append(float(pct) / 100.0)
        profile = RISK.tail_profile(returns)
        profile["source"] = "paper" if returns else "none"
        return profile

    def _dd_paused(self) -> bool:
        return _now_ms() < self.dd_state.get("paused_until", 0)

    def _build_features(self, symbol: str, bars: list, state: dict,
                        direction: str) -> dict:
        now = _now_ms()
        features: dict = {}
        if bars:
            features["macd"] = macd(bars)
            features["squeeze"] = squeeze_state(bars)
            features["cvd"] = cvd_metrics(bars)
            features["vsaBull"] = vsa_metrics(bars, "bullish")
            features["vsaBear"] = vsa_metrics(bars, "bearish")
            features["close"] = float(bars[-1].close)
        depth = self.depth_fn(symbol) if self.depth_fn else None
        features["obi"] = order_book_imbalance(
            depth, levels=10, now_ms=now,
            max_age_ms=int(self.decision_cfg.stale_depth_sec * 1000),
        )
        market = self.market_data_fn(symbol) if self.market_data_fn else {}
        oi = market.get("oi") if isinstance(market, dict) else None
        if oi and bars and len(bars) >= 2:
            lookback = 10
            base = bars[-min(len(bars), lookback + 1)].close
            price_change = (bars[-1].close - base) / base if base else 0.0
            factors = open_interest_bias(float(oi.get("changePct") or 0), price_change)
            features["oi"] = {**factors, "changePct": oi.get("changePct"), "priceChange": price_change}
        funding = market.get("funding") if isinstance(market, dict) else None
        if funding:
            features["funding"] = funding_context(
                funding.get("rate"), funding.get("nextTime"),
                "BUY" if direction == "bullish" else "SELL", now,
            )
        if self.news_fn:
            try:
                features["sentiment"] = aggregate_news_sentiment(
                    self.news_fn() or [], symbol, now)
            except Exception:
                features["sentiment"] = {"score": 0.0, "confidence": 0.0}
        if self.watch_bars_fn:
            try:
                watched = self.watch_bars_fn(symbol)
                if watched:
                    _, h1, h4 = watched
                    if h4:
                        features["htf"] = ema_direction(
                            h4, self.decision_cfg.watch_ema_fast,
                            self.decision_cfg.watch_ema_slow)
                        if self.decision_cfg.hmm_shadow and len(h4) >= 60:
                            bucket = int(getattr(h4[-1], "open_time", 0) or 0)
                            key = (symbol, bucket)
                            fit = self._hmm_cache.get(key)
                            if fit is None:
                                fit = hmm_regime(h4, window=min(180, len(h4)),
                                                 iterations=18)
                                self._hmm_cache[key] = fit
                            features["hmm"] = fit
                            if (self.decision_cfg.hmm_use_in_score and
                                    fit.get("ok") and
                                    fit.get("confidence", 0) >=
                                    self.decision_cfg.hmm_min_confidence):
                                label = fit.get("label")
                                if label in ("bull", "bear", "range"):
                                    features["htf"] = {
                                        "direction": ("bullish" if label == "bull"
                                                      else "bearish" if label == "bear"
                                                      else "neutral"),
                                        "strength": fit["confidence"],
                                        "source": "hmm",
                                    }
            except Exception:
                pass
        if self.liquidation_fn:
            try:
                features["liquidation"] = self.liquidation_fn(symbol)
            except Exception:
                pass
        return features

    def _position_risk_inputs(self, st: dict) -> list[PositionRiskInput]:
        out = []
        for symbol, pos in st.get("positions", {}).items():
            stop = pos.get("sl")
            if not stop and pos.get("entry"):
                distance = abs(float(pos.get("slPct") or abs(SL_PCT) * 100)) / 100
                stop = float(pos["entry"]) * (1 - distance if pos.get("side") == "BUY"
                                              else 1 + distance)
            out.append(PositionRiskInput(
                symbol=symbol, side=str(pos.get("side") or ""),
                qty=float(pos.get("qty") or 0), entry=float(pos.get("entry") or 0),
                sl=float(stop) if stop else None,
            ))
        return out

    def _returns_for_symbols(self, symbols: set[str]) -> dict[str, list[float]]:
        out = {}
        if not self.bars_fn:
            return out
        for symbol in symbols:
            try:
                bars = self.bars_fn(symbol)
                if bars:
                    out[symbol] = return_series(
                        bars, self.decision_cfg.correlation_lookback)
            except Exception:
                continue
        return out

    def _place_protection(self, a, symbol: str, side: str, qty: float,
                          sl: float, tp: float | None) -> tuple[int | None, int | None]:
        close_side = "SELL" if side == "BUY" else "BUY"
        sl_order = place_protective_order(
            a["api_key"], a["api_secret"], symbol, close_side,
            "STOP_MARKET", qty, sl,
        )
        tp_order = None
        try:
            if tp:
                tp_order = place_protective_order(
                    a["api_key"], a["api_secret"], symbol, close_side,
                    "TAKE_PROFIT_MARKET", qty, tp,
                )
            else:
                return sl_order.get("orderId"), None
        except Exception:
            # TP is allowed to fall back to software monitoring; the stop is
            # the non-negotiable protection leg.
            self._event("error",
                        f"{symbol.upper()} 交易所止盈单挂载失败，已回落到软件止盈",
                        account=a.get("name"), symbol=symbol)
            return sl_order.get("orderId"), None
        return sl_order.get("orderId"), tp_order.get("orderId")

    def _cancel_protection(self, a, symbol: str) -> None:
        """平仓后撤掉保护单。

        **两处都要撤**：普通挂单（/fapi/v1/order）和算法条件单
        （/fapi/v1/algoOrder）。止损现在走 algoOrder 接口，只调
        cancel_all_open_orders 撤不掉它 —— 仓位平了止损单还挂着，
        就成了孤儿单，会一直累积（线上实测留下 2 张，其中一张
        名义 0.623 BTC，是这个仓的 500 倍）。

        不看 exchange_protection：那个开关只决定"引擎要不要自己挂保护单"，
        撤单永远要做。手动单的止损也是 algo 单，开关关掉时更不能留孤儿。
        """
        key, secret = a["api_key"], a["api_secret"]
        try:
            cancel_all_open_orders(key, secret, symbol)
        except Exception as e:
            self._event("error", f"撤销保护单失败({symbol}): {e}", symbol=symbol)
        # 算法单单独撤：接口失败不影响上面已撤的普通单
        try:
            cancel_algo_orders(key, secret, symbol)
        except Exception as e:
            self._event("error", f"撤销算法条件单失败({symbol}): {e}", symbol=symbol)

    def _place_entry_order(self, a, symbol: str, side: str, qty: float,
                           reference_price: float) -> dict | None:
        """Optional maker-first entry with cancel-and-replace timeout."""
        if not self.decision_cfg.maker_first:
            recent = list(self._recent_entry_slippage)
            if len(recent) == 3 and sum(recent) / 3 > 0.3:
                depth = self.depth_fn(symbol) if self.depth_fn else None
                now = _now_ms()
                if (depth and now - int(depth.get("ts") or 0) <=
                        self.decision_cfg.stale_depth_sec * 1000):
                    bids = depth.get("bids") or []
                    asks = depth.get("asks") or []
                    tick = self._tick_size(a["api_key"], a["api_secret"], symbol)
                    if bids and asks and tick > 0:
                        raw = float(asks[0][0] if side == "BUY" else bids[0][0])
                        price = round(round((raw + (tick if side == "BUY" else -tick)) /
                                            tick) * tick, 12)
                        try:
                            order = self._place_order(
                                a["api_key"], a["api_secret"], symbol, side, qty,
                                order_type="LIMIT", price=price,
                                time_in_force="IOC",
                            )
                            if float(order.get("executedQty") or 0) > 0:
                                order["iocFallback"] = True
                                return order
                            self._event(
                                "skip",
                                f"{symbol.upper()} 连续高滑点，IOC 未成交，放弃信号",
                                symbol=symbol,
                            )
                            return None
                        except Exception as e:
                            self._event("error",
                                        f"{symbol.upper()} IOC 下单失败: {e}",
                                        symbol=symbol)
                            return None
            return self._place_order(
                a["api_key"], a["api_secret"], symbol, side, qty)
        depth = self.depth_fn(symbol) if self.depth_fn else None
        now = _now_ms()
        if not depth or now - int(depth.get("ts") or 0) > self.decision_cfg.stale_depth_sec * 1000:
            self._event("system", f"{symbol.upper()} 盘口不可用，maker-first 回落市价",
                        symbol=symbol)
            return self._place_order(a["api_key"], a["api_secret"], symbol, side, qty)
        bids = depth.get("bids") or []
        asks = depth.get("asks") or []
        if not bids or not asks:
            return self._place_order(a["api_key"], a["api_secret"], symbol, side, qty)
        tick = self._tick_size(a["api_key"], a["api_secret"], symbol)
        if tick <= 0:
            return self._place_order(a["api_key"], a["api_secret"], symbol, side, qty)
        attempts = 1 + self.decision_cfg.maker_max_reposts
        remaining = qty
        fills = []
        for _ in range(attempts):
            current_depth = self.depth_fn(symbol) if self.depth_fn else depth
            book = current_depth or depth
            raw_price = (book.get("bids") or [[reference_price]])[0][0] if side == "BUY" else (
                (book.get("asks") or [[reference_price]])[0][0])
            price = round(round(float(raw_price) / tick) * tick, 12)
            try:
                order = self._place_order(
                    a["api_key"], a["api_secret"], symbol, side, remaining,
                    order_type="LIMIT", price=price, time_in_force="GTX",
                )
            except Exception as e:
                self._event("error", f"{symbol.upper()} maker 挂单失败: {e}", symbol=symbol)
                return None
            order_id = order.get("orderId")
            deadline = time.monotonic() + self.decision_cfg.maker_timeout_sec
            status = order
            while time.monotonic() < deadline:
                time.sleep(1.0)
                try:
                    status = query_order(
                        a["api_key"], a["api_secret"], symbol, int(order_id))
                except Exception:
                    continue
                if status.get("status") in ("FILLED", "CANCELED", "REJECTED", "EXPIRED"):
                    break
            executed = float(status.get("executedQty") or order.get("executedQty") or 0)
            if executed > 0:
                fills.append({
                    "qty": executed,
                    "avgPrice": float(status.get("avgPrice") or price),
                    "orderId": order_id,
                })
                remaining -= executed
                if remaining <= 1e-12:
                    break
            if status.get("status") not in ("FILLED", "CANCELED", "REJECTED", "EXPIRED"):
                try:
                    cancel_order(a["api_key"], a["api_secret"], symbol, int(order_id))
                except Exception:
                    pass
        if not fills:
            self._event("skip", f"{symbol.upper()} maker-first 超时未成交，放弃本次信号",
                        symbol=symbol)
            return None
        total_qty = sum(item["qty"] for item in fills)
        avg = sum(item["qty"] * item["avgPrice"] for item in fills) / total_qty
        return {
            "orderId": fills[-1]["orderId"],
            "avgPrice": avg,
            "executedQty": total_qty,
            "makerFirst": True,
            "partial": total_qty + 1e-12 < qty,
        }

    def _replace_exchange_stop(self, a, pos: dict, new_stop: float) -> None:
        if not self.decision_cfg.exchange_protection:
            return
        symbol = pos["symbol"]
        old_id = pos.get("slOrderId")
        close_side = "SELL" if pos["side"] == "BUY" else "BUY"
        try:
            if old_id:
                cancel_order(a["api_key"], a["api_secret"], symbol, int(old_id))
            order = place_protective_order(
                a["api_key"], a["api_secret"], symbol, close_side,
                "STOP_MARKET", float(pos["qty"]), float(new_stop),
            )
            pos["slOrderId"] = order.get("orderId")
            pos["protectionOk"] = bool(pos.get("slOrderId") or pos.get("tpOrderId"))
            self._event("protection",
                        f"{symbol.upper()} 吊灯止损移至 {new_stop:.8f}",
                        account=pos.get("account"), symbol=symbol,
                        slOrderId=pos.get("slOrderId"))
        except Exception as e:
            pos["protectionOk"] = False
            self._event("error",
                        f"{symbol.upper()} 保护单更新失败: {e}", symbol=symbol)

    # ---------- 信号入口（收线确认信号驱动） ----------

    @_serialized
    def on_signal(self, hit: dict) -> None:
        """确认信号 → 逐账户决策开仓。任何异常只记录并跳过。"""
        if not self.enabled:
            return
        try:
            symbol = hit.get("symbol")
            if not symbol:
                return
            # 行情非真实（演示/降级数据）→ 一律不下单：随机游走产生的形态没有交易意义
            if self.feed_is_live_fn is not None and not self.feed_is_live_fn():
                self._event("skip", "当前为演示行情（非真实数据），不下单",
                            account=self._primary_account(), symbol=symbol,
                            pattern=hit.get("pattern"), name_zh=hit.get("name_zh"))
                return
            # 不在当前观察范围内的币种直接忽略：不计入信号统计、不产生决策流水
            if self.symbol_filter is not None and symbol not in self.symbol_filter:
                return
            self.stats["signals"] += 1
            direction = hit.get("direction")
            price = (self.price_fn or (lambda s: None))(symbol)
            trace = self._trace_start(hit, price)
            trace["account"] = self._primary_account()
            self._trace_stage(
                trace, "intake", "信号接收", "done",
                f"{hit.get('name_zh') or hit.get('pattern') or '形态'} · "
                f"{'看涨' if direction == 'bullish' else '看跌' if direction == 'bearish' else '中性'}",
                price=price, pattern=hit.get("pattern"), direction=direction,
            )
            # 每条确认信号都先落一条「收到信号」：流水必须能看到"系统收到了什么信号"，
            # 否则被过滤/中性的信号只剩一条拦截原因，回看时无法判断到底收到过什么。
            dir_text = {"bullish": "看涨", "bearish": "看跌"}.get(direction, "中性")
            self._event("signal",
                        f"收到 {hit.get('name_zh') or hit.get('pattern')} {dir_text}信号"
                        + (f" @ {price}" if price else ""),
                        account=self._primary_account(), symbol=symbol,
                        pattern=hit.get("pattern"), name_zh=hit.get("name_zh"),
                        direction=direction, price=float(price) if price else None)
            if direction not in ("bullish", "bearish"):
                # 中性形态（十字星等）不指示方向，无法决定多空 → 明确记录，便于核对决策流水完整性
                self._event("skip", f"{hit.get('name_zh') or '中性形态'}没有方向，不下单",
                            account=self._primary_account(), symbol=symbol,
                            pattern=hit.get("pattern"), name_zh=hit.get("name_zh"))
                self._finish_trace(trace, "neutral", "无方向",
                                   "中性形态，等待下一根确认")
                return
            if not price or price <= 0:
                self._event("skip", "取不到最新价，这次信号跳过", account=self._primary_account(), symbol=symbol)
                self._finish_trace(trace, "error", "无报价", "取不到最新价")
                return
            bars = self.bars_fn(symbol) if self.bars_fn else []
            st = self.indicators.get(symbol) or {}
            if not st and len(bars) >= 30:
                st = IND.market_state(bars)
                self.indicators[symbol] = st
            features = self._build_features(symbol, bars, st, direction)
            trace["factors"] = {
                "regime": st.get("regime"),
                "volState": st.get("volState"),
                "rsi": st.get("rsi"),
                "z": st.get("z"),
                "adx": st.get("adx"),
                "roc": st.get("roc"),
                "atrPct": st.get("atrPct"),
                "squeeze": features.get("squeeze"),
                "macd": features.get("macd"),
                "cvd": features.get("cvd"),
                "obi": features.get("obi"),
                "oi": features.get("oi"),
                "funding": features.get("funding"),
                "sentiment": features.get("sentiment"),
                "htf": features.get("htf"),
                "hmm": features.get("hmm"),
                "liquidation": features.get("liquidation"),
            }
            self._trace_stage(
                trace, "features", "数据协作", "done",
                f"技术 / 量价 / 衍生品 / 信息面已汇总",
                factors=trace["factors"],
            )
            pattern_stats = self.pattern_stats_fn() if self.pattern_stats_fn else {}
            signal_score = None
            if self.decision_cfg.scoring_enabled:
                if not st:
                    why = "市场状态数据不足，无法计算共鸣分"
                    self._event("skip", f"信号评分拦截：{why}", account=self._primary_account(),
                                symbol=symbol, pattern=hit.get("pattern"))
                    self._finish_trace(trace, "blocked", "数据不足", why)
                    return
                signal_score = score_signal(
                    direction, st, features, self.decision_cfg,
                    pattern=hit.get("pattern"), pattern_stats=pattern_stats,
                )
                if not signal_score.allowed:
                    why = "；".join(signal_score.vetoes) or "信号分不足"
                    trace["score"] = {
                        "allowed": signal_score.allowed,
                        "score": signal_score.score,
                        "oppositeScore": signal_score.opposite_score,
                        "gap": signal_score.gap,
                        "grade": signal_score.grade,
                        "confidence": signal_score.confidence,
                        "breakdown": signal_score.breakdown,
                        "reasons": signal_score.reasons,
                        "vetoes": signal_score.vetoes,
                    }
                    self._trace_stage(
                        trace, "score", "共鸣评分", "blocked", why,
                        score=trace["score"])
                    self._append_gate({
                        "ts": _now_ms(), "symbol": symbol, "direction": direction,
                        "reason": why, "score": signal_score.score,
                        "gap": signal_score.gap, "grade": signal_score.grade,
                        "breakdown": signal_score.breakdown,
                    })
                    self._event("skip", f"信号评分拦截：{why}",
                                account=self._primary_account(), symbol=symbol,
                                pattern=hit.get("pattern"), score=signal_score.score,
                                gap=signal_score.gap)
                    self._finish_trace(trace, "blocked", "评分未通过", why,
                                       score=trace["score"])
                    return
                trace["score"] = {
                    "allowed": signal_score.allowed,
                    "score": signal_score.score,
                    "oppositeScore": signal_score.opposite_score,
                    "gap": signal_score.gap,
                    "grade": signal_score.grade,
                    "confidence": signal_score.confidence,
                    "breakdown": signal_score.breakdown,
                    "reasons": signal_score.reasons,
                    "vetoes": signal_score.vetoes,
                }
                self._trace_stage(
                    trace, "score", "共鸣评分", "done",
                    f"{signal_score.score:.0f} 分 · {signal_score.grade} · "
                    f"分离度 {signal_score.gap:.0f}",
                    score=trace["score"])
            elif USE_MARKET_GATE and st:
                ok, why = IND.signal_gate(st, direction)
                if not ok:
                    self._trace_stage(trace, "score", "环境过滤", "blocked", why)
                    self._append_gate({
                        "ts": _now_ms(), "symbol": symbol, "direction": direction,
                        "reason": why,
                    })
                    self._event("skip", f"市场状态过滤：{why}", account=self._primary_account(),
                                symbol=symbol, pattern=hit.get("pattern"))
                    self._finish_trace(trace, "blocked", "环境未通过", why)
                    return
            # 「收到信号」记录已在入口处统一落库，这里不再重复发。
            side = "BUY" if direction == "bullish" else "SELL"
            accounts = self._tradable_accounts()
            if not accounts:
                self._finish_trace(trace, "skipped", "无账户", "没有可交易账户")
                return
            for index, a in enumerate(accounts):
                name = a.get("name", "")
                try:
                    account_trace = copy.deepcopy(trace)
                    account_trace["id"] = f"{trace['id']}-{index}"
                    account_trace["account"] = name
                    self._decide_open(name, a, symbol, side, direction, float(price), hit,
                                      features=features, signal_score=signal_score,
                                      bars=bars, trace=account_trace)
                except Exception as e:
                    self.stats["errors"] += 1
                    self._event("error", f"决策异常({name}/{symbol}): {e}", symbol=symbol)
                    self._finish_trace(
                        account_trace, "error", "决策异常", str(e),
                        execution={"error": str(e)},
                    )
        except Exception as e:
            self.stats["errors"] += 1
            self._event("error", f"信号处理异常: {e}")

    def _decide_open(self, name, a, symbol, side, direction, price, hit,
                     features=None, signal_score=None, bars=None, trace=None):
        if trace is None:
            trace = self._trace_start(hit, price)
            trace["account"] = name
        st = self._st(name)
        now = _now_ms()
        if symbol in self._unsupported:
            self._finish_trace(trace, "blocked", "合约不可用", "该合约未开通或已跳过")
            return
        if now < self._rest_pause_until:
            left = int((self._rest_pause_until - now) / 1000)
            self._event("skip", f"交易所限频冷却中，还需 {left} 秒",
                        account=name, symbol=symbol, reasonCode="rest_pause")
            self._finish_trace(trace, "blocked", "执行冷却", "交易所 REST 限频")
            return
        if now < self._permission_pause_until:
            left = int((self._permission_pause_until - now) / 1000)
            self._event("skip", f"API 权限异常冷却中，还需 {left} 秒",
                        account=name, symbol=symbol, reasonCode="permission_pause")
            self._finish_trace(trace, "blocked", "权限异常", "API Key/权限需要检查")
            return
        if now < self._drift_shadow_until:
            left = int((self._drift_shadow_until - now) / 1000)
            self._event("skip", f"机制漂移影子模式中，停止真实开仓（还剩 {left} 秒）",
                        account=name, symbol=symbol, reasonCode="drift_shadow")
            self._finish_trace(trace, "blocked", "影子模式", "机制漂移，停止真实开仓")
            return
        drift_caution = now < self._drift_caution_until
        equity, avail = self._balance_snapshot(a)
        if equity <= 0:
            equity = avail
        watched = self.watch_bars_fn(symbol) if self.watch_bars_fn else None
        watch_plan = None
        if self.decision_cfg.watch_enabled:
            if not watched:
                self._event("skip", "4h/1h 盯盘数据未就绪，暂不开仓",
                            account=name, symbol=symbol)
                self._finish_trace(trace, "blocked", "多周期未就绪",
                                   "4h/1h 盯盘数据未就绪")
                return
            entry_bars, h1_bars, h4_bars = watched
            watch_plan = build_watch_plan(
                symbol, direction, entry_bars, h1_bars, h4_bars,
                equity=equity, available=avail, config=self.decision_cfg,
                entry_price=price,
            )
            if not watch_plan.allowed:
                trace["watch"] = {
                    "allowed": False, "reason": watch_plan.reason,
                    "code": watch_plan.code, "h4Direction": watch_plan.h4_direction,
                    "h1Rules": watch_plan.h1_rules, "metrics": watch_plan.metrics,
                }
                self._trace_stage(
                    trace, "watch", "多周期确认", "blocked", watch_plan.reason,
                    watch=trace["watch"])
                self._append_gate({
                    "ts": now, "symbol": symbol, "direction": direction,
                    "reason": f"多周期盯盘：{watch_plan.reason}",
                    "watch": watch_plan.metrics,
                })
                self._event("skip", f"多周期盯盘：{watch_plan.reason}",
                            account=name, symbol=symbol, pattern=hit.get("pattern"))
                self._finish_trace(trace, "blocked", "多周期未通过",
                                   watch_plan.reason, watch=trace["watch"])
                return
            trace["watch"] = {
                "allowed": True,
                "h4Direction": watch_plan.h4_direction,
                "h1Rules": watch_plan.h1_rules,
                "metrics": watch_plan.metrics,
                "entry": watch_plan.entry,
                "sl": watch_plan.sl,
                "tp": watch_plan.tp,
                "qty": watch_plan.qty,
                "score": watch_plan.score,
            }
            passed_h1 = sum(1 for row in watch_plan.h1_rules if row.get("passed"))
            self._trace_stage(
                trace, "watch", "多周期确认", "done",
                f"4h {watch_plan.h4_direction} · 1h {passed_h1} 条通过",
                watch=trace["watch"])
        if not self.decision_cfg.scoring_enabled and self.pattern_stats_fn:
            pst = (self.pattern_stats_fn() or {}).get(hit.get("pattern")) or {}
            wins = int(pst.get("wins") or 0)
            losses = int(pst.get("losses") or 0)
            if wins + losses >= self.min_pattern_samples:
                winrate = wins / (wins + losses)
                if winrate < self.min_pattern_winrate:
                    self._trace_stage(
                        trace, "pattern", "历史优势", "blocked",
                        f"滚动胜率 {winrate * 100:.0f}%")
                    self._event(
                        "skip",
                        f"该形态历史胜率仅 {round(winrate * 100)}%，低于门槛 "
                        f"{round(self.min_pattern_winrate * 100)}%，跳过",
                        account=name, symbol=symbol,
                    )
                    self._finish_trace(trace, "blocked", "形态优势不足",
                                       f"滚动胜率 {winrate * 100:.0f}%")
                    return
        st_ind = self.indicators.get(symbol) or {}
        if not st_ind and bars and len(bars) >= 30:
            st_ind = IND.market_state(bars)
            self.indicators[symbol] = st_ind
        if watch_plan:
            plan = {
                "sl": watch_plan.sl,
                "tp": watch_plan.tp,
                "slPct": abs(watch_plan.entry - watch_plan.sl) / watch_plan.entry * 100,
                "tpPct": abs(watch_plan.tp - watch_plan.entry) / watch_plan.entry * 100,
            }
        elif USE_ATR_STOPS:
            plan = IND.adaptive_sl_tp(
                price, st_ind.get("atr"), side,
                sl_mult=ATR_SL_MULT, tp_mult=ATR_TP_MULT,
                sl_min_pct=SL_MIN_PCT, tp_min_pct=TP_MIN_PCT,
                sl_max_pct=SL_MAX_PCT, tp_max_pct=TP_MAX_PCT)
        else:
            plan = IND.adaptive_sl_tp(
                price, None, side,
                sl_min_pct=abs(SL_PCT), tp_min_pct=TP_PCT,
                sl_max_pct=abs(SL_PCT), tp_max_pct=TP_PCT)
        positions = self._position_risk_inputs(st)
        symbols = {symbol, *(p.symbol for p in positions)}
        cascade = (features or {}).get("liquidation") or {}
        data_ok = True
        if self.data_health_fn:
            try:
                data_ok = bool(self.data_health_fn(symbol))
            except Exception:
                data_ok = False
        risk_input = RiskInput(
            account=name, symbol=symbol, side=side, entry=price, sl=plan["sl"],
            equity=equity, available=avail, positions=positions,
            returns_by_symbol=self._returns_for_symbols(symbols),
            step_size=self._step_size(a["api_key"], a["api_secret"], symbol),
            now_ms=now, data_ok=data_ok,
            is_demo=bool(self.feed_is_live_fn and not self.feed_is_live_fn()),
            drawdown_paused=self._dd_paused(),
            drawdown_pct=float(self.dd_state.get("current") or 0),
            signal_score=signal_score.score if signal_score else None,
            signal_grade=signal_score.grade if signal_score else None,
            confidence=signal_score.confidence if signal_score else None,
            atr_pct=st_ind.get("atrPct"),
            liquidation_cascade=bool(cascade.get("blocked") or cascade.get("active")),
            funding_blocked=bool((features or {}).get("funding", {}).get("blocked")),
            max_positions_override=(
                min(2, self.decision_cfg.max_positions) if drift_caution else None),
            cooldown_extra_sec=900 if drift_caution else 0,
        )
        decision = self.risk.evaluate(risk_input)
        trace["risk"] = {
            "allowed": decision.allowed, "code": decision.code,
            "reason": decision.reason, "qty": decision.qty,
            "notional": decision.notional, "sizeFactor": decision.size_factor,
            "kellyPct": decision.kelly_pct, "riskPct": decision.risk_pct,
            "heatPct": decision.heat_pct, "correlationMax": decision.correlation_max,
            "warnings": decision.warnings, "metrics": decision.metrics,
        }
        if not decision.allowed:
            self._trace_stage(trace, "risk", "统一风控", "blocked",
                              decision.reason, risk=trace["risk"])
            self._append_gate({
                "ts": now, "symbol": symbol, "direction": direction,
                "reason": decision.reason, "code": decision.code,
                "metrics": decision.metrics,
            })
            self._event("skip", f"统一风控：{decision.reason}",
                        account=name, symbol=symbol, pattern=hit.get("pattern"),
                        riskCode=decision.code)
            self._finish_trace(trace, "blocked", "风控未通过",
                               decision.reason, risk=trace["risk"])
            return
        self._trace_stage(
            trace, "risk", "统一风控", "done",
            f"数量 {decision.qty} · 热度 {decision.heat_pct:.2f}% · "
            f"Kelly {decision.kelly_pct * 100:.1f}%",
            risk=trace["risk"])
        qty = decision.qty
        self.risk.record_order_attempt(name, now)
        try:
            r = self._place_entry_order(a, symbol, side, qty, price)
        except Exception as e:
            self.stats["errors"] += 1
            if self._mark_unsupported(symbol, e):
                st["cooldown"][symbol] = now
                self._finish_trace(trace, "error", "下单失败", str(e),
                                   execution={"error": str(e)})
                return
            st["cooldown"][symbol] = now
            self._event("error", f"下单失败({name}/{symbol}): {e}", symbol=symbol)
            self._finish_trace(trace, "error", "下单失败", str(e),
                               execution={"error": str(e)})
            return
        if not r:
            st["cooldown"][symbol] = now
            self._finish_trace(trace, "skipped", "未成交",
                               "限价单超时，信号放弃", execution={"order": "unfilled"})
            return
        qty = float(r.get("executedQty") or qty)
        qty = self._quantize(qty, self._step_size(a["api_key"], a["api_secret"], symbol))
        if qty <= 0:
            self._finish_trace(trace, "skipped", "数量为零", "步长取整后数量为 0")
            return
        entry_price = float(r.get("avgPrice") or price)
        adverse_slippage = (entry_price - price) * (1 if side == "BUY" else -1)
        atr_at_entry = st_ind.get("atr")
        slippage_atr = adverse_slippage / atr_at_entry if atr_at_entry else 0.0
        self._recent_entry_slippage.append(abs(slippage_atr))
        if slippage_atr > 0.3:
            self._event("slippage",
                        f"{symbol.upper()} 开仓滑点 {slippage_atr:.2f}×ATR",
                        account=name, symbol=symbol,
                        expected=price, fill=entry_price,
                        slippageAtr=round(slippage_atr, 6))
        sl_order_id = tp_order_id = None
        if self.decision_cfg.exchange_protection:
            try:
                sl_order_id, tp_order_id = self._place_protection(
                    a, symbol, side, qty, plan["sl"], plan["tp"])
            except Exception as e:
                self.stats["errors"] += 1
                close_side = "SELL" if side == "BUY" else "BUY"
                try:
                    self._place_order(a["api_key"], a["api_secret"], symbol,
                                      close_side, qty, reduce_only=True)
                except Exception as close_error:
                    self._event("error",
                                f"保护单失败且紧急平仓失败({name}/{symbol}): {close_error}",
                                account=name, symbol=symbol)
                self._event("error",
                            f"保护单挂载失败，已拒绝裸仓({name}/{symbol}): {e}",
                            account=name, symbol=symbol)
                self._cancel_protection(a, symbol)
                self._finish_trace(trace, "error", "保护单失败",
                                   "保护单挂载失败，已拒绝裸仓",
                                   execution={"error": str(e)})
                return
        pos = {
            "symbol": symbol, "side": side, "qty": qty,
            "account": name,
            "margin": round(qty * entry_price, 8),
            "entry": entry_price, "entryTs": now,
            "orderId": r.get("orderId"), "pattern": hit.get("pattern"),
            "name_zh": hit.get("name_zh"), "direction": direction,
            "sl": round(plan["sl"], 8) if plan["sl"] else None,
            "tp": round(plan["tp"], 8) if plan["tp"] else None,
            "slPct": round(plan["slPct"], 3),
            "tpPct": round(plan["tpPct"], 3),
            "atrAtEntry": atr_at_entry,
            "regimeAtEntry": st_ind.get("regime"),
            "rsiAtEntry": st_ind.get("rsi"),
            "score": signal_score.score if signal_score else None,
            "scoreGrade": signal_score.grade if signal_score else None,
            "scoreBreakdown": signal_score.breakdown if signal_score else None,
            "watchPlan": watch_plan.metrics if watch_plan else None,
            "slOrderId": sl_order_id, "tpOrderId": tp_order_id,
            "protectionOk": bool(sl_order_id),
            "slippageAtr": round(slippage_atr, 6),
            "invalidation": self._invalidation_price(hit, side, atr_at_entry),
            "executionType": "maker" if r.get("makerFirst") else "market",
            "partialFill": bool(r.get("partial")),
        }
        st["positions"][symbol] = pos
        st["cooldown"][symbol] = now
        self.stats["decisions"] += 1
        self._save_positions()
        self._event(
            "open",
            f"{'做多' if side == 'BUY' else '做空'} {symbol.upper()} "
            f"数量 {qty} @ {entry_price}（风险 {decision.risk_pct:.2f}%，"
            f"热度 {decision.heat_pct:.2f}%）",
            account=name, symbol=symbol, side=side, qty=qty, entry=entry_price,
            orderId=pos["orderId"], margin=pos["margin"], pattern=hit.get("pattern"),
            name_zh=hit.get("name_zh"), direction=direction,
            slPct=pos["slPct"], tpPct=pos["tpPct"], atr=atr_at_entry,
            score=pos["score"], scoreGrade=pos["scoreGrade"],
            heatPct=decision.heat_pct, kellyPct=decision.kelly_pct * 100,
            warnings=decision.warnings, slippageAtr=pos["slippageAtr"],
            slOrderId=sl_order_id, tpOrderId=tp_order_id,
        )
        self._broadcast_positions()
        trace["execution"] = {
            "type": pos["executionType"],
            "entry": entry_price,
            "qty": qty,
            "notional": pos["margin"],
            "sl": pos["sl"],
            "tp": pos["tp"],
            "slOrderId": sl_order_id,
            "tpOrderId": tp_order_id,
            "slippageAtr": pos["slippageAtr"],
        }
        self._trace_stage(
            trace, "execution", "执行与保护", "done",
            f"{pos['executionType']} · {qty} @ {entry_price}",
            execution=trace["execution"])
        self._finish_trace(
            trace, "opened", "已开仓",
            f"{'做多' if side == 'BUY' else '做空'} {symbol.upper()} · "
            f"{qty} @ {entry_price}",
            execution=trace["execution"],
        )

    @staticmethod
    def _invalidation_price(hit: dict, side: str, atr: float | None) -> float | None:
        geometry = hit.get("geometry") or {}
        neck = geometry.get("neck")
        if neck is None or not atr:
            return None
        if side == "BUY":
            return float(neck) - 0.1 * atr
        return float(neck) + 0.1 * atr

    def _balance_snapshot(self, a) -> tuple[float, float]:
        name = str(a.get("name") or a.get("id") or "")
        now = _now_ms()
        cached = self._balance_cache.get(name)
        if cached and now - cached[2] < 10_000:
            return cached[0], cached[1]
        try:
            balances = signed_request(a["api_key"], a["api_secret"], "/fapi/v2/balance")
            for b in balances:
                if b.get("asset") == "USDT":
                    wallet = float(b.get("balance", 0) or 0)
                    available = float(b.get("availableBalance", 0) or 0)
                    self._balance_cache[name] = (wallet, available, now)
                    return wallet, available
        except Exception as e:
            self._event("error", f"可用余额获取失败({a.get('name')}): {e}")
        return 0.0, 0.0

    def _available(self, a) -> float:
        return self._balance_snapshot(a)[1]

    # ---------- 持仓监控（止损/止盈） ----------

    def _mark_price(self, a, symbol: str) -> float | None:
        """取标记价：优先用本地行情（监控池内币种，零延迟）；
        池外持仓（历史遗留/手工仓）回落到交易所 markPrice，保证风控始终有效。"""
        p = (self.price_fn or (lambda s: None))(symbol)
        if p and p > 0:
            return p
        cache = self._mark_cache.get(symbol)
        now = _now_ms()
        if cache and now - cache[1] < 15000:
            return cache[0]
        try:
            rows = signed_request(a["api_key"], a["api_secret"], "/fapi/v2/positionRisk",
                                  {"symbol": symbol.upper()})
            for row in rows:
                if row.get("symbol", "").lower() == symbol:
                    mp = float(row.get("markPrice") or 0)
                    if mp > 0:
                        self._mark_cache[symbol] = (mp, now)
                        return mp
        except Exception as e:
            self._event("error", f"标记价获取失败({symbol}): {e}", symbol=symbol)
        return None

    @_serialized
    def manage(self) -> None:
        """每 5 秒调用：刷新技术指标 → 盯每仓盈亏 → 触发止损/止盈即平仓。"""
        if not self.enabled:
            return
        self.refresh_indicators()
        # 监控范围收窄（用户把币删出展示列表）→ 清掉范围外的模拟仓，
        # 否则 paper_positions 会留下永不 tick 的残行（P2-5）。
        # 启动初期 symbol_filter=None 不动账本（范围还没确定）；异常不能打断 manage。
        try:
            self._settle_paper_out_of_scope()
        except Exception:
            pass
        now = _now_ms()
        # 对账：本地记录与交易所有出入时以交易所为准（每小时一次即可）
        if now - getattr(self, "_last_reconcile", 0) > 3600_000:
            self._last_reconcile = now
            try:
                self.reconcile_positions()
            except Exception as e:
                self._event("error", f"持仓对账失败: {e}")
        # 只管理"参与交易"的账户（按 API Key 去重），避免别名账户重复平仓
        tradable = {a.get("name") for a in self._tradable_accounts()}
        for name in list(self.state.keys()):
            if name not in tradable:
                continue
            a = self._acc(name)
            if not a:
                continue
            st = self._st(name)
            self._manual_reload()
            peak_dirty = False
            for symbol in list(st["positions"].keys()):
                if (name, symbol) in self._manual:
                    continue        # 手动仓位：止盈止损交给交易所挂单，引擎不干预
                pos = st["positions"][symbol]
                price = self._mark_price(a, symbol)
                if not price or price <= 0:
                    continue
                direction_up = pos["side"] == "BUY"
                move = (price - pos["entry"]) / pos["entry"] * (1 if direction_up else -1)
                hold_min = (now - pos.get("entryTs", now)) / 60000
                # 记录峰值浮盈（移动止盈用）：首次进入即初始化，之后只升不降
                prev_peak = pos.get("peak")
                peak = move if prev_peak is None else prev_peak
                if move > peak:
                    peak = move
                if prev_peak is None or peak > prev_peak + 0.0001:
                    pos["peak"] = peak      # 首次/明显创新高才标记，循环结束统一写盘
                    peak_dirty = True
                bars = self.bars_fn(symbol) if self.bars_fn else []
                atr_now = (self.indicators.get(symbol) or {}).get("atr")
                if bars and atr_now:
                    last_closed = next(
                        (bar for bar in reversed(bars) if getattr(bar, "closed", False)), None)
                    if last_closed is not None:
                        if direction_up:
                            pos["peakPrice"] = max(
                                float(pos.get("peakPrice") or pos["entry"]),
                                float(last_closed.high))
                        else:
                            pos["peakPrice"] = min(
                                float(pos.get("peakPrice") or pos["entry"]),
                                float(last_closed.low))
                        invalidation = pos.get("invalidation")
                        if invalidation:
                            invalidated = (
                                float(last_closed.close) < float(invalidation)
                                if direction_up else
                                float(last_closed.close) > float(invalidation)
                            )
                            if invalidated:
                                self._close(
                                    name, a, symbol, price,
                                    f"形态失效(收盘{last_closed.close}穿越{invalidation})")
                                continue
                        armed = peak >= self.decision_cfg.trail_arm_atr * atr_now / pos["entry"]
                        if armed:
                            if direction_up:
                                chandelier = float(pos["peakPrice"]) - (
                                    self.decision_cfg.trail_atr_mult * atr_now)
                                new_stop = max(float(pos.get("sl") or 0), chandelier)
                            else:
                                chandelier = float(pos["peakPrice"]) + (
                                    self.decision_cfg.trail_atr_mult * atr_now)
                                old_stop = float(pos.get("sl") or 1e30)
                                new_stop = min(old_stop, chandelier)
                            min_step = max(atr_now * 0.05, pos["entry"] * 0.0002)
                            improvement = (
                                new_stop - float(pos.get("sl") or 0)
                                if direction_up else
                                float(pos.get("sl") or 1e30) - new_stop
                            )
                            if improvement > min_step:
                                pos["sl"] = round(new_stop, 8)
                                pos["slPct"] = round(
                                    abs(new_stop - pos["entry"]) / pos["entry"] * 100, 3)
                                pos["trailAtr"] = round(new_stop, 8)
                                peak_dirty = True
                                self._replace_exchange_stop(a, pos, new_stop)
                # 优先用开仓时按 ATR 算出的止损/止盈价；缺失时退回全局百分比
                hit_sl = hit_tp = False
                if pos.get("sl") or pos.get("tp"):
                    if pos.get("sl"):
                        hit_sl = (price <= pos["sl"]) if direction_up else (price >= pos["sl"])
                    if pos.get("tp"):
                        hit_tp = (price >= pos["tp"]) if direction_up else (price <= pos["tp"])
                else:
                    hit_sl = move <= SL_PCT
                    hit_tp = move >= TP_PCT
                if hit_sl or hit_tp:
                    reason = "止盈" if hit_tp else "止损"
                    detail = (f"目标{pos.get('tpPct', 0):.2f}%" if hit_tp
                              else f"止损{pos.get('slPct', 0):.2f}%")
                    self._close(name, a, symbol, price,
                                f"{reason}({move * 100:.2f}%, {detail})")
                elif not atr_now and peak >= max(TRAIL_ARM, (pos.get("slPct") or 0) / 100 * 0.5) and move <= peak - max(TRAIL_GAP, (pos.get("slPct") or 0) / 100 * 0.15):
                    # 移动止盈：浮盈曾达门槛，回撤到阈值 → 锁定剩余利润
                    self._close(name, a, symbol, price,
                                f"移动止盈(峰值{peak * 100:.2f}%→{move * 100:.2f}%)")
                elif hold_min >= MAX_HOLD_MIN:
                    # 超时出场：避免窄幅震荡时仓位长期占满槽位（策略停摆兜底）
                    self._close(name, a, symbol, price, f"超时({hold_min:.0f}min,{move * 100:.2f}%)")
            if peak_dirty:
                self._save_positions()      # 峰值更新落盘，重启后移动止盈仍可用
        # manage 末尾：风控/漂移状态节流落库（成交、漂移事件另有 force 落库）。
        # _refresh_drawdown() 由本轮开头的 refresh_indicators() 调用（无 bars_fn 时会提前返回），
        # 所以放在整个 manage 的真正末尾，保证本轮风控状态一定被尝试保存。
        self.save_persistent_state()

    def _live_amt(self, api_key: str, api_secret: str, symbol: str) -> float | None:
        """查交易所该币种真实持仓量（平仓必须用它，本地记录可能因部分成交而不准）。"""
        try:
            rows = signed_request(api_key, api_secret, "/fapi/v2/positionRisk", {"symbol": symbol.upper()})
            for p in rows:
                if p.get("symbol", "").lower() == symbol:
                    return float(p.get("positionAmt", 0))
        except Exception as e:
            self._event("error", f"查询真实持仓失败({symbol}): {e}", symbol=symbol)
        return None

    def _close(self, name, a, symbol, price, reason: str) -> None:
        st = self._st(name)
        pos = st["positions"].get(symbol)
        if not pos:
            return
        # 防死循环：同一持仓连续平仓失败 3 次后本轮放弃，等审计周期重置后再试
        fails = pos.get("closeFails", 0)
        if fails >= 3:
            return
        # 以交易所真实持仓量为准（本地数量可能是部分成交后的旧值）
        live_amt = self._live_amt(a["api_key"], a["api_secret"], symbol)
        if live_amt is not None:
            if abs(live_amt) < 1e-9:
                # 交易所已无仓位 → 直接核销，不再下单
                self._cancel_protection(a, symbol)
                st["positions"].pop(symbol)
                self._save_positions()
                self._event("close", f"{reason}：交易所已没有这个仓位，清理本地记录", account=name, symbol=symbol)
                self._broadcast_positions()
                return
            close_qty = abs(live_amt)
            if abs(close_qty - abs(pos["qty"])) > 1e-9:
                pos["qty"] = close_qty        # 修正为真实量再平
        else:
            close_qty = abs(pos["qty"])
        close_side = "SELL" if pos["side"] == "BUY" else "BUY"
        self._cancel_protection(a, symbol)
        step = self._step_size(a["api_key"], a["api_secret"], symbol)
        close_qty = self._quantize(close_qty, step)
        if close_qty <= 0:
            st["positions"].pop(symbol)
            self._save_positions()
            return
        try:
            r = self._place_order(a["api_key"], a["api_secret"], symbol, close_side, close_qty, reduce_only=True)
        except Exception as e:
            if "-2022" in str(e):
                amt = self._live_amt(a["api_key"], a["api_secret"], symbol)
                if amt is not None and abs(amt) < 1e-9:
                    self._cancel_protection(a, symbol)
                    st["positions"].pop(symbol, None)
                    self._save_positions()
                    self._event("close", f"{reason}：平仓竞态，仓位已由交易所了结",
                                account=name, symbol=symbol, apiCode="-2022")
                    self._broadcast_positions()
                    return
            pos["closeFails"] = fails + 1
            self.stats["errors"] += 1
            self._event("error", f"平仓失败({name}/{symbol}) 第{fails + 1}次: {e}",
                        symbol=symbol)
            if pos["closeFails"] >= 3:
                self._event("error", f"{name}/{symbol} 平仓连续失败3次，暂停重试等待审计核销", symbol=symbol)
            return
        fill_price = float(r.get("avgPrice") or 0) or price
        direction_sign = 1 if pos["side"] == "BUY" else -1
        move = (fill_price - pos["entry"]) / pos["entry"] * direction_sign
        pnl = round((fill_price - pos["entry"]) * close_qty * direction_sign, 2)
        close_slippage = (price - fill_price) * direction_sign
        atr_at_entry = float(pos.get("atrAtEntry") or 0)
        slippage_atr = close_slippage / atr_at_entry if atr_at_entry else 0.0
        trade = {
            "ts": _now_ms(), "account": name, "symbol": symbol, "side": pos["side"],
            "qty": close_qty, "entry": pos["entry"], "exit": fill_price, "pnl": pnl,
            "movePct": round(move * 100, 2), "reason": reason,
            "entryTs": pos["entryTs"], "holdMin": round((now_ms() - pos["entryTs"]) / 60000, 1),
            "pattern": pos.get("pattern"), "orderId": pos.get("orderId"), "closeId": r.get("orderId"),
            "slippageAtr": round(slippage_atr, 6),
            "entrySlippageAtr": float(pos.get("slippageAtr") or 0),
            "atrAtEntry": atr_at_entry,
        }
        st["positions"].pop(symbol)
        self._save_positions()
        self._record_trade(trade)
        self.stats["closes"] += 1
        if pnl > 0:
            self.stats["wins"] += 1
        elif pnl < 0:
            self.stats["losses"] += 1
        self._event("close", f"{reason}平仓 {symbol.upper()}，盈亏 {pnl}",
                    account=name, symbol=symbol, pnl=pnl, side=pos["side"],
                    qty=close_qty, entry=pos["entry"], exit=fill_price,
                    slippageAtr=round(slippage_atr, 6))
        if slippage_atr > 0.3:
            self._event("slippage",
                        f"{symbol.upper()} 平仓滑点 {slippage_atr:.2f}×ATR",
                        account=name, symbol=symbol,
                        expected=price, fill=fill_price,
                        slippageAtr=round(slippage_atr, 6))
        self._broadcast_positions()

    # ---------- 每 5 分钟自审计（对账 + 兜底） ----------

    def _audit_protection(self, a, pos: dict) -> bool:
        if not self.decision_cfg.exchange_protection:
            return True
        symbol = pos["symbol"]
        stop = pos.get("sl")
        target = pos.get("tp")
        if not stop:
            return True
        try:
            orders = open_orders(a["api_key"], a["api_secret"], symbol)
            stop_orders = [o for o in orders if o.get("type") == "STOP_MARKET"]
            expected = float(stop)
            atr = float(pos.get("atrAtEntry") or 0)
            tolerance = max(float(pos["entry"]) * 0.0005, atr * 0.1)
            valid = any(
                abs(float(o.get("stopPrice") or 0) - expected) <= tolerance
                for o in stop_orders
            )
            if valid:
                pos["protectionOk"] = True
                return True
            self._event("audit", f"{symbol.upper()} 止损保护单缺失或价格漂移，准备补挂",
                        account=pos.get("account"), symbol=symbol)
            try:
                cancel_all_open_orders(a["api_key"], a["api_secret"], symbol)
            except Exception:
                pass
            sl_id, tp_id = self._place_protection(
                a, symbol, pos["side"], float(pos["qty"]), expected, target)
            pos["slOrderId"] = sl_id
            pos["tpOrderId"] = tp_id
            pos["protectionOk"] = bool(sl_id)
            self._event("audit", f"{symbol.upper()} 保护单已补挂",
                        account=pos.get("account"), symbol=symbol,
                        slOrderId=sl_id, tpOrderId=tp_id)
            return True
        except Exception as e:
            pos["protectionOk"] = False
            self._event("error",
                        f"{symbol.upper()} 保护单补挂失败，准备市价保命平仓: {e}",
                        account=pos.get("account"), symbol=symbol)
            return False

    @_serialized
    def audit(self) -> None:
        """用交易所 positionRisk 对账：漏记补记、已平核销、卡单标记。"""
        if not self.enabled or _now_ms() - self.last_audit_ms < AUDIT_SEC * 1000:
            return
        self.last_audit_ms = _now_ms()
        try:
            for a in self._tradable_accounts():
                name = a.get("name", "")
                try:
                    rows = signed_request(a["api_key"], a["api_secret"], "/fapi/v2/positionRisk")
                except Exception as e:
                    self._event("error", f"审计拉取失败({name}): {e}")
                    continue
                st = self._st(name)
                live = {}
                for p in rows:
                    amt = float(p.get("positionAmt", 0))
                    if abs(amt) < 1e-9:
                        continue
                    sym = p.get("symbol", "").lower()
                    live[sym] = {"amt": amt, "price": float(p.get("markPrice", 0) or 0)}
                changed = False
                # 交易所有、本地漏记 → 补记（手动仓位除外：纳入跟踪会被止盈止损规则误平）
                for sym, li in live.items():
                    if sym not in st["positions"]:
                        if (name, sym) in self._manual:
                            continue
                        pos = {"symbol": sym, "side": "BUY" if li["amt"] > 0 else "SELL",
                               "qty": abs(li["amt"]), "entry": li["price"] or 0, "entryTs": _now_ms(),
                               "orderId": None, "pattern": None, "direction": None,
                               "recovered": True}
                        st["positions"][sym] = pos
                        changed = True
                        self._event("audit", f"发现 {sym.upper()} 有仓位但本地没记录，已补记", account=name, symbol=sym)
                # 本地有、交易所已无 → 核销（含平仓熔断后卡住的记录；手动仓位被止损/强平也在此清理）
                for sym in list(st["positions"].keys()):
                    if sym not in live:
                        if (name, sym) in self._manual:
                            self._manual.discard((name, sym))
                            self._save_manual()
                        pos = st["positions"].pop(sym)
                        changed = True
                        was_stuck = pos.get("closeFails", 0) >= 3
                        self._event("audit",
                                    f"{sym.upper()} 已经平掉了，清理本地记录{'（之前平仓连续失败过）' if was_stuck else ''}",
                                    account=name, symbol=sym)
                # 本地与交易所数量不一致 → 以交易所为准修正数量
                for sym, li in live.items():
                    pos = st["positions"].get(sym)
                    if pos and abs(abs(pos["qty"]) - abs(li["amt"])) > 1e-9:
                        pos["qty"] = abs(li["amt"])
                        changed = True
                        self._event("audit", f"{sym.upper()} 的持仓数量和交易所对不上，已按交易所更新为 {abs(li['amt'])}", account=name, symbol=sym)
                # 止损单是最后防线：缺失/漂移时补挂，补挂失败则保命平仓
                for sym, pos in list(st["positions"].items()):
                    if (name, sym) in self._manual:
                        continue
                    if not pos.get("sl"):
                        mark = live.get(sym, {}).get("price") or pos.get("entry", 0)
                        st_ind = self.indicators.get(sym) or {}
                        if mark:
                            recovered_plan = IND.adaptive_sl_tp(
                                float(mark), st_ind.get("atr"), pos["side"],
                                sl_mult=ATR_SL_MULT, tp_mult=ATR_TP_MULT,
                                sl_min_pct=SL_MIN_PCT, tp_min_pct=TP_MIN_PCT,
                                sl_max_pct=SL_MAX_PCT, tp_max_pct=TP_MAX_PCT)
                            pos.update({
                                "sl": recovered_plan["sl"],
                                "tp": recovered_plan["tp"],
                                "slPct": recovered_plan["slPct"],
                                "tpPct": recovered_plan["tpPct"],
                            })
                            changed = True
                    if not self._audit_protection(a, pos):
                        price = live.get(sym, {}).get("price") or pos.get("entry", 0)
                        if price:
                            self._close(name, a, sym, float(price),
                                        "保护单缺失且补挂失败")
                # 豁免登记的仓位在交易所已不存在（未被跟踪故未走上面核销）→ 清理登记，
                # 否则残留豁免会让量化引擎后续在同账户同币种开的仓绕过止盈止损
                for (n, sym) in list(self._manual):
                    if n == name and sym not in live:
                        self._manual.discard((n, sym))
                        self._save_manual()
                        self._event("audit", f"{sym.upper()} 的手动仓位已平，解除引擎豁免", account=name, symbol=sym)
                if changed:
                    self._save_positions()
                # 审计周期重置平仓失败计数：熔断只是暂停，不是永久放弃（下次重试）
                for pos in st["positions"].values():
                    if pos.get("closeFails", 0) >= 3:
                        pos["closeFails"] = 0
                        self._event("audit", f"重新尝试平掉 {pos.get('symbol').upper()}",
                                    account=name, symbol=pos.get("symbol"))
            self._event("audit", "已核对仓位：本地记录和交易所一致", positions={n: list(self._st(n)["positions"].keys()) for n in self.state})
        except Exception as e:
            self.stats["errors"] += 1
            self._event("error", f"审计异常: {e}")

    # ---------- 广播快照 ----------

    def set_enabled(self, v: bool) -> None:
        self.enabled = bool(v)
        self._event("system", f"量化引擎{'开启' if v else '关闭'}", enabled=v)
        self._broadcast_positions()

    def _broadcast_positions(self):
        if self.broadcast_fn:
            try:
                self.broadcast_fn({"type": "quant", "snapshot": self.snapshot()})
            except Exception:
                pass

    @staticmethod
    def _public_state(st: dict) -> dict:
        """给前端的市场状态（保留可 JSON 化的数值字段）。"""
        keys = ("price", "bars", "atr", "atrPct", "realizedVol", "boll", "bwPct",
                "rsi", "z", "adx", "plusDI", "minusDI", "roc", "regime",
                "volState", "squeeze")
        return {k: st.get(k) for k in keys if k in st}

    def _calc_stream(self) -> list[dict]:
        """计算流水快照：内存为主，空了就从库补最近 20 秒。

        冷启动时内存 deque 是空的，直接返回空数组会让前端"从 0 开始跑"，
        看起来像刚开机。库里存了最近 1 分钟，取 20 秒足够铺满首屏。
        """
        mem = list(self.calc_stream)
        if mem:
            return mem[-600:]
        if self.calc_read_fn:
            try:
                return self.calc_read_fn(20)
            except Exception:
                return []
        return []

    def _paper_context(self, sym: str) -> dict:
        """给模拟仓一轮所需的上下文。数据不全时返回空 dict（不编）。"""
        state = self.indicators.get(sym) or {}
        evaluation = self.live_evaluations.get(sym) or {}
        if not state.get("price") or not evaluation.get("direction"):
            return {}
        advice = {}
        try:
            max_lev = int(self.max_leverage_fn(sym) or 0) if self.max_leverage_fn else 0
            advice = build_advice(sym, state, evaluation, max_lev or None)
        except Exception:
            advice = {}
        vetoes = evaluation.get("vetoes") or []
        if vetoes:
            reason = str(vetoes[0])[:40]
        elif evaluation.get("grade"):
            reason = f"{evaluation.get('grade')} 信号"
        else:
            reason = str(advice.get("reason") or "")[:40]
        # 最近K线：只给"从库恢复的仓位在停机期间到期"时补算离场价用，
        # 普通 tick 不读它，成本可忽略。
        bars = []
        try:
            bars = self.bars_fn(sym) if self.bars_fn else []
        except Exception:
            bars = []
        return {
            "price": state.get("price"),
            "atr": state.get("atr"),
            "score": evaluation.get("score"),
            "gap": evaluation.get("gap"),
            "grade": evaluation.get("grade"),
            "direction": evaluation.get("direction"),
            "reason": reason,
            "regime": state.get("regime"),
            "pattern": advice.get("pattern") or "",
            "patternStats": self.pattern_stats_fn() if self.pattern_stats_fn else {},
            "maxLeverage": advice.get("maxLeverage"),
            "bars": bars,
        }

    def _settle_paper_out_of_scope(self) -> list[str]:
        """清理已不在监控范围（symbol_filter）内的模拟仓，返回处理的币种。

        交给 PaperBook.close_missing：有 mark 的按 mark 平仓并记
        reason="removed" 的正常流水，没有可用价格的只删未平仓行。
        symbol_filter 为空时不动账本；异常只记事件，不能打断 5 秒一轮的 manage。
        """
        keep = self.symbol_filter
        if not keep:
            return []
        try:
            return self.paper.close_missing(set(keep))
        except Exception as e:
            self._event("error", f"模拟仓监控范围清理失败: {e}")
            return []

    def _paper_by_symbol(self) -> dict:
        """按币种的累计已平仓战绩：{symbol: {closed,wins,losses,winrate,netPnl}}。

        和卡片上的"估算胜率"是两回事：
          · 估算胜率 = 这一仓按 SL/TP 距离 + 信号分算的概率（前瞻）
          · 累计胜率 = 这个币历史上平掉的单子赢了多少（后验）
        两个都给前端，用户才能看出"信号说 65%，实际只赢 40%"这种偏差。

        查询失败或没接数据库时退回内存窗口口径，不影响页面渲染。
        """
        try:
            summary = self.paper.summary()
        except Exception:
            return {}
        rows = summary.get("bySymbol") or []
        out = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            sym = str(row.get("symbol") or "")
            if not sym:
                continue
            out[sym] = {
                "closed": int(row.get("closed") or 0),
                "wins": int(row.get("wins") or 0),
                "losses": int(row.get("losses") or 0),
                "winrate": row.get("winrate"),
                "netPnl": row.get("netPnl"),
                "source": summary.get("source") or "db",
            }
        return out

    def paper_refresh(self, symbol: str) -> dict | None:
        """手动刷新某个币的模拟仓：平掉当前、立刻按现状重开。

        前端每个盒子上有一个小按钮，点一下就是"按当前数据再开一次仓"。
        """
        ctx = self._paper_context(symbol)
        if not ctx:
            return None
        try:
            return self.paper.force(symbol, ctx)
        except Exception as e:
            self._event("error", f"模拟仓刷新失败({symbol}): {e}", symbol=symbol)
            return None

    # ---------- Agent 手动下单 ----------

    # 不设人工上限：人能下的单它也能下。
    # 唯一的约束来自交易所本身（可用保证金、合约杠杆上限、最小名义价值），
    # 这些不是我们加的规则，是真实存在的边界——人下单同样受它们约束。
    # 想控制风险请直接说金额/杠杆，或调低账户里的可用余额。

    def _normalize_symbol(self, symbol) -> str:
        """把模型给的各种写法归一成内部键（btcusdt）。

        模型会写 "BTC" / "btc" / "BTC-USDT" / "BTCUSDT" / "币安BTC"…
        内部键、.indicators、.price_fn 全部是 "btcusdt" 这种小写无分隔形式，
        不归一会直接取不到价（线上实测："取不到 BTC 最新价"）。
        认不出来就返回空串——宁可拒绝，也不要拿一个猜的币种去下单。
        """
        raw = str(symbol or "").strip().lower()
        if not raw:
            return ""
        # 去掉常见分隔符与中文前缀
        cleaned = re.sub(r"[\s\-_/]|币安|okx|欧易|合约|永续", "", raw)
        if not cleaned:
            return ""
        known = set(self.indicators.keys()) | set(self._step_sizes) | {
            s.lower() for s in (self.symbol_filter or ())}
        if cleaned in known:
            return cleaned
        # 只有币名没有计价货币：补 usdt
        if cleaned in ("btc", "eth", "bnb", "sol", "xrp", "doge", "ada", "tsla",
                       "xau", "xag", "avax", "link", "ltc", "dot", "trx", "matic"):
            return cleaned + "usdt"
        # 已经是 usdt 结尾或者带 usdc
        if cleaned.endswith(("usdt", "usdc")):
            return cleaned
        # 在监控池里找前缀匹配（btc → btcusdt），只认唯一命中
        hits = [k for k in known if k.startswith(cleaned)]
        if len(hits) == 1:
            return hits[0]
        return cleaned if cleaned.isalnum() and len(cleaned) <= 16 else ""

    def _manual_price(self, acct: dict, sym: str) -> tuple[float | None, str]:
        """下单用价：本地行情 → 交易所 markPrice → 交易所最新成交价。

        三层兜底，因为三种情况都要覆盖：
          · 监控池内的币：本地聚合器零延迟
          · 池外但有持仓：markPrice
          · 池外且没持仓、markPrice 也不给（比如刚上架的合约）：拉 24h ticker
        返回 (价格, 来源)，来源写进 plan 让用户知道这个价是怎么来的。
        """
        try:
            p = (self.price_fn or (lambda s: None))(sym)
            if p and float(p) > 0:
                return float(p), "local"
        except Exception:
            pass
        try:
            p = self._mark_price(acct, sym)
            if p and float(p) > 0:
                return float(p), "markPrice"
        except Exception:
            pass
        # 最后一层：24h ticker 的最新价（池外币种也能取到）
        try:
            rows = signed_request(acct.get("api_key", ""), acct.get("api_secret", ""),
                                  "/fapi/v1/ticker/24hr", {"symbol": sym.upper()})
            row = rows[0] if isinstance(rows, list) and rows else rows
            if isinstance(row, dict):
                for key in ("lastPrice", "price", "markPrice"):
                    v = row.get(key)
                    if v and float(v) > 0:
                        return float(v), "ticker"
        except Exception:
            pass
        return None, ""

    def manual_order_plan(self, symbol: str, side: str, notional: float | None = None,
                          leverage: int | None = None) -> dict:
        """算一张手动单的全部参数，**不下单**——供 Agent 先给用户看。

        为什么单独一步：真实资金操作必须先让用户看到
        「币种 / 方向 / 数量 / 杠杆 / 保证金 / 止损止盈」再确认。
        模型把"做个空"理解反、或者数量算错，直接下单就是真金白银的损失。

        返回 ready=false 时带 reason，Agent 应原样转述给用户，不要自己编。
        """
        # 币种归一：模型经常只写 "BTC"、"btc"、"BTC-USDT"，而内部键统一是
        # "btcusdt"。不归一就会取不到价（线上实测："取不到 BTC 最新价"）。
        sym = self._normalize_symbol(symbol)
        if not sym:
            return {"ready": False, "reason": "缺少币种"}
        direction = str(side or "").strip().lower()
        if direction in ("long", "buy", "做多", "多"):
            order_side, close_side, label = "BUY", "SELL", "做多"
        elif direction in ("short", "sell", "做空", "空"):
            order_side, close_side, label = "SELL", "BUY", "做空"
        else:
            return {"ready": False, "reason": "方向只能是做多或做空"}

        accounts = self._tradable_accounts()
        if not accounts:
            return {"ready": False, "reason": "没有可交易的账户（未配置 API Key）"}
        acct = accounts[0]
        key, secret = acct.get("api_key", ""), acct.get("api_secret", "")

        price, price_src = self._manual_price(acct, sym)
        if not price or price <= 0:
            return {"ready": False,
                    "reason": f"取不到 {sym.upper()} 的最新价（本地行情没有这个币，"
                              f"交易所接口也没返回），无法算单"}

        # 杠杆：只受交易所对该合约的上限约束（这是真实规则，人下单也一样）。
        # 用户要 150x 而交易所只支持 125x → 按 125x 算并说明，不额外加自己的顶。
        try:
            max_lev = int(self.max_leverage_fn(sym) or 0) if self.max_leverage_fn else 0
        except Exception:
            max_lev = 0
        want_lev = int(leverage or max_lev or 20)
        lev = max(1, want_lev)
        note = ""
        if max_lev and want_lev > max_lev:
            lev = max_lev
            note = (f"这个合约交易所最高 {max_lev}x，已按 {lev}x 算"
                    f"（你要求 {want_lev}x）")

        # 保证金：唯一约束是账户里真有这么多钱。
        # 不再按百分比打折——人要满仓就能满仓。
        try:
            available = float(self._available(acct) or 0)
        except Exception:
            available = 0.0
        if available <= 0:
            return {"ready": False, "reason": "账户可用保证金为 0，无法下单"}
        # 满杠杆下的名义上限：可用保证金 × 杠杆
        cap = round(available * lev, 2)
        want = float(notional or 0)
        if want <= 0:
            # 没指定金额：按可用保证金的 10% 折算，给个不至于一开口就满仓的默认
            want = round(min(cap, available * lev * 0.1), 2)
        if want > cap:
            # 超出可用保证金能支撑的规模：这是账户余额的硬约束，不是我们设的顶。
            # 把真实数字一起返回，模型才能给用户"最多能下多少"的可用建议。
            return {"ready": False,
                    "reason": (f"金额 {want:.0f} USDT 超出账户能支撑的 {cap:.0f} USDT"
                               f"（可用保证金 {available:.2f} × {lev}x）"),
                    "limits": {
                        "maxNotional": cap,
                        "available": round(available, 2),
                        "leverage": lev,
                        "maxLeverage": max_lev or None,
                    },
                    "suggest": (f"按 {lev}x 最多可下 {cap:.0f} USDT 名义"
                                f"（约 {available:.0f} USDT 保证金，即当前全部可用）")}
        if want < 5:
            return {"ready": False, "reason": "金额太小，交易所最小名义价值约 5 USDT"}

        try:
            step = self._step_size(key, secret, sym)
        except Exception:
            step = 0.001
        qty = self._quantize(want / price, step)
        if qty <= 0:
            return {"ready": False, "reason": "按当前价格和最小步长算出的数量为 0"}

        # 止损止盈：沿用 K 线胶囊那套 ATR 参数，保证和页面建议一致
        state = self.indicators.get(sym) or {}
        from decision.advice import _stop_plan
        plan = _stop_plan(float(price), state.get("atr"), direction == "long")
        if not plan:
            return {"ready": False, "reason": "算不出止损止盈"}

        return {
            "ready": True,
            "account": acct.get("name", ""),
            "symbol": sym,
            "priceSource": price_src,
            "orderSide": order_side,
            "closeSide": close_side,
            "direction": "long" if order_side == "BUY" else "short",
            "label": label,
            "price": round(float(price), 6),
            "qty": qty,
            "notional": round(qty * float(price), 2),
            "leverage": lev,
            "margin": round(qty * float(price) / lev, 2),
            "available": round(available, 2),
            "sl": plan["sl"], "tp": plan["tp"],
            "slPct": plan["slPct"], "tpPct": plan["tpPct"],
            "rr": plan["rr"],
            "note": note,
        }

    def manual_order_execute(self, plan: dict) -> dict:
        """按 manual_order_plan 的结果真正下单，并挂上交易所侧止损止盈。

        **只接受 plan 对象**，不接受"币种+方向"重新算一遍：
        用户确认的是那张具体的单（数量/杠杆/止损价），执行时必须一模一样，
        重新计算可能因为价格变了导致下出来的和看到的不一样。
        """
        if not isinstance(plan, dict) or not plan.get("ready"):
            return {"ok": False, "error": "下单计划无效"}
        accounts = self._tradable_accounts()
        if not accounts:
            return {"ok": False, "error": "没有可交易的账户"}
        acct = accounts[0]
        key, secret = acct.get("api_key", ""), acct.get("api_secret", "")
        sym = str(plan.get("symbol") or "")
        side = str(plan.get("orderSide") or "")
        qty = float(plan.get("qty") or 0)
        lev = int(plan.get("leverage") or 1)
        if not sym or side not in ("BUY", "SELL") or qty <= 0:
            return {"ok": False, "error": "下单参数不完整"}

        # 杠杆先设好，否则实际名义价值和计划不一致
        try:
            signed_request(key, secret, "/fapi/v1/leverage",
                           {"symbol": sym.upper(), "leverage": lev}, method="POST")
        except Exception as e:
            return {"ok": False, "error": f"设置杠杆失败: {e}"}

        try:
            order = self._place_order(key, secret, sym, side, qty, order_type="MARKET")
        except Exception as e:
            self._event("error", f"手动下单失败({sym}): {e}", symbol=sym)
            return {"ok": False, "error": f"交易所拒绝: {e}"}

        fill = self._num(order.get("avgPrice")) or float(plan.get("price") or 0)
        sl_order_id = tp_order_id = None
        protect_errors = []
        # 止损和止盈**分别**捕获：一个失败不能连累另一个。
        # 止损优先——仓位已经开了，没有止损就是在裸奔。
        try:
            sl_order = place_protective_order(
                key, secret, sym, plan["closeSide"], "STOP_MARKET", qty,
                float(plan["sl"]))
            sl_order_id = sl_order.get("algoId") or sl_order.get("orderId")
        except Exception as e:
            protect_errors.append(f"止损: {str(e)[:120]}")
        try:
            tp_order = place_protective_order(
                key, secret, sym, plan["closeSide"], "TAKE_PROFIT_MARKET", qty,
                float(plan["tp"]))
            tp_order_id = tp_order.get("algoId") or tp_order.get("orderId")
        except Exception as e:
            protect_errors.append(f"止盈: {str(e)[:120]}")
        protect_error = "；".join(protect_errors)

        self.stats["orders"] += 1
        self._event(
            "open",
            f"手动{plan['label']} {sym.upper()} 数量 {qty} @ {fill}"
            + (f"（止损单挂失败: {protect_error[:60]}）" if protect_error else ""),
            account=acct.get("name", ""), symbol=sym, side=side, qty=qty,
            entry=fill, orderId=order.get("orderId"), margin=plan.get("margin"),
            direction=plan.get("direction"), slPct=plan.get("slPct"),
            tpPct=plan.get("tpPct"), manual=True)
        self._broadcast_positions()
        return {
            "ok": True, "symbol": sym, "side": side, "qty": qty, "fill": fill,
            "orderId": order.get("orderId"),
            "slOrderId": sl_order_id, "tpOrderId": tp_order_id,
            "protectError": protect_error,
            # 止损没挂上 = 仓位裸奔，必须让用户知道，不能只在 tooltip 里躺着
            "unprotected": bool(protect_error),
            "warning": (f"仓位已成交，但保护单没挂上：{protect_error}。"
                        f"这个仓现在是裸的，建议手动补止损或立刻平掉。"
                        if protect_error else ""),
        }

    @staticmethod
    def _num(v):
        try:
            f = float(v)
            return f if math.isfinite(f) else None
        except (TypeError, ValueError):
            return None

    def _advice_map(self) -> dict:
        """把评估结果 + 真实杠杆上限压成前端可直接展示的一句话建议。
        永远给「现在该做多还是做空」的分析结论；有没有持仓不影响，
        持仓状态由账户卡和仓位历史展示，不在这里重复。
        """
        out = {}
        for sym, state in self.indicators.items():
            try:
                max_lev = int(self.max_leverage_fn(sym) or 0) if self.max_leverage_fn else 0
            except Exception:
                max_lev = 0
            try:
                out[sym] = build_advice(sym, state, self.live_evaluations.get(sym),
                                        max_lev or None)
            except Exception:
                continue
        return out

    def snapshot(self) -> dict:
        positions = []
        for name in self.state:
            st = self._st(name)
            for sym, pos in st["positions"].items():
                price = (self.price_fn or (lambda s: None))(sym) or pos.get("entry", 0)
                upnl = round((price - pos["entry"]) * pos["qty"] * (1 if pos["side"] == "BUY" else -1), 2)
                positions.append({**{k: pos.get(k) for k in ("symbol", "side", "qty", "entry", "entryTs", "margin", "pattern", "name_zh", "direction")},
                                  "account": name, "mark": price, "upnl": upnl,
                                  "movePct": round((price - pos["entry"]) / pos["entry"] * 100 * (1 if pos["side"] == "BUY" else -1), 2) if pos["entry"] else 0})
        return {
            "enabled": self.enabled, "ts": _now_ms(),
            "stats": dict(self.stats),
            "events": list(self.events)[-30:],
            "positions": positions,
            "trades": self.trades[-20:],
            "accounts": list(self.state.keys()),
            "unsupported": sorted(self._unsupported),
            # ---- 策略分析（第三行可视化用） ----
            "analysis": {
                # 每个币种的完整指标与状态
                "symbols": {sym: self._public_state(st) for sym, st in self.indicators.items()},
                # 计算流水：每轮每个币的多条算式，按时间排列、不分组。
                # 前端直接渲染成一条连续的滚动流，不要按币归类。
                # 计算流水：内存里有就用内存的（最全）；刚启动内存是空的时候
                # 从库里补最近 20 秒——否则页面一刷新就是从 0 开始跑。
                "calcStream": self._calc_stream(),
                "liveFeatures": dict(self.live_features),
                "liveEvaluations": dict(self.live_evaluations),
                "liveScoreHistory": {
                    sym: list(history) for sym, history in self.live_score_history.items()
                },
                # 账户回撤与熔断
                "drawdown": {
                    "current": self.dd_state.get("current", 0),
                    "max": self.dd_state.get("max", 0),
                    "peak": self.dd_state.get("peak", 0),
                    # 回撤已经持续多少个采样点（"刚回撤"vs"回撤三小时"是两件事）
                    "currentBars": self.dd_state.get("currentBars", 0),
                    "maxBars": self.dd_state.get("maxBars", 0),
                    "samples": self.dd_state.get("samples", 0),
                    "paused": self._dd_paused(),
                    "pausedUntil": self.dd_state.get("paused_until", 0),
                    "limit": -self.decision_cfg.drawdown_limit_pct * 100,
                },
                "drift": {
                    "cautionUntil": self._drift_caution_until,
                    "shadowUntil": self._drift_shadow_until,
                    "caution": _now_ms() < self._drift_caution_until,
                    "shadow": _now_ms() < self._drift_shadow_until,
                },
                # 过滤器拦截记录（最近 N 条）
                "gateLog": list(self.gate_log)[-20:],
                "decisionTraces": list(self.decision_traces)[-16:],
                "latestTraces": dict(self.latest_traces),
                # 尾部风险画像：CVaR / 已实现波动率 / 最差笔。
                # 之前看板上只有一个"回撤"，看不出"一次尾部亏损能吃多少"。
                "risk": self._tail_profile(),
                # 当前策略参数（前端展示"系统在用哪些规则"）
                "params": {
                    "calcIntervalSec": 5,
                    "minScore": self.decision_cfg.minimum_score,
                    "minScoreGap": self.decision_cfg.minimum_score_gap,
                    "atrSlMult": ATR_SL_MULT, "atrTpMult": ATR_TP_MULT,
                    "slMinPct": SL_MIN_PCT * 100, "slMaxPct": SL_MAX_PCT * 100,
                    "tpMinPct": TP_MIN_PCT * 100, "tpMaxPct": TP_MAX_PCT * 100,
                    "ddLimitPct": -self.decision_cfg.drawdown_limit_pct * 100,
                    "ddPauseMin": self.decision_cfg.drawdown_pause_min,
                    "maxPositions": self.decision_cfg.max_positions,
                    "maxSameDir": self.decision_cfg.max_same_direction,
                    "cooldownSec": self.decision_cfg.cooldown_base_sec,
                    "cooldownMaxSec": self.decision_cfg.cooldown_max_sec,
                    "maxHoldMin": MAX_HOLD_MIN,
                    "trailArmAtr": self.decision_cfg.trail_arm_atr,
                    "trailAtrMult": self.decision_cfg.trail_atr_mult,
                    "marginPct": self.decision_cfg.margin_pct_default * 100,
                    "riskPerTradePct": self.decision_cfg.risk_per_trade_pct * 100,
                    "portfolioHeatMaxPct": self.decision_cfg.portfolio_heat_max_pct * 100,
                    "dailyLossLimitPct": self.decision_cfg.daily_loss_limit_pct * 100,
                    "dailyProfitLockPct": self.decision_cfg.daily_profit_lock_pct * 100,
                    "rsiOverbought": IND.RSI_OVERBOUGHT, "rsiOversold": IND.RSI_OVERSOLD,
                    "zExtreme": IND.Z_EXTREME, "adxTrend": IND.ADX_TREND,
                    "adxRange": IND.ADX_RANGE, "squeezePct": IND.BW_SQUEEZE_PCT,
                },
                # 每币一句话建议（K线图右上角胶囊）：方向 / 杠杆 / 时长 / 止损止盈
                "advice": self._advice_map(),
                # 模拟仓：每个监控币一个 15 分钟的纸上交易，附最近 10 根K线
                "paper": self.paper.snapshot(
                    list(self.symbol_filter or self.indicators.keys()),
                    lambda s: self.bars_fn(s) if self.bars_fn else []),
                "paperStats": dict(self.paper.stats),
                "paperHistory": self.paper.history_list()[-30:],
                # 按币种的累计战绩（closed/wins/losses/winrate/netPnl）。
                # 前端模拟仓卡片的第二个徽标要显示"这个币历史上赢了多少"，
                # 与卡片上那个"当前仓的估算胜率"是两个口径，必须分开给。
                "paperBySymbol": self._paper_by_symbol(),
                # 当前持仓的开仓依据（供"为什么开了这个仓"回看）
                "positionPlans": [
                    {k: p.get(k) for k in ("symbol", "side", "entry", "sl", "tp", "slPct",
                                           "tpPct", "atrAtEntry", "regimeAtEntry",
                                           "rsiAtEntry", "pattern", "name_zh")}
                    for name in self.state
                    for p in self._st(name)["positions"].values()
                ],
            },
        }


def fmt_qty(q) -> str:
    """持仓/下单数量的可读格式。

    直接用 str(qty) 或两位小数都不行：BTC 仓位 0.0045 会显示成 0.0045
    （太长）或 0.00（信息全丢）。按量级选有效位数，并去掉多余的尾零。
    """
    try:
        v = float(q)
    except (TypeError, ValueError):
        return "--"
    a = abs(v)
    if a >= 1000:
        out = f"{a:,.2f}"
    elif a >= 1:
        out = f"{a:.4f}"
    elif a >= 0.01:
        out = f"{a:.4f}"
    else:
        out = f"{a:.6f}"
    # 去尾零（保留至少一位小数）
    if "." in out:
        out = out.rstrip("0").rstrip(".")
    return ("-" if v < 0 else "") + out


def now_ms() -> int:
    return _now_ms()
