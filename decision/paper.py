"""模拟仓：每个监控币种跑一个 15 分钟的纸上交易，用来验证策略而不是真下单。

设计要点（为什么这样定）：

1. **每币一仓，永不停止**
   平仓后立刻按当前数据再开一个。停掉就看不到策略在连续行情下的表现。

2. **止盈止损是动态的，但只允许收紧**
   每轮按当前 ATR 重算 SL/TP；已经朝有利方向走过的仓位，止损只能往
   盈利方向移（移动止损），绝不放大。放大会让"已经赚到的"重新暴露风险。

3. **胜率是算出来的，不是拍的**
   三个真实分量加权，每一步都写清楚：
     baseline = SL 距离 / (SL 距离 + TP 距离)
                随机游走下"先碰到 TP"的概率（RR 1:2 时是 1/3）
     edge     = (评分 − 50) / 50，截断到 ±0.5
                信号给出的方向优势（评分 75 → +0.5）
     p        = baseline + edge × 0.6
     再与形态历史胜率混（样本 ≥ 5 才参与，权重 0.3）
   这样 RR 越大、baseline 越低，但方向优势能把概率抬回来；
   评分低或者形态一直很差时，胜率会真的低下去。

4. **强平价按维持保证金折算**
   liq = entry × (1 ∓ 1/leverage × 0.95)，0.95 是维持保证金余量。

5. **方向翻转不提前离场**
   评分每 5 秒重算一次，方向在小分差上来回摆是常态。曾经把"方向翻了"
   当成离场信号，结果线上 11 小时平了 9762 笔、100% 是 flip、中位持有
   6 秒——15 分钟的窗口一次都走不完，胜率统计全是噪声。
   纸上交易要验证的假设是"按信号方向持有 15 分钟"，所以只由
   止盈 / 止损 / 到期离场；方向变了也等这一仓走完，下一仓再按新方向开。
   风险仍由止损兜底，不需要用"翻向就跑"来控风险。
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field, asdict

# 一个模拟仓的生命周期
HOLD_MS = 15 * 60 * 1000
# 每个模拟仓的固定名义价值（USDT）。固定下来才能横向比较策略表现。
NOTIONAL = 1000.0
# 历史记录条数（内存环形，不落库）
MAX_HISTORY = 200
# 动态止损：ATR 的倍数
TRAIL_ATR = 1.4
# 最强不过 20x：模拟仓是为了验证信号，不是赌爆仓
MAX_PAPER_LEV = 20


def _num(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _round(v, digits=2):
    return round(v, digits) if v is not None else None


def estimate_winrate(score, gap, sl_pct, tp_pct, pattern_stats=None,
                     pattern: str = "") -> dict:
    """估算这一仓"先碰到止盈"的概率。返回概率与每一项的分量。

    公式与理由见模块注释。所有输入都来自真实计算，没有魔法常数之外的拍脑袋。
    """
    s = _num(score) or 0.0
    g = _num(gap) or 0.0
    sl = _num(sl_pct)
    tp = _num(tp_pct)
    if not sl or not tp or sl <= 0 or tp <= 0:
        # 缺 SL/TP 就算不出概率，别编
        return {"prob": None, "parts": {}}

    # 1) 随机游走基线：先触 TP 的概率 = SL 距离 / 总距离
    baseline = sl / (sl + tp)
    # 2) 方向优势：评分越高于 50 越有把握，gap 也加分
    edge = max(-0.5, min(0.5, (s - 50.0) / 50.0))
    gap_bonus = max(0.0, min(0.15, g / 400.0))
    p = baseline + (edge + gap_bonus) * 0.6
    parts = {
        "baseline": round(baseline, 4),
        "edge": round(edge, 4),
        "gapBonus": round(gap_bonus, 4),
        "afterSignal": round(p, 4),
    }

    # 3) 形态历史胜率：样本足够才参与，权重 0.3
    stats = (pattern_stats or {}).get(pattern) if pattern else None
    if isinstance(stats, dict):
        n = _num(stats.get("effectiveSamples")) or _num(stats.get("count")) or 0
        wr = _num(stats.get("weightedWinrate"))
        if wr is not None and n >= 5:
            p = p * 0.7 + wr * 0.3
            parts["patternWinrate"] = round(wr, 4)
            parts["patternWeight"] = 0.3
    p = max(0.05, min(0.95, p))
    parts["final"] = round(p, 4)
    return {"prob": round(p * 100, 1), "parts": parts}


@dataclass
class PaperPosition:
    symbol: str
    direction: str                 # long / short
    entry: float
    entry_ts: int
    expires_ts: int
    leverage: int
    notional: float
    margin: float
    qty: float
    liq: float
    sl: float
    tp: float
    sl_pct: float
    tp_pct: float
    rr: float | None
    score: float | None
    gap: float | None
    grade: str
    reason: str
    winrate: float | None
    winrate_parts: dict = field(default_factory=dict)
    # 开仓时的 ATR（价格口径）。移动止损必须用它做基准，不能用每轮的当前 ATR：
    # 行情转静时 ATR 会塌下去，止损就会被拽到贴着现价，一碰就出局。
    atr_at_entry: float = 0.0
    # 运行中更新
    mark: float = 0.0
    upnl: float = 0.0
    upnl_pct: float = 0.0        # 相对保证金
    peak: float = 0.0            # 最有利价格（移动止损用）
    trailing: bool = False       # 止损是否已被移动过
    ticks: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def _liq_price(entry: float, leverage: int, direction: str) -> float:
    """强平价：按维持保证金余量折算，多空对称。"""
    span = 1.0 / max(1, leverage) * 0.95
    return entry * (1 - span) if direction == "long" else entry * (1 + span)


class PaperBook:
    """所有币种的模拟仓集合。线程安全（manage 在 thread pool 里跑）。"""

    def __init__(self):
        self._lock = threading.RLock()
        self.positions: dict[str, PaperPosition] = {}
        self.history: list[dict] = []
        self.stats = {"opened": 0, "closed": 0, "wins": 0, "losses": 0,
                      "timeouts": 0, "netPnl": 0.0}

    # ---------- 对外 ----------

    def snapshot(self, symbols: list[str], candles_fn=None) -> list[dict]:
        """给前端的快照：每个币一个盒子，附带最近 10 根K线。"""
        out = []
        with self._lock:
            for sym in symbols:
                pos = self.positions.get(sym)
                if pos is None:
                    continue
                item = pos.to_dict()
                item["candles"] = self._candles(sym, candles_fn)
                out.append(item)
        return out

    def history_list(self) -> list[dict]:
        with self._lock:
            return list(self.history[-MAX_HISTORY:])

    def summary(self) -> dict:
        """按币种汇总模拟仓战绩（只统计已平仓）。

        两个口径必须分开报，否则会把"最近 200 条"当成"全部"：
          · session：进程启动以来的累计计数（stats，可能远多于窗口）
          · bySymbol：只覆盖内存里最近 MAX_HISTORY 条平仓记录
        Agent 回答"哪个模拟仓胜率最高"用的就是 bySymbol；不能拿实盘账户去推。
        """
        with self._lock:
            rows = list(self.history)
            open_count = len(self.positions)
            stats = dict(self.stats)
        by: dict[str, dict] = {}
        for r in rows:
            sym = str(r.get("symbol") or "")
            if not sym:
                continue
            item = by.setdefault(sym, {"symbol": sym, "closed": 0, "wins": 0,
                                       "losses": 0, "timeouts": 0, "netPnl": 0.0})
            item["closed"] += 1
            pnl = _num(r.get("pnl")) or 0.0
            item["netPnl"] = round(item["netPnl"] + pnl, 4)
            if pnl > 0:
                item["wins"] += 1
            elif pnl < 0:
                item["losses"] += 1
            if r.get("reason") == "timeout":
                item["timeouts"] += 1
        ranked = []
        for item in by.values():
            item["winrate"] = (round(item["wins"] / item["closed"] * 100, 1)
                               if item["closed"] else None)
            item["avgPnl"] = (round(item["netPnl"] / item["closed"], 4)
                              if item["closed"] else None)
            ranked.append(item)
        # 胜率高的在前；胜率相同比样本量。没有平仓记录的币不上榜。
        ranked.sort(key=lambda x: (-(x["winrate"] or 0.0), -x["closed"]))
        return {
            "note": "模拟仓=15 分钟纸上交易，不产生真实委托，与账户实盘持仓无关。"
                    "session 是进程启动以来的累计值；bySymbol 只覆盖内存里最近 "
                    f"{MAX_HISTORY} 条平仓记录，样本少时不要当稳定胜率。",
            "openCount": open_count,
            "session": {
                "closed": stats["closed"], "wins": stats["wins"],
                "losses": stats["losses"], "timeouts": stats["timeouts"],
                "winrate": (round(stats["wins"] / stats["closed"] * 100, 1)
                            if stats["closed"] else None),
                "netPnl": round(stats["netPnl"], 4),
            },
            "window": {"records": len(rows), "limit": MAX_HISTORY},
            "bySymbol": ranked,
        }

    def force(self, symbol: str, ctx: dict) -> dict:
        """手动刷新：立刻平掉当前仓（按现价），再按当前数据重开一个。"""
        with self._lock:
            pos = self.positions.get(symbol)
            if pos is not None:
                mark = _num(ctx.get("price")) or pos.mark or pos.entry
                self._close(pos, mark, "manual", ctx)
        return self.tick(symbol, ctx)

    def tick(self, symbol: str, ctx: dict) -> dict | None:
        """推进一步。没有持仓就开仓；有持仓就更新盈亏、判断离场。

        ctx 需要的字段：
          price, atr, score, gap, grade, direction, reason, regime,
          pattern, patternStats, maxLeverage
        返回当前持仓的 dict（或 None）。
        """
        price = _num(ctx.get("price"))
        if not price or price <= 0:
            return self.positions.get(symbol).to_dict() if symbol in self.positions else None

        with self._lock:
            pos = self.positions.get(symbol)
            if pos is None:
                self._open(symbol, ctx, price)
                pos = self.positions.get(symbol)
                return pos.to_dict() if pos else None

            pos.ticks += 1
            pos.mark = price
            # 已实现到保证金的浮动盈亏
            sign = 1.0 if pos.direction == "long" else -1.0
            pos.upnl = round((price - pos.entry) * pos.qty * sign, 4)
            pos.upnl_pct = round(pos.upnl / pos.margin * 100, 2) if pos.margin else 0.0

            # 更新最有利价（移动止损用）
            if (pos.direction == "long" and price > pos.peak) or \
               (pos.direction == "short" and price < pos.peak):
                pos.peak = price

            exit_reason = self._check_exit(pos, price)
            if exit_reason:
                self._close(pos, price, exit_reason, ctx)
                # 平完立刻按当前数据重开，仓位永不停
                self._open(symbol, ctx, price)
            else:
                self._trail(pos, ctx)
            pos = self.positions.get(symbol)
            return pos.to_dict() if pos else None

    def drop_missing(self, keep: set[str]) -> None:
        """币种被移出监控时清掉它的模拟仓（不记录，因为不是正常平仓）。"""
        with self._lock:
            for sym in list(self.positions):
                if sym not in keep:
                    self.positions.pop(sym, None)

    # ---------- 内部 ----------

    def _candles(self, sym: str, candles_fn) -> list[dict]:
        """最近 10 根K线，只带画图需要的字段。"""
        if not candles_fn:
            return []
        try:
            bars = candles_fn(sym) or []
        except Exception:
            return []
        out = []
        for c in bars[-10:]:
            out.append({
                "t": int(getattr(c, "open_time", 0) or 0),
                "o": _round(_num(getattr(c, "open", None)), 4),
                "h": _round(_num(getattr(c, "high", None)), 4),
                "l": _round(_num(getattr(c, "low", None)), 4),
                "c": _round(_num(getattr(c, "close", None)), 4),
            })
        return out

    def _open(self, symbol: str, ctx: dict, price: float) -> None:
        """按当前评估开一个模拟仓。数据不全就不开——不编。"""
        direction = str(ctx.get("direction") or "")
        if direction not in ("bullish", "bearish"):
            return
        atr = _num(ctx.get("atr"))
        sl_pct = (atr * 1.5 / price) if (atr and atr > 0) else 0.008
        sl_pct = min(0.02, max(0.004, sl_pct))
        tp_pct = max(sl_pct * 1.6, min(0.03, (atr * 2.5 / price) if (atr and atr > 0) else 0.018))

        lev = int(_num(ctx.get("maxLeverage")) or 20)
        lev = max(1, min(MAX_PAPER_LEV, lev))
        bullish = direction == "bullish"
        entry = price
        sl = entry * (1 - sl_pct) if bullish else entry * (1 + sl_pct)
        tp = entry * (1 + tp_pct) if bullish else entry * (1 - tp_pct)
        margin = NOTIONAL / lev
        qty = NOTIONAL / entry

        wr = estimate_winrate(ctx.get("score"), ctx.get("gap"), sl_pct, tp_pct,
                              ctx.get("patternStats"), str(ctx.get("pattern") or ""))

        now = int(time.time() * 1000)
        pos = PaperPosition(
            symbol=symbol,
            direction="long" if bullish else "short",
            entry=_round(entry, 4),
            entry_ts=now,
            expires_ts=now + HOLD_MS,
            leverage=lev,
            notional=NOTIONAL,
            margin=_round(margin, 2),
            qty=round(qty, 6),
            liq=_round(_liq_price(entry, lev, "long" if bullish else "short"), 4),
            sl=_round(sl, 4),
            tp=_round(tp, 4),
            sl_pct=round(sl_pct * 100, 2),
            tp_pct=round(tp_pct * 100, 2),
            rr=round(tp_pct / sl_pct, 2) if sl_pct else None,
            score=_round(_num(ctx.get("score")), 1),
            gap=_round(_num(ctx.get("gap")), 1),
            grade=str(ctx.get("grade") or ""),
            reason=str(ctx.get("reason") or "")[:40],
            winrate=wr["prob"],
            winrate_parts=wr["parts"],
            atr_at_entry=round(atr, 8) if (atr and atr > 0) else 0.0,
            mark=entry,
            peak=entry,
        )
        self.positions[symbol] = pos
        self.stats["opened"] += 1

    def _check_exit(self, pos: PaperPosition, price: float) -> str:
        """判断是否该离场：止盈 / 止损 / 到期。

        方向翻转**不算**离场信号：评分每 5 秒重算、方向在小分差上反复摆，
        拿它当离场条件会让 15 分钟的窗口永远走不完（线上实测中位持有 6 秒、
        100% 靠 flip 平仓），胜率统计随之失真。见模块注释第 5 条。
        """
        long_ = pos.direction == "long"
        if long_:
            if price >= pos.tp:
                return "tp"
            if price <= pos.sl:
                return "sl"
        else:
            if price <= pos.tp:
                return "tp"
            if price >= pos.sl:
                return "sl"
        if int(time.time() * 1000) >= pos.expires_ts:
            return "timeout"
        return ""

    def _trail(self, pos: PaperPosition, ctx: dict) -> None:
        """移动止损：先把浮盈跑出一段再抬，且只往盈利方向移。

        两个必须守住的约束，缺一个都会把 15 分钟的交易变成秒级刮头皮：

        1. **基准用开仓时的 ATR，不用当前 ATR。**
           行情转静时当前 ATR 会塌下去（BTC 5 分钟 ATR 能从 60 掉到 8），
           用当前值算「peak − 1.4 ATR」就等于把止损贴到现价上，
           价格回抽一点点就被打掉。线上实测止损距离被压到 0.01%–0.17%，
           9 笔里 8 笔在 2–6 分钟内止损，全是被这一条坑的。
        2. **先触发再移（arming）。** 价格至少朝有利方向走 1 个 ATR，
           才允许把止损抬到成本之上；没走出这段空间就保持原止损不动，
           否则等于开仓即被一个极窄的止损罩住。

        移动后仍保证止损在现价的正确一侧（多头的止损必须低于现价）。
        """
        atr = _num(pos.atr_at_entry) or _num(ctx.get("atr"))
        if not atr or atr <= 0:
            return
        long_ = pos.direction == "long"
        # 未武装：价格还没走出 1 个 ATR，不动止损
        if long_:
            if pos.peak < pos.entry + atr:
                return
            candidate = pos.peak - atr * TRAIL_ATR
            if candidate > pos.sl and candidate < pos.mark:
                pos.sl = _round(candidate, 4)
                pos.trailing = True
                pos.sl_pct = round((pos.entry - pos.sl) / pos.entry * 100, 2)
        else:
            if pos.peak > pos.entry - atr:
                return
            candidate = pos.peak + atr * TRAIL_ATR
            if candidate < pos.sl and candidate > pos.mark:
                pos.sl = _round(candidate, 4)
                pos.trailing = True
                pos.sl_pct = round((pos.sl - pos.entry) / pos.entry * 100, 2)

    def _close(self, pos: PaperPosition, price: float, reason: str, ctx: dict) -> None:
        qty_actual = (price - pos.entry) * pos.qty * (1 if pos.direction == "long" else -1)
        pnl = round(qty_actual, 4)
        self.positions.pop(pos.symbol, None)
        record = {
            "symbol": pos.symbol,
            "direction": pos.direction,
            "entry": pos.entry,
            "exit": _round(price, 4),
            "sl": pos.sl,
            "tp": pos.tp,
            "leverage": pos.leverage,
            "margin": pos.margin,
            "qty": pos.qty,
            "pnl": pnl,
            "pnlPct": round(pnl / pos.margin * 100, 2) if pos.margin else 0,
            "reason": reason,
            "winrate": pos.winrate,
            "score": pos.score,
            "grade": pos.grade,
            "heldMs": max(0, int(time.time() * 1000) - pos.entry_ts),
            "closedAt": int(time.time() * 1000),
            "trailing": pos.trailing,
        }
        self.history.append(record)
        if len(self.history) > MAX_HISTORY:
            self.history = self.history[-MAX_HISTORY:]
        self.stats["closed"] += 1
        if pnl > 0:
            self.stats["wins"] += 1
        elif pnl < 0:
            self.stats["losses"] += 1
        if reason == "timeout":
            self.stats["timeouts"] += 1
        self.stats["netPnl"] = round(self.stats["netPnl"] + pnl, 4)
