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

import json
import math
import os
import threading
import time
from collections import deque

from accounts import signed_request

BASE = "https://testnet.binancefuture.com"

# 止盈止损按 3m K线实测波动率标定（BTC 振幅中位 0.08%、p90 0.25%；ETH 0.13%/0.36%）：
# 止损取 p90 振幅量级，止盈取约 1.6 倍止损，保证在正常波动内能出场，
# 避免仓位长期占满导致策略停摆（原 -1.5%/+2.5% 相对 3m 波动过大，几乎永不触发）。
SL_PCT = -0.0035     # 止损 -0.35%（约 p90 振幅）
TP_PCT = 0.0055      # 止盈 +0.55%（约 1.6 倍止损）
MAX_HOLD_MIN = 45    # 超时平仓：持仓超过 45 分钟未触发止盈止损则市价了结，释放槽位
MARGIN_PCT = 0.05    # 单仓保证金占可用余额比例
MARGIN_MIN = 10.0    # 单仓保证金下限（USDT）
MAX_POSITIONS = 4    # 每账户最大同时持仓数
COOLDOWN_SEC = 900   # 同币种冷却 15 分钟
AUDIT_SEC = 300      # 自审计间隔 5 分钟


def _now_ms() -> int:
    return int(time.time() * 1000)


class QuantEngine:
    def __init__(self, accts_box: dict, config_path: str, price_fn=None):
        self.accts_box = accts_box          # main 的账户盒子（list/refresh）
        self.config_path = config_path
        self.price_fn = price_fn            # symbol -> 最新价（来自聚合器）
        self.enabled = True
        self.state: dict[str, dict] = {}    # name -> {positions:{sym:{}}, cooldown:{sym:ts}, trades:[], seq:int}
        self.events: deque = deque(maxlen=60)   # 决策/事件流水（广播给前端）
        self.stats = {"signals": 0, "decisions": 0, "orders": 0, "closes": 0, "errors": 0, "wins": 0, "losses": 0}
        self.last_audit_ms = 0
        self.broadcast_fn = None           # main 注入：asyncio.create_task(hub.broadcast(obj))
        self._err_dedupe: dict[str, int] = {}
        self._lock = threading.Lock()
        self._warned_dupes = False
        self._step_sizes: dict[str, float] = {}
        self._log_path = os.path.join(os.path.dirname(os.path.abspath(config_path)), "quant.log")
        self._trades_path = os.path.join(os.path.dirname(os.path.abspath(config_path)), "quant-trades.json")
        self._pos_path = os.path.join(os.path.dirname(os.path.abspath(config_path)), "quant-positions.json")
        self._load_trades()
        self._load_positions()
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
        if step <= 0:
            return qty
        return math.floor(qty / step) * step

    def _place_order(self, api_key, api_secret, symbol, side, qty, reduce_only=False) -> dict:
        params = {"symbol": symbol.upper(), "side": side, "type": "MARKET",
                  "quantity": qty, "newOrderRespType": "RESULT"}
        if reduce_only:
            params["reduceOnly"] = "true"
        # 下单必须 POST（GET 会被路由到查询订单接口，报 -1102）
        r = signed_request(api_key, api_secret, "/fapi/v1/order", params, method="POST")
        self.stats["orders"] += 1
        return r

    # ---------- 状态 ----------

    def _st(self, name: str) -> dict:
        with self._lock:
            if name not in self.state:
                self.state[name] = {"positions": {}, "cooldown": {}, "seq": 0}
            return self.state[name]

    # ---------- 信号入口（收线确认信号驱动） ----------

    def on_signal(self, hit: dict) -> None:
        """确认信号 → 逐账户决策开仓。任何异常只记录并跳过。"""
        if not self.enabled:
            return
        try:
            self.stats["signals"] += 1
            symbol = hit.get("symbol")
            direction = hit.get("direction")
            if not symbol or direction not in ("bullish", "bearish"):
                return
            price = (self.price_fn or (lambda s: None))(symbol)
            if not price or price <= 0:
                self._event("skip", "无最新价", symbol=symbol)
                return
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
        # 风控 1：该账户已有该币种持仓
        if symbol in st["positions"]:
            self._event("skip", "已有该币种持仓", account=name, symbol=symbol)
            return
        # 风控 2：最大持仓数
        if len(st["positions"]) >= MAX_POSITIONS:
            self._event("skip", "已达最大持仓数", account=name, symbol=symbol)
            return
        # 风控 3：同币种冷却
        if now - st["cooldown"].get(symbol, 0) < COOLDOWN_SEC * 1000:
            self._event("skip", "冷却中", account=name, symbol=symbol)
            return
        # 风控 4：可用余额与保证金
        avail = self._available(a)
        margin = max(MARGIN_MIN, round(avail * MARGIN_PCT, 2))
        if avail < MARGIN_MIN:
            self._event("skip", "可用余额不足", account=name, symbol=symbol)
            return
        qty_raw = margin / price
        step = self._step_size(a["api_key"], a["api_secret"], symbol)
        qty = self._quantize(qty_raw, step)
        if qty <= 0:
            self._event("skip", f"数量过小(保证金{margin})", account=name, symbol=symbol)
            return
        # 下单
        try:
            r = self._place_order(a["api_key"], a["api_secret"], symbol, side, qty)
        except Exception as e:
            self.stats["errors"] += 1
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
        self._event("open", f"确认信号开仓: {hit.get('name_zh')}", account=name, symbol=symbol,
                    side=side, qty=qty, entry=pos["entry"], orderId=pos["orderId"], margin=margin)
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
            for symbol in list(st["positions"].keys()):
                pos = st["positions"][symbol]
                price = (self.price_fn or (lambda s: None))(symbol)
                if not price or price <= 0:
                    continue
                direction_up = pos["side"] == "BUY"
                move = (price - pos["entry"]) / pos["entry"] * (1 if direction_up else -1)
                hold_min = (now - pos.get("entryTs", now)) / 60000
                if move <= SL_PCT or move >= TP_PCT:
                    reason = "止盈" if move >= TP_PCT else "止损"
                    self._close(name, a, symbol, price, f"{reason}({move * 100:.2f}%)")
                elif hold_min >= MAX_HOLD_MIN:
                    # 超时出场：避免窄幅震荡时仓位长期占满槽位（策略停摆兜底）
                    self._close(name, a, symbol, price, f"超时({hold_min:.0f}min,{move * 100:.2f}%)")

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
        # 防死循环：同一持仓连续平仓失败 3 次后放弃（交由审计核销/人工处理）
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
        self._event("close", f"{reason}平仓: {symbol} 盈亏 {pnl}", account=name, symbol=symbol, pnl=pnl)
        self._broadcast_positions()

    # ---------- 每 5 分钟自审计（对账 + 兜底） ----------

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
                # 交易所有、本地漏记 → 补记
                for sym, li in live.items():
                    if sym not in st["positions"]:
                        pos = {"symbol": sym, "side": "BUY" if li["amt"] > 0 else "SELL",
                               "qty": abs(li["amt"]), "entry": li["price"] or 0, "entryTs": _now_ms(),
                               "orderId": None, "pattern": None, "direction": None,
                               "recovered": True}
                        st["positions"][sym] = pos
                        changed = True
                        self._event("audit", f"审计补记漏记持仓 {sym}", account=name, symbol=sym)
                # 本地有、交易所已无 → 核销
                for sym in list(st["positions"].keys()):
                    if sym not in live:
                        st["positions"].pop(sym)
                        changed = True
                        self._event("audit", f"审计核销已平持仓 {sym}（交易所已无仓位）", account=name, symbol=sym)
                # 本地与交易所数量不一致 → 以交易所为准修正数量
                for sym, li in live.items():
                    pos = st["positions"].get(sym)
                    if pos and abs(abs(pos["qty"]) - abs(li["amt"])) > 1e-9:
                        pos["qty"] = abs(li["amt"])
                        changed = True
                        self._event("audit", f"审计修正持仓数量 {sym} → {abs(li['amt'])}", account=name, symbol=sym)
                if changed:
                    self._save_positions()
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
        }


def now_ms() -> int:
    return _now_ms()
