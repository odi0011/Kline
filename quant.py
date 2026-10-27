"""量化交易引擎：以形态引擎的收线确认信号为入场依据，在监控池币种上自动开仓/平仓。

设计要点（全部为兜底服务稳定性）：
  · 每个账户独立状态：持仓 / 冷却 / 当日决策计数
  · 风控：每账户最多 3 个持仓、单仓保证金 = 可用余额 5%（下限 10 USDT）、
    同币种冷却 15 分钟、止损 -1.5%、止盈 +2.5%
  · 全程记录：quant.log（JSONL，决策/下单/开仓/平仓/错误/审计逐条落盘）+
    quant-trades.json（已平仓历史，重启恢复用）
  · 每 5 分钟自审计：用交易所 positionRisk 对账，发现漏记持仓自动补记、
    记录了但交易所已无的持仓自动核销，审计结果作为事件记录
  · 所有交易所调用均有 try/except，异常只会跳过本次决策并记录 error 事件，
    绝不影响 K线/形态主链路
"""
from __future__ import annotations

import functools
import json
import math
import os
import threading
import time
from collections import deque

from accounts import signed_request
from analysis import indicators as IND

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
                 min_pattern_winrate: float = 0.35, min_pattern_samples: int = 5):
        self.accts_box = accts_box          # main 的账户盒子（list/refresh）
        self.config_path = config_path
        self.price_fn = price_fn            # symbol -> 最新价（来自聚合器）
        self.enabled = True
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
        self._log_path = os.path.join(os.path.dirname(os.path.abspath(config_path)), "quant.log")
        self._trades_path = os.path.join(os.path.dirname(os.path.abspath(config_path)), "quant-trades.json")
        self._pos_path = os.path.join(os.path.dirname(os.path.abspath(config_path)), "quant-positions.json")
        self._unsupported_path = os.path.join(os.path.dirname(os.path.abspath(config_path)), "quant-unsupported.json")
        self._manual_path = os.path.join(os.path.dirname(os.path.abspath(config_path)), "quant-manual.json")
        self._manual: set[tuple[str, str]] = set()   # (account, symbol) 手动仓位：引擎不干预止盈止损
        self._manual_mtime = 0.0
        self._load_trades()
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

    def _place_order(self, api_key, api_secret, symbol, side, qty, reduce_only=False) -> dict:
        params = {"symbol": symbol.upper(), "side": side, "type": "MARKET",
                  "quantity": qty, "newOrderRespType": "RESULT"}
        if reduce_only:
            params["reduceOnly"] = "true"
        # 下单必须 POST（GET 会被路由到查询订单接口，报 -1102）
        r = signed_request(api_key, api_secret, "/fapi/v1/order", params, method="POST")
        self.stats["orders"] += 1
        return r

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
                    self.indicators[sym] = IND.market_state(cs)
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
            if dd["current"] / 100 <= -DD_LIMIT_PCT and now >= self.dd_state["paused_until"]:
                self.dd_state["paused_until"] = now + DD_PAUSE_MIN * 60_000
                self._event("skip",
                            f"账户回撤 {dd['current']:.2f}% 触及熔断线 "
                            f"{-DD_LIMIT_PCT * 100:.0f}%，暂停开仓 {DD_PAUSE_MIN} 分钟")
        except Exception:
            pass

    def _dd_paused(self) -> bool:
        return _now_ms() < self.dd_state.get("paused_until", 0)

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
            if direction not in ("bullish", "bearish"):
                # 中性形态（十字星等）不指示方向，无法决定多空 → 明确记录，便于核对决策流水完整性
                self._event("skip", f"{hit.get('name_zh') or '中性形态'}没有方向，不下单",
                            account=self._primary_account(), symbol=symbol,
                            pattern=hit.get("pattern"), name_zh=hit.get("name_zh"))
                return
            price = (self.price_fn or (lambda s: None))(symbol)
            if not price or price <= 0:
                self._event("skip", "取不到最新价，这次信号跳过", account=self._primary_account(), symbol=symbol)
                return
            # 市场状态过滤（形态之外的第二层）：震荡/极端波动/超买超卖/逆势
            if USE_MARKET_GATE:
                st = self.indicators.get(symbol) or {}
                if st:
                    ok, why = IND.signal_gate(st, direction)
                    if not ok:
                        self.gate_log.append({
                            "ts": _now_ms(), "symbol": symbol, "direction": direction,
                            "reason": why, "state": {
                                "regime": st.get("regime"), "rsi": st.get("rsi"),
                                "z": st.get("z"), "adx": st.get("adx"),
                                "volState": st.get("volState"), "squeeze": st.get("squeeze"),
                            },
                        })
                        self._event("skip", f"市场状态过滤：{why}", account=self._primary_account(),
                                    symbol=symbol, pattern=hit.get("pattern"))
                        return
            # 记录一次"收到信号"的思考过程：形态 / 方向 / 价格
            self._event("signal",
                        f"收到 {hit.get('name_zh') or hit.get('pattern')} "
                        f"{'看涨' if direction == 'bullish' else '看跌'}信号 @ {price}",
                        account=self._primary_account(), symbol=symbol,
                        pattern=hit.get("pattern"), name_zh=hit.get("name_zh"),
                        direction=direction, price=float(price))
            side = "BUY" if direction == "bullish" else "SELL"
            for a in self._tradable_accounts():
                name = a.get("name", "")
                try:
                    self._decide_open(name, a, symbol, side, direction, float(price), hit)
                except Exception as e:
                    self.stats["errors"] += 1
                    self._event("error", f"决策异常({name}/{symbol}): {e}", symbol=symbol)
        except Exception as e:
            self.stats["errors"] += 1
            self._event("error", f"信号处理异常: {e}")

    def _decide_open(self, name, a, symbol, side, direction, price, hit):
        st = self._st(name)
        now = _now_ms()
        # 风控 0：该合约未开通权限（如 TradFi 需签协议）→ 不再尝试
        if symbol in self._unsupported:
            return
        # 风控 0.5：账户回撤熔断
        if self._dd_paused():
            left = int((self.dd_state["paused_until"] - now) / 1000)
            self._event("skip", f"账户回撤熔断中，还需 {max(1, left)} 秒恢复",
                        account=name, symbol=symbol)
            return
        # 风控 1：该账户已有该币种持仓
        if symbol in st["positions"]:
            held = st["positions"][symbol].get("qty")
            self._event("skip", f"该币已有 {fmt_qty(held)} 的仓位，不重复开",
                        account=name, symbol=symbol)
            return
        # 风控 2：最大持仓数
        if len(st["positions"]) >= MAX_POSITIONS:
            self._event("skip",
                        f"仓位已满 {len(st['positions'])}/{MAX_POSITIONS}，等先平掉一个",
                        account=name, symbol=symbol)
            return
        # 风控 3：同币种冷却
        if now - st["cooldown"].get(symbol, 0) < COOLDOWN_SEC * 1000:
            left = int((COOLDOWN_SEC * 1000 - (now - st["cooldown"].get(symbol, 0))) / 1000)
            self._event("skip", f"刚平过这个币，还需等 {left} 秒",
                        account=name, symbol=symbol)
            return
        # 风控 3.5：同方向持仓上限（防止同向信号聚集造成方向过度集中）
        same_dir = sum(1 for p in st["positions"].values() if p.get("side") == side)
        if same_dir >= MAX_SAME_DIR:
            self._event("skip",
                        f"同方向已持 {same_dir} 个仓（上限 {MAX_SAME_DIR}），不再加",
                        account=name, symbol=symbol)
            return
        # 风控 3.7：形态历史胜率质量门——胜率过低的形态不入场（与形态统计系统联动）
        if self.pattern_stats_fn:
            pst = (self.pattern_stats_fn() or {}).get(hit.get("pattern"))
            if pst:
                n_dec = pst["wins"] + pst["losses"]
                if n_dec >= self.min_pattern_samples:
                    wr = pst["wins"] / n_dec
                    if wr < self.min_pattern_winrate:
                        self._event("skip",
                                    f"该形态历史胜率仅 {round(wr * 100)}%，低于门槛 {round(self.min_pattern_winrate * 100)}%，跳过",
                                    account=name, symbol=symbol)
                        return
        # 风控 4：可用余额与保证金
        avail = self._available(a)
        margin = max(MARGIN_MIN, round(avail * MARGIN_PCT, 2))
        if avail < MARGIN_MIN:
            self._event("skip",
                        f"可用余额 {fmt_qty(avail)} U，不够最低 {fmt_qty(MARGIN_MIN)} U",
                        account=name, symbol=symbol)
            return
        qty_raw = margin / price
        step = self._step_size(a["api_key"], a["api_secret"], symbol)
        qty = self._quantize(qty_raw, step)
        if qty <= 0:
            self._event("skip",
                        f"保证金 {fmt_qty(margin)} U 太少，按最小下单量算出 0，跳过",
                        account=name, symbol=symbol)
            return
        # 下单
        try:
            r = self._place_order(a["api_key"], a["api_secret"], symbol, side, qty)
        except Exception as e:
            self.stats["errors"] += 1
            if self._mark_unsupported(symbol, e):
                st["cooldown"][symbol] = now
                return
            self._event("error", f"下单失败({name}/{symbol}): {e}", symbol=symbol)
            st["cooldown"][symbol] = now
            return
        entry_price = float(r.get("avgPrice") or price)
        # 按当前 ATR 给出该仓位的止损/止盈价（不再用全局固定百分比）
        st_ind = self.indicators.get(symbol) or {}
        if USE_ATR_STOPS:
            plan = IND.adaptive_sl_tp(
                entry_price, st_ind.get("atr"), side,
                sl_mult=ATR_SL_MULT, tp_mult=ATR_TP_MULT,
                sl_min_pct=SL_MIN_PCT, tp_min_pct=TP_MIN_PCT,
                sl_max_pct=SL_MAX_PCT, tp_max_pct=TP_MAX_PCT)
        else:
            plan = IND.adaptive_sl_tp(
                entry_price, None, side,
                sl_min_pct=abs(SL_PCT), tp_min_pct=TP_PCT,
                sl_max_pct=abs(SL_PCT), tp_max_pct=TP_PCT)
        pos = {
            "symbol": symbol, "side": side, "qty": qty, "margin": margin,
            "entry": entry_price, "entryTs": now,
            "orderId": r.get("orderId"), "pattern": hit.get("pattern"),
            "name_zh": hit.get("name_zh"), "direction": direction,
            # 自适应止损止盈（监控时优先用这两个价，缺失则退回全局百分比）
            "sl": round(plan["sl"], 8) if plan["sl"] else None,
            "tp": round(plan["tp"], 8) if plan["tp"] else None,
            "slPct": round(plan["slPct"], 3),
            "tpPct": round(plan["tpPct"], 3),
            "atrAtEntry": st_ind.get("atr"),
            "regimeAtEntry": st_ind.get("regime"),
            "rsiAtEntry": st_ind.get("rsi"),
        }
        st["positions"][symbol] = pos
        st["cooldown"][symbol] = now
        self.stats["decisions"] += 1
        self._save_positions()
        self._event("open",
                    f"{'做多' if side == 'BUY' else '做空'} {symbol.upper()} "
                    f"数量 {qty} @ {pos['entry']}（保证金 {margin}）",
                    account=name, symbol=symbol, side=side, qty=qty, entry=pos["entry"],
                    orderId=pos["orderId"], margin=margin, pattern=hit.get("pattern"),
                    name_zh=hit.get("name_zh"), direction=direction,
                    slPct=pos["slPct"], tpPct=pos["tpPct"], atr=pos["atrAtEntry"])
        self._broadcast_positions()

    def _available(self, a) -> float:
        try:
            balances = signed_request(a["api_key"], a["api_secret"], "/fapi/v2/balance")
            for b in balances:
                if b.get("asset") == "USDT":
                    return float(b.get("availableBalance", 0))
        except Exception as e:
            self._event("error", f"可用余额获取失败({a.get('name')}): {e}")
        return 0.0

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
                elif peak >= max(TRAIL_ARM, (pos.get("slPct") or 0) / 100 * 0.5) and move <= peak - max(TRAIL_GAP, (pos.get("slPct") or 0) / 100 * 0.15):
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
        step = self._step_size(a["api_key"], a["api_secret"], symbol)
        close_qty = self._quantize(close_qty, step)
        if close_qty <= 0:
            st["positions"].pop(symbol)
            self._save_positions()
            return
        try:
            r = self._place_order(a["api_key"], a["api_secret"], symbol, close_side, close_qty, reduce_only=True)
        except Exception as e:
            pos["closeFails"] = fails + 1
            self.stats["errors"] += 1
            self._event("error", f"平仓失败({name}/{symbol}) 第{fails + 1}次: {e}",
                        symbol=symbol)
            if pos["closeFails"] >= 3:
                self._event("error", f"{name}/{symbol} 平仓连续失败3次，暂停重试等待审计核销", symbol=symbol)
            return
        move = (price - pos["entry"]) / pos["entry"] * (1 if pos["side"] == "BUY" else -1)
        pnl = round((price - pos["entry"]) * close_qty * (1 if pos["side"] == "BUY" else -1), 2)
        trade = {
            "ts": _now_ms(), "account": name, "symbol": symbol, "side": pos["side"],
            "qty": close_qty, "entry": pos["entry"], "exit": price, "pnl": pnl,
            "movePct": round(move * 100, 2), "reason": reason,
            "entryTs": pos["entryTs"], "holdMin": round((now_ms() - pos["entryTs"]) / 60000, 1),
            "pattern": pos.get("pattern"), "orderId": pos.get("orderId"), "closeId": r.get("orderId"),
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
                    qty=close_qty, entry=pos["entry"], exit=price)
        self._broadcast_positions()

    # ---------- 每 5 分钟自审计（对账 + 兜底） ----------

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
                    "limit": -DD_LIMIT_PCT * 100,
                },
                # 过滤器拦截记录（最近 N 条）
                "gateLog": list(self.gate_log)[-20:],
                # 当前策略参数（前端展示"系统在用哪些规则"）
                "params": {
                    "atrSlMult": ATR_SL_MULT, "atrTpMult": ATR_TP_MULT,
                    "slMinPct": SL_MIN_PCT * 100, "slMaxPct": SL_MAX_PCT * 100,
                    "tpMinPct": TP_MIN_PCT * 100, "tpMaxPct": TP_MAX_PCT * 100,
                    "ddLimitPct": -DD_LIMIT_PCT * 100, "ddPauseMin": DD_PAUSE_MIN,
                    "maxPositions": MAX_POSITIONS, "maxSameDir": MAX_SAME_DIR,
                    "cooldownSec": COOLDOWN_SEC, "maxHoldMin": MAX_HOLD_MIN,
                    "trailArm": TRAIL_ARM * 100, "trailGap": TRAIL_GAP * 100,
                    "marginPct": MARGIN_PCT * 100,
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
