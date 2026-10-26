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
        self._err_dedupe: dict[str, int] = {}
        self._mark_cache: dict[str, tuple] = {}
        self._unsupported: set[str] = set()   # 未开通合约权限的币种（如 TradFi 需签协议）
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
        self._event("system", "量化引擎启动", enabled=True)

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
                self.storage_fn(ev)
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
                self._event("system", f"恢复未平仓持仓 {n} 个（含 entry 价与开仓时间）")
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
            self._event("system", f"手动仓位注册表更新: {len(self._manual)} 个",
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
                        f"账户别名去重：{', '.join(dup_names)} 与已启用账户共用同一 API Key，"
                        f"已跳过（同一账户只交易一份）")
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
                    self._event("system", f"恢复跳过列表: {', '.join(sorted(self._unsupported))}")
        except Exception:
            pass

    # ---------- 状态 ----------

    def _st(self, name: str) -> dict:
        with self._lock:
            if name not in self.state:
                self.state[name] = {"positions": {}, "cooldown": {}, "seq": 0}
            return self.state[name]

    # ---------- 信号入口（收线确认信号驱动） ----------

    @_serialized
    def on_signal(self, hit: dict) -> None:
        """确认信号 → 逐账户决策开仓。任何异常只记录并跳过。"""
        if not self.enabled:
            return
        try:
            self.stats["signals"] += 1
            symbol = hit.get("symbol")
            direction = hit.get("direction")
            if not symbol:
                return
            if direction not in ("bullish", "bearish"):
                # 中性形态（十字星等）不指示方向，无法决定多空 → 明确记录，便于核对决策流水完整性
                self._event("skip", f"中性形态不参与交易（{hit.get('name_zh') or 'neutral'} 无方向）",
                            account=self._primary_account(), symbol=symbol,
                            pattern=hit.get("pattern"), name_zh=hit.get("name_zh"))
                return
            price = (self.price_fn or (lambda s: None))(symbol)
            if not price or price <= 0:
                self._event("skip", "无最新价，无法计算下单数量", account=self._primary_account(), symbol=symbol)
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
        # 风控 1：该账户已有该币种持仓
        if symbol in st["positions"]:
            self._event("skip", f"该币种已有持仓（{st['positions'][symbol].get('qty')}），不重复开仓",
                        account=name, symbol=symbol)
            return
        # 风控 2：最大持仓数
        if len(st["positions"]) >= MAX_POSITIONS:
            self._event("skip", f"已达最大持仓数（{len(st['positions'])}/{MAX_POSITIONS}）",
                        account=name, symbol=symbol)
            return
        # 风控 3：同币种冷却
        if now - st["cooldown"].get(symbol, 0) < COOLDOWN_SEC * 1000:
            left = int((COOLDOWN_SEC * 1000 - (now - st["cooldown"].get(symbol, 0))) / 1000)
            self._event("skip", f"冷却中（还需 {left}s）", account=name, symbol=symbol)
            return
        # 风控 3.5：同方向持仓上限（防止同向信号聚集造成方向过度集中）
        same_dir = sum(1 for p in st["positions"].values() if p.get("side") == side)
        if same_dir >= MAX_SAME_DIR:
            self._event("skip", f"同方向持仓已达上限（{same_dir}/{MAX_SAME_DIR}）",
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
                                    f"形态历史胜率过低({round(wr * 100)}%, n={n_dec})，质量门拦截",
                                    account=name, symbol=symbol)
                        return
        # 风控 4：可用余额与保证金
        avail = self._available(a)
        margin = max(MARGIN_MIN, round(avail * MARGIN_PCT, 2))
        if avail < MARGIN_MIN:
            self._event("skip", f"可用余额不足（{round(avail, 2)} < 最低 {MARGIN_MIN}）",
                        account=name, symbol=symbol)
            return
        qty_raw = margin / price
        step = self._step_size(a["api_key"], a["api_secret"], symbol)
        qty = self._quantize(qty_raw, step)
        if qty <= 0:
            self._event("skip", f"数量过小（保证金 {margin}，按步长 {step} 取整后为 0）",
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
        pos = {
            "symbol": symbol, "side": side, "qty": qty, "margin": margin,
            "entry": float(r.get("avgPrice") or price), "entryTs": now,
            "orderId": r.get("orderId"), "pattern": hit.get("pattern"),
            "name_zh": hit.get("name_zh"), "direction": direction,
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
                    name_zh=hit.get("name_zh"), direction=direction)
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
        """每 5 秒调用：盯每仓盈亏，触发止损/止盈即平仓。"""
        if not self.enabled:
            return
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
                if move <= SL_PCT or move >= TP_PCT:
                    reason = "止盈" if move >= TP_PCT else "止损"
                    self._close(name, a, symbol, price, f"{reason}({move * 100:.2f}%)")
                elif peak >= TRAIL_ARM and move <= peak - TRAIL_GAP:
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
                self._event("close", f"{reason}（交易所已无仓位，核销记录）", account=name, symbol=symbol)
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
                        self._event("audit", f"审计补记漏记持仓 {sym}", account=name, symbol=sym)
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
                                    f"审计核销已平持仓 {sym}（交易所已无仓位{'，此前平仓熔断' if was_stuck else ''}）",
                                    account=name, symbol=sym)
                # 本地与交易所数量不一致 → 以交易所为准修正数量
                for sym, li in live.items():
                    pos = st["positions"].get(sym)
                    if pos and abs(abs(pos["qty"]) - abs(li["amt"])) > 1e-9:
                        pos["qty"] = abs(li["amt"])
                        changed = True
                        self._event("audit", f"审计修正持仓数量 {sym} → {abs(li['amt'])}", account=name, symbol=sym)
                # 豁免登记的仓位在交易所已不存在（未被跟踪故未走上面核销）→ 清理登记，
                # 否则残留豁免会让量化引擎后续在同账户同币种开的仓绕过止盈止损
                for (n, sym) in list(self._manual):
                    if n == name and sym not in live:
                        self._manual.discard((n, sym))
                        self._save_manual()
                        self._event("audit", f"清理已平手动仓位的豁免登记 {sym}", account=name, symbol=sym)
                if changed:
                    self._save_positions()
                # 审计周期重置平仓失败计数：熔断只是暂停，不是永久放弃（下次重试）
                for pos in st["positions"].values():
                    if pos.get("closeFails", 0) >= 3:
                        pos["closeFails"] = 0
                        self._event("audit", f"重置平仓重试计数 {pos.get('symbol')}（进入新一轮尝试）",
                                    account=name, symbol=pos.get("symbol"))
            self._event("audit", "5 分钟自审计完成", positions={n: list(self._st(n)["positions"].keys()) for n in self.state})
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
        }


def now_ms() -> int:
    return _now_ms()
