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
import json
import math
import os
import threading
import time
from collections import deque

from accounts import (
    signed_request, open_orders, cancel_order, query_order, cancel_all_open_orders,
    place_protective_order,
)
from analysis import indicators as IND
from decision.config import DecisionConfig
from decision.drift import ADWIN
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


def _now_ms() -> int:
    return int(time.time() * 1000)


class QuantEngine:
    def __init__(self, accts_box: dict, config_path: str, price_fn=None,
                 min_pattern_winrate: float = 0.35, min_pattern_samples: int = 5,
                 decision_config: DecisionConfig | None = None):
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
        self.indicators: dict[str, dict] = {}   # symbol -> 市场状态（每轮刷新）
        self.gate_log: deque = deque(maxlen=GATE_MAX_LOG)  # 最近的过滤器拦截记录
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
        self._drift_detectors: dict[str, dict[str, ADWIN]] = {}
        self._drift_last_alert: dict[tuple[str, str], int] = {}
        self._drift_last_global_alert = 0
        self._drift_caution_until = 0
        self._drift_shadow_until = 0
        self._vol_drift = ADWIN(self.decision_cfg.drift_delta)
        self._vol_last_bar: dict[str, int] = {}
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
        try:
            with open(self._trades_path, "w", encoding="utf-8") as f:
                json.dump(self.trades[-500:], f, ensure_ascii=False, indent=1)
        except Exception:
            pass
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
        try:
            data = {name: st["positions"] for name, st in self.state.items() if st.get("positions")}
            with open(self._pos_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
        except Exception:
            pass

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
        try:
            with open(self._manual_path, "w", encoding="utf-8") as f:
                json.dump(sorted(f"{a}|{s}" for a, s in self._manual), f, ensure_ascii=False)
        except Exception:
            pass

    def _save_unsupported(self):
        try:
            with open(self._unsupported_path, "w", encoding="utf-8") as f:
                json.dump(sorted(self._unsupported), f, ensure_ascii=False)
        except Exception:
            pass

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

    def _refresh_drawdown(self) -> None:
        """回撤熔断状态：从权益采样算当前/最大回撤，超阈值则暂停开仓一段时间。"""
        if not self.equity_fn:
            return
        try:
            vals = self.equity_fn()
            if not vals or len(vals) < 2:
                return
            dd = IND.drawdown(vals)
            if not dd:
                return
            self.dd_state["current"] = round(dd["current"], 3)
            self.dd_state["max"] = round(dd["max"], 3)
            self.dd_state["peak"] = round(dd["peak"], 2)
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
        if not self.decision_cfg.exchange_protection:
            return
        try:
            cancel_all_open_orders(a["api_key"], a["api_secret"], symbol)
        except Exception as e:
            self._event("error", f"撤销保护单失败({symbol}): {e}", symbol=symbol)

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
                return
            if not price or price <= 0:
                self._event("skip", "取不到最新价，这次信号跳过", account=self._primary_account(), symbol=symbol)
                return
            bars = self.bars_fn(symbol) if self.bars_fn else []
            st = self.indicators.get(symbol) or {}
            if not st and len(bars) >= 30:
                st = IND.market_state(bars)
                self.indicators[symbol] = st
            features = self._build_features(symbol, bars, st, direction)
            pattern_stats = self.pattern_stats_fn() if self.pattern_stats_fn else {}
            signal_score = None
            if self.decision_cfg.scoring_enabled:
                if not st:
                    why = "市场状态数据不足，无法计算共鸣分"
                    self._event("skip", f"信号评分拦截：{why}", account=self._primary_account(),
                                symbol=symbol, pattern=hit.get("pattern"))
                    return
                signal_score = score_signal(
                    direction, st, features, self.decision_cfg,
                    pattern=hit.get("pattern"), pattern_stats=pattern_stats,
                )
                if not signal_score.allowed:
                    why = "；".join(signal_score.vetoes) or "信号分不足"
                    self.gate_log.append({
                        "ts": _now_ms(), "symbol": symbol, "direction": direction,
                        "reason": why, "score": signal_score.score,
                        "gap": signal_score.gap, "grade": signal_score.grade,
                        "breakdown": signal_score.breakdown,
                    })
                    self._event("skip", f"信号评分拦截：{why}",
                                account=self._primary_account(), symbol=symbol,
                                pattern=hit.get("pattern"), score=signal_score.score,
                                gap=signal_score.gap)
                    return
            elif USE_MARKET_GATE and st:
                ok, why = IND.signal_gate(st, direction)
                if not ok:
                    self.gate_log.append({
                        "ts": _now_ms(), "symbol": symbol, "direction": direction,
                        "reason": why,
                    })
                    self._event("skip", f"市场状态过滤：{why}", account=self._primary_account(),
                                symbol=symbol, pattern=hit.get("pattern"))
                    return
            # 「收到信号」记录已在入口处统一落库，这里不再重复发。
            side = "BUY" if direction == "bullish" else "SELL"
            for a in self._tradable_accounts():
                name = a.get("name", "")
                try:
                    self._decide_open(name, a, symbol, side, direction, float(price), hit,
                                      features=features, signal_score=signal_score,
                                      bars=bars)
                except Exception as e:
                    self.stats["errors"] += 1
                    self._event("error", f"决策异常({name}/{symbol}): {e}", symbol=symbol)
        except Exception as e:
            self.stats["errors"] += 1
            self._event("error", f"信号处理异常: {e}")

    def _decide_open(self, name, a, symbol, side, direction, price, hit,
                     features=None, signal_score=None, bars=None):
        st = self._st(name)
        now = _now_ms()
        if symbol in self._unsupported:
            return
        if now < self._rest_pause_until:
            left = int((self._rest_pause_until - now) / 1000)
            self._event("skip", f"交易所限频冷却中，还需 {left} 秒",
                        account=name, symbol=symbol, reasonCode="rest_pause")
            return
        if now < self._permission_pause_until:
            left = int((self._permission_pause_until - now) / 1000)
            self._event("skip", f"API 权限异常冷却中，还需 {left} 秒",
                        account=name, symbol=symbol, reasonCode="permission_pause")
            return
        if now < self._drift_shadow_until:
            left = int((self._drift_shadow_until - now) / 1000)
            self._event("skip", f"机制漂移影子模式中，停止真实开仓（还剩 {left} 秒）",
                        account=name, symbol=symbol, reasonCode="drift_shadow")
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
                return
            entry_bars, h1_bars, h4_bars = watched
            watch_plan = build_watch_plan(
                symbol, direction, entry_bars, h1_bars, h4_bars,
                equity=equity, available=avail, config=self.decision_cfg,
                entry_price=price,
            )
            if not watch_plan.allowed:
                self.gate_log.append({
                    "ts": now, "symbol": symbol, "direction": direction,
                    "reason": f"多周期盯盘：{watch_plan.reason}",
                    "watch": watch_plan.metrics,
                })
                self._event("skip", f"多周期盯盘：{watch_plan.reason}",
                            account=name, symbol=symbol, pattern=hit.get("pattern"))
                return
        if not self.decision_cfg.scoring_enabled and self.pattern_stats_fn:
            pst = (self.pattern_stats_fn() or {}).get(hit.get("pattern")) or {}
            wins = int(pst.get("wins") or 0)
            losses = int(pst.get("losses") or 0)
            if wins + losses >= self.min_pattern_samples:
                winrate = wins / (wins + losses)
                if winrate < self.min_pattern_winrate:
                    self._event(
                        "skip",
                        f"该形态历史胜率仅 {round(winrate * 100)}%，低于门槛 "
                        f"{round(self.min_pattern_winrate * 100)}%，跳过",
                        account=name, symbol=symbol,
                    )
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
        if not decision.allowed:
            self.gate_log.append({
                "ts": now, "symbol": symbol, "direction": direction,
                "reason": decision.reason, "code": decision.code,
                "metrics": decision.metrics,
            })
            self._event("skip", f"统一风控：{decision.reason}",
                        account=name, symbol=symbol, pattern=hit.get("pattern"),
                        riskCode=decision.code)
            return
        qty = decision.qty
        self.risk.record_order_attempt(name, now)
        try:
            r = self._place_entry_order(a, symbol, side, qty, price)
        except Exception as e:
            self.stats["errors"] += 1
            if self._mark_unsupported(symbol, e):
                st["cooldown"][symbol] = now
                return
            st["cooldown"][symbol] = now
            self._event("error", f"下单失败({name}/{symbol}): {e}", symbol=symbol)
            return
        if not r:
            st["cooldown"][symbol] = now
            return
        qty = float(r.get("executedQty") or qty)
        qty = self._quantize(qty, self._step_size(a["api_key"], a["api_secret"], symbol))
        if qty <= 0:
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
        now = _now_ms()
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
                # 账户回撤与熔断
                "drawdown": {
                    "current": self.dd_state.get("current", 0),
                    "max": self.dd_state.get("max", 0),
                    "peak": self.dd_state.get("peak", 0),
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
                # 当前策略参数（前端展示"系统在用哪些规则"）
                "params": {
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
