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

6. **账本必须进数据库，不能只在内存**
   2026-11 之前这里是纯内存账本：仓位、平仓记录、累计统计全在
   `self.positions / self.history / self.stats` 里，进程一重启全部清零，
   也没法用 SQL 做长期策略分析。现在由 main 注入 MySQL 存储层：
     · 未平仓仓位 → paper_positions 表（每次 tick 覆盖，重启原样恢复，
       连移动止损之后的 sl、最有利价 peak 都保留）
     · 已平仓记录 → paper_trades 表（append-only，默认永久保留）
     · 汇总统计 → 直接按 paper_trades 全量聚合（session / bySymbol 同口径）
   store=None 时（单元测试 / 无 MySQL 降级）仍按纯内存跑，行为与旧版一致。
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import asdict, dataclass, field, fields

# 一个模拟仓的生命周期
HOLD_MS = 15 * 60 * 1000
# 每个模拟仓的固定名义价值（USDT）。固定下来才能横向比较策略表现。
NOTIONAL = 1000.0
# 内存里保留的最近平仓记录条数（只是缓存；全量在 MySQL paper_trades 表）
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
    # 开仓时的形态与行情状态（平仓记录要带上，便于按形态/市况复盘）
    pattern: str = ""
    regime: str = ""
    # 运行中更新
    mark: float = 0.0
    upnl: float = 0.0
    upnl_pct: float = 0.0        # 相对保证金
    peak: float = 0.0            # 最有利价格（移动止损用）
    trailing: bool = False       # 止损是否已被移动过
    ticks: int = 0
    # 开仓单调序号：同一毫秒内平仓并重开也不会让 trade_key 碰撞。
    # 进程内从 1 递增，重启时从恢复出来的最大值继续，不能跨重启归零。
    open_seq: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


# 从库里恢复仓位 JSON 时只认这些字段，多出来的（旧版本/新版本）一律忽略
_POSITION_FIELDS = tuple(f.name for f in fields(PaperPosition))


def _position_from_dict(data) -> "PaperPosition | None":
    """把 paper_positions.data 里的 JSON 还原成 PaperPosition。

    缺字段/类型不对的脏数据返回 None（跳过这一条），不能让一条坏记录
    把整个进程的启动恢复带崩。
    """
    if not isinstance(data, dict):
        return None
    known = {name: data[name] for name in _POSITION_FIELDS if name in data}
    try:
        pos = PaperPosition(**known)
    except (TypeError, ValueError):
        return None
    # 字段类型也要校验并**强转**：库里被手工改坏/旧版本残留的行如果带着
    # entry="abc"（或 "1.5" 这种数值字符串）进来，tick 里 ""100" - 1" /
    # price > peak 会抛 TypeError 被引擎静默吞掉，那个币的模拟仓就永久停摆。
    # 坏行直接跳过（treated as 未恢复），能救的数值字符串转成真正的 float/int。
    if not str(pos.symbol or "").strip():
        return None
    if pos.direction not in ("long", "short"):
        return None
    int_fields = ("entry_ts", "expires_ts", "leverage", "ticks", "open_seq")
    float_fields = ("entry", "qty", "sl", "tp", "margin", "liq", "notional",
                    "sl_pct", "tp_pct", "atr_at_entry", "mark", "upnl",
                    "upnl_pct", "peak")
    for name in int_fields + float_fields:
        value = _num(getattr(pos, name, None))
        if value is None:
            return None
        setattr(pos, name, int(value) if name in int_fields else float(value))
    for name in ("entry", "entry_ts", "expires_ts", "leverage", "qty",
                 "sl", "tp", "margin", "liq"):
        if getattr(pos, name) <= 0:
            return None
    # 可选数值：None 是合法值（如没算胜率），但 "abc" 这种垃圾要跳过整行
    for name in ("rr", "score", "gap", "winrate"):
        raw = getattr(pos, name, None)
        if raw is None:
            continue
        value = _num(raw)
        if value is None:
            return None
        setattr(pos, name, float(value))
    return pos


def _liq_price(entry: float, leverage: int, direction: str) -> float:
    """强平价：按维持保证金余量折算，多空对称。"""
    span = 1.0 / max(1, leverage) * 0.95
    return entry * (1 - span) if direction == "long" else entry * (1 + span)


class PaperBook:
    """所有币种的模拟仓集合。线程安全（manage 在 thread pool 里跑）。

    store 是 MySQL 持久化接口（storage.Storage 那套方法，鸭子类型；None=纯内存）：
      · 开仓 + 每轮更新 → paper_positions（重启原样恢复，移动止损后的 sl 也保留）
      · 平仓 → paper_trades（append-only，重启不清零，可直接 SQL 做策略分析）
      · 汇总 → paper_trades 全量聚合（session 与 bySymbol 同一口径）
    落库失败只打一次日志、绝不让 5 秒一轮的推演中断；内存账本照常跑。
    """

    def __init__(self, store=None):
        self._lock = threading.RLock()
        self.positions: dict[str, PaperPosition] = {}
        self.history: list[dict] = []
        self.stats = {"opened": 0, "closed": 0, "wins": 0, "losses": 0,
                      "timeouts": 0, "netPnl": 0.0}
        # Storage 未启用（没配 MySQL / 连不上，enabled=False）时等同于没接库：
        # 否则 summary 会读到一张空表并误报 source=mysql。
        if store is not None and not bool(getattr(store, "enabled", True)):
            store = None
        self.store = store
        # 刚从库里恢复、还没在 tick 里处理过的仓位。停机期间可能已经到期，
        # 要用这段时间的 K 线补算真实离场价，不能拿重启后的现价硬平。
        self._recovered: set[str] = set()
        self._storage_error = ""
        # 落库失败但还没写进 paper_trades 的平仓流水：先留在内存里重试，
        # 这段时间不删/不覆盖 paper_positions 的旧行——宁可重启后按旧仓
        # 再结算一次，也不能让一笔成交凭空消失。
        self._pending_trades: list[dict] = []
        self._pending_deletes: set[str] = set()
        # 开仓单调序号（进程内递增，restore 后从最大值续号）：
        # trade_key = symbol:entryTs:openSeq 用它消除"同毫秒平仓重开"的碰撞。
        self._open_seq = 0
        # 启动时读不到 paper_positions（数据库不可用）：本进程退化为纯内存账本，
        # 不写库——否则新仓 REPLACE 会把库里没读到的那行覆盖掉。重启后再恢复。
        self._store_suspended = False
        # 启动恢复结果，main 用它打一行日志（恢复了几个仓、多少条历史）
        self.restore_info = {"positions": 0, "trades": 0, "closed": 0}
        if self.store is not None:
            self.restore_info = self.restore()

    # ---------- 持久化 ----------

    def restore(self) -> dict:
        """从数据库恢复未平仓仓位、最近平仓记录与累计统计（启动时调用）。

        返回恢复了多少，供启动日志核对。store 未接入时返回全 0。
        """
        summary = {"positions": 0, "trades": 0, "closed": 0}
        if self.store is None:
            return summary
        rows = self._read("恢复未平仓仓位", self.store.paper_positions)
        positions_ok = rows is not None
        loaded: dict[str, PaperPosition] = {}
        for data in rows or []:
            pos = _position_from_dict(data)
            if pos is None:
                continue
            # _recovered 里的仓位在第一次 tick 时检查是否停机期间已到期
            loaded[pos.symbol] = pos
        trades = self._read("恢复最近平仓记录", self.store.paper_trades, MAX_HISTORY) or []
        agg = self._read("恢复累计统计", self.store.paper_trade_stats, 0)
        with self._lock:
            self.positions.update(loaded)
            self._recovered.update(loaded)
            if trades:
                # 存储层按 closed_ts 倒序返回，内存里保持时间升序
                self.history = list(reversed(trades))[-MAX_HISTORY:]
            # 开仓序号续号：不能从 0 重来，否则重启后同毫秒重开又会撞 trade_key。
            # 未平仓仓位的 open_seq 是主口径；历史流水里的 openSeq 作为补充
            # （全部仓位都已平掉时也能续号；老数据没有该键则按 0 跳过）。
            restored_seq = 0
            for pos in loaded.values():
                restored_seq = max(restored_seq, int(_num(pos.open_seq) or 0))
            for record in self.history:
                if isinstance(record, dict):
                    restored_seq = max(restored_seq, int(_num(record.get("openSeq")) or 0))
            self._open_seq = max(self._open_seq, restored_seq)
            if isinstance(agg, dict):
                closed = int(agg.get("count") or 0)
                self.stats = {
                    # opened 无法从"已平仓"精确反推：用累计平仓数 + 当前未平仓数近似；
                    # 引擎每开一仓都会写 paper_positions，这个口径够看趋势。
                    "opened": closed + len(self.positions),
                    "closed": closed,
                    "wins": int(agg.get("wins") or 0),
                    "losses": int(agg.get("losses") or 0),
                    "timeouts": int(agg.get("timeouts") or 0),
                    "netPnl": round(float(agg.get("pnl") or 0), 4),
                }
            summary = {"positions": len(loaded), "trades": len(self.history),
                       "closed": self.stats["closed"]}
        # 读不到未平仓行时不要当成"空账本"继续写库，否则会把旧行覆盖掉；
        # 本进程按纯内存模式跑（summary 也会标 source=memory），重启后再恢复。
        self._store_suspended = not positions_ok
        return summary

    def _read(self, action: str, fn, *args):
        """读库容错：读失败按"空"处理，但要留下可见日志。"""
        try:
            return fn(*args)
        except Exception as exc:       # 数据库/网络异常都不该打断推演
            self._warn(action, exc)
            return None

    def _warn(self, action: str, exc: Exception) -> None:
        """落库失败只报一次同样的错，避免每 5 秒刷满日志。"""
        message = f"[paper] {action}失败: {type(exc).__name__}: {exc}"
        if message != self._storage_error:
            self._storage_error = message
            print(message)

    def _saved(self) -> None:
        self._storage_error = ""

    def _store_usable(self) -> bool:
        return self.store is not None and not self._store_suspended

    def _flush_pending(self) -> None:
        """重试上次写失败的平仓流水/删除动作（每轮 tick 都会调，通常为空）。"""
        if not self._store_usable():
            return
        for record in list(self._pending_trades):
            if not self._save_trade(record):
                continue
            self._pending_trades.remove(record)
            symbol = str(record.get("symbol") or "")
            if not self._forget(symbol):
                self._pending_deletes.add(symbol)
        for symbol in list(self._pending_deletes):
            if symbol in self.positions:
                # 新仓的 REPLACE 已经把旧行覆盖掉了，不需要再删
                self._pending_deletes.discard(symbol)
                continue
            if self._forget(symbol):
                self._pending_deletes.discard(symbol)

    def _pending_for(self, symbol: str) -> bool:
        return any(str(r.get("symbol") or "") == symbol
                   for r in self._pending_trades)

    def _persist(self, pos: PaperPosition) -> bool:
        """未平仓仓位 UPSERT 到 paper_positions（开仓/每轮更新都调）。

        有同币的平仓流水还没写进库时先不写仓位：此时 REPLACE 会把那笔
        平仓对应的旧行覆盖掉，一旦进程随后崩溃，成交和水位就没了。
        """
        if not self._store_usable():
            return False
        self._flush_pending()
        if self._pending_for(pos.symbol):
            return False
        try:
            self.store.save_paper_position(pos.to_dict())
            self._pending_deletes.discard(pos.symbol)
            self._saved()
            return True
        except Exception as exc:
            self._warn("模拟仓仓位落库", exc)
            return False

    def _forget(self, symbol: str) -> bool:
        """平仓后删掉 paper_positions 里那一行。"""
        if not self._store_usable():
            return False
        try:
            self.store.delete_paper_position(symbol)
            self._saved()
            return True
        except Exception as exc:
            self._warn("模拟仓仓位删除", exc)
            return False

    def _save_trade(self, record: dict) -> bool:
        """平仓记录 INSERT 到 paper_trades（只追加，不依赖内存窗口）。"""
        if not self._store_usable():
            return False
        try:
            self.store.save_paper_trade(record)
            self._saved()
            return True
        except Exception as exc:
            self._warn("模拟仓平仓记录落库", exc)
            return False

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

        接了数据库（store 非空）时：session 与 bySymbol 都来自 paper_trades
        全量聚合，服务重启不清零、也不受内存 200 条缓存限制——这是做策略
        分析唯一可信的口径。没接数据库的降级模式才退回内存口径，并明确标注。

        两个口径都保留同样的键名（closed/wins/losses/timeouts/winrate/netPnl），
        Agent 的 read_paper 不管底层是哪种都不会读崩。
        """
        with self._lock:
            rows = list(self.history)
            open_count = len(self.positions)
            stats = dict(self.stats)
        db_stats = db_symbols = None
        if self.store is not None and not self._store_suspended:
            # 先把挂起的流水补写掉，避免"内存已平仓、DB 统计还是旧值"的短暂不一致。
            # 必须和 tick 共用同一把锁：summary 在事件循环线程、tick 在线程池，
            # 两个线程同时 _flush_pending 会对同一个 _pending_trades 做 remove。
            with self._lock:
                self._flush_pending()
            db_stats = self._read("读取模拟仓统计", self.store.paper_trade_stats, 0)
            db_symbols = self._read("读取模拟仓分币统计",
                                    self.store.paper_symbol_stats, 0)
        if isinstance(db_stats, dict):
            return self._db_summary(db_stats, db_symbols or [], open_count, len(rows))
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
            # 分母用"有明确输赢的笔数"：平局和超时不算赢也不算输，
            # 放进分母会把胜率压成 0.x% 这种看着像"几乎全输"的数字。
            decided = item["wins"] + item["losses"]
            item["decided"] = decided
            item["winrate"] = (round(item["wins"] / decided * 100, 1)
                               if decided else None)
            item["avgPnl"] = (round(item["netPnl"] / item["closed"], 4)
                              if item["closed"] else None)
            ranked.append(item)
        # 胜率高的在前；胜率相同比样本量。没有平仓记录的币不上榜。
        ranked.sort(key=lambda x: (-(x["winrate"] or 0.0), -x["closed"]))
        return {
            "note": "模拟仓=15 分钟纸上交易，不产生真实委托，与账户实盘持仓无关。"
                    "当前数据库不可用（纯内存降级模式）：session 是进程启动以来的"
                    f"累计值，bySymbol 只覆盖内存里最近 {MAX_HISTORY} 条平仓记录，"
                    "重启会清零——数据库恢复后这些数据会自动重新进库。",
            "source": "memory",
            "openCount": open_count,
            "session": {
                "closed": stats["closed"], "wins": stats["wins"],
                "losses": stats["losses"], "timeouts": stats["timeouts"],
                # 与 bySymbol 同一口径：分母只算有明确输赢的笔数
                "decided": stats["wins"] + stats["losses"],
                "winrate": (round(stats["wins"] / (stats["wins"] + stats["losses"]) * 100, 1)
                            if (stats["wins"] + stats["losses"]) else None),
                "netPnl": round(stats["netPnl"], 4),
            },
            # 内存降级模式保持旧契约（Agent/测试都按这两个键读）
            "window": {"records": len(rows), "limit": MAX_HISTORY},
            "bySymbol": ranked,
        }

    def _db_summary(self, agg: dict, symbols: list, open_count: int,
                    cached: int) -> dict:
        """数据库口径的汇总：全量聚合，按胜率排序。"""
        closed = int(agg.get("count") or 0)
        wins = int(agg.get("wins") or 0)
        ranked = []
        for item in symbols:
            if not isinstance(item, dict):
                continue
            sym = str(item.get("symbol") or "")
            if not sym:
                continue
            n = int(item.get("closed") or 0)
            w = int(item.get("wins") or 0)
            l = int(item.get("losses") or 0)
            pnl = round(float(item.get("netPnl") or 0), 4)
            # 胜率的分母用"有明确输赢的笔数"，不是全部分母。
            # 平局（pnl 恰好为 0）和超时都不进 wins/losses，把它们算进分母
            # 会把胜率压成 0.2%、0.6% 这种数字——线上 ada 936 笔里只有 3 笔
            # 有输赢，显示 0.2% 看着像"几乎全输"，其实是 933 笔平价离场。
            decided = w + l
            ranked.append({
                "symbol": sym,
                "closed": n,
                "wins": w,
                "losses": l,
                "decided": decided,
                "timeouts": int(item.get("timeouts") or 0),
                "netPnl": pnl,
                "winrate": round(w / decided * 100, 1) if decided else None,
                "avgPnl": round(pnl / n, 4) if n else None,
            })
        # 胜率高的在前；胜率相同比样本量。没有平仓记录的币不上榜。
        ranked.sort(key=lambda x: (-(x["winrate"] or 0.0), -x["closed"]))
        return {
            "note": "模拟仓=15 分钟纸上交易，不产生真实委托，与账户实盘持仓无关。"
                    "全部已平仓记录在 MySQL paper_trades 表：session 与 bySymbol "
                    "都是数据库全量累计口径，服务重启不会清零，可直接用 SQL 复盘。",
            "source": "mysql",
            "openCount": open_count,
            "session": {
                "closed": closed, "wins": wins,
                "losses": int(agg.get("losses") or 0),
                "timeouts": int(agg.get("timeouts") or 0),
                "winrate": (round(wins / closed * 100, 1) if closed else None),
                "netPnl": round(float(agg.get("pnl") or 0), 4),
            },
            # window 现在只是"内存里缓存了多少条最近记录"，统计口径看 session/bySymbol
            "window": {"records": cached, "limit": MAX_HISTORY,
                       "source": "memory-cache"},
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
        可选字段 bars：该币最近 K 线（list[Candle]），只在"从库恢复的仓位
        在停机期间已到期"时用来补算真实离场价；没有就退回现价。
        返回当前持仓的 dict（或 None）。
        """
        price = _num(ctx.get("price"))
        if not price or price <= 0:
            pos = self.positions.get(symbol)
            return pos.to_dict() if pos else None

        with self._lock:
            self._flush_pending()

            pos = self.positions.get(symbol)
            if pos is None:
                self._open(symbol, ctx, price)
                pos = self.positions.get(symbol)
                return pos.to_dict() if pos else None

            # 从数据库恢复的仓位：先看停机期间是否已到期。到期就用那段时间的
            # K 线判断先碰止盈还是止损，按真实离场价记账；还没到期就正常推进。
            if symbol in self._recovered:
                self._recovered.discard(symbol)
                settled = self._settle_recovered(pos, price, ctx.get("bars") or [])
                if settled is not None:
                    self._close(pos, settled[0], settled[1], ctx, recovered=True,
                                settle_bars=settled[2], settle_approx=settled[3])
                    # 补结算后立刻按当前数据重开，仓位永不停
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
                # 浮盈/peak/移动止损都可能变了；每轮覆盖进库，重启后接着走
                self._persist(pos)
            pos = self.positions.get(symbol)
            return pos.to_dict() if pos else None

    def drop_missing(self, keep: set[str]) -> None:
        """币种被移出监控时清掉它的模拟仓（不记录，因为不是正常平仓）。"""
        removed = []
        with self._lock:
            for sym in list(self.positions):
                if sym not in keep:
                    self.positions.pop(sym, None)
                    self._recovered.discard(sym)
                    removed.append(sym)
        for sym in removed:
            self._forget(sym)

    def close_missing(self, keep: set[str], ctx: dict | None = None) -> list[str]:
        """取消监控收尾（P2-5）：处理所有不在 keep 里的模拟仓，返回处理的币种。

        与 drop_missing 的区别：这里**要留下可复盘的数据**——
          · mark>0：按最后一个已知 mark 平仓，记一条 reason="removed" 的正常
            流水（统计口径与其他平仓一致），并删掉未平仓行；
          · mark<=0（还没有任何可用价格）：只删 paper_positions 行、不记流水，
            绝不能为"连价格都没有的仓位"编一个成交价；
          · keep 全包含时返回 []。
        ctx 参数留给平仓流水补充上下文字段（_close 目前不读）。
        """
        done: list[str] = []
        with self._lock:
            self._flush_pending()
            for symbol in list(self.positions):
                if symbol in keep:
                    continue
                pos = self.positions.get(symbol)
                if pos is None:
                    continue
                mark = _num(pos.mark)
                self._recovered.discard(symbol)
                if mark is not None and mark > 0:
                    self._close(pos, mark, "removed", ctx or {})
                else:
                    self.positions.pop(symbol, None)
                    # 只删行不记流水；删除失败挂起重试，不能留下永远清不掉的残行
                    if self._store_usable() and not self._forget(symbol):
                        self._pending_deletes.add(symbol)
                done.append(symbol)
        return done

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
        # 先自增再写入：同一毫秒内的每一仓都有唯一 open_seq
        self._open_seq += 1
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
            pattern=str(ctx.get("pattern") or "")[:40],
            regime=str(ctx.get("regime") or "")[:16],
            mark=entry,
            peak=entry,
            open_seq=self._open_seq,
        )
        self.positions[symbol] = pos
        self.stats["opened"] += 1
        # 开仓即落库：进程重启后这一仓还在，不会"凭空消失"
        self._persist(pos)

    def _settle_recovered(self, pos: PaperPosition, price: float,
                          bars: list) -> tuple[float, str, int, bool] | None:
        """补结算"停机期间已经到期"的恢复仓位。

        重启后不能拿现价当 15 分钟窗口到期价——停机期间价格可能已经先碰过
        止盈/止损。这里按 entry_ts ~ expires_ts 之间的 K 线逐根判断：
        先碰止损记 sl、先碰止盈记 tp，都没碰则用到期前最后一根收盘价记 timeout。
        一根K线内高低都碰到时保守判止损（与胜率评估口径一致）。

        返回 (离场价, 原因, bars_used, approx)：
          · bars_used：实际参与判断的窗口内K线根数（没有可用 bars 为 0）；
          · approx：离场价是否为近似值——无可用 bars / 用现价回落 → True；
            bars 没有覆盖到 expires_ts（窗口尾部缺失）→ True；
            命中 tp/sl → False（有明确触发价）。
        仓位还没到期返回 None。

        近似说明：移动止损在停机期间的历史位置没有逐笔记录，这里用恢复出来的
        最终 sl 判断，属于偏保守的近似（不会高估盈利）。
        """
        now = int(time.time() * 1000)
        if now < pos.expires_ts:
            return None
        long_ = pos.direction == "long"
        last_close = None
        bars_used = 0
        latest_ts = 0
        for bar in bars or []:
            try:
                bar_ts = int(getattr(bar, "open_time", 0) or 0)
                high = float(getattr(bar, "high", None))
                low = float(getattr(bar, "low", None))
            except (AttributeError, TypeError, ValueError):
                continue
            latest_ts = max(latest_ts, bar_ts)
            if bar_ts <= pos.entry_ts or bar_ts > pos.expires_ts:
                continue
            bars_used += 1
            if long_:
                if low <= pos.sl:
                    return (pos.sl, "sl", bars_used, False)
                if high >= pos.tp:
                    return (pos.tp, "tp", bars_used, False)
            else:
                if high >= pos.sl:
                    return (pos.sl, "sl", bars_used, False)
                if low <= pos.tp:
                    return (pos.tp, "tp", bars_used, False)
            last_close = _num(getattr(bar, "close", None))
        # 覆盖到 expires_ts：窗口尾部有K线可看，timeout 价不算近似
        covered = bool(bars_used) and latest_ts >= pos.expires_ts
        if last_close is None:
            # 没有可用K线：只能按现价平，原因仍记 timeout，但 record.recovered=True
            return (price, "timeout", bars_used, True)
        return (last_close, "timeout", bars_used, not covered)

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
        2. **先触发再移（arming），且触发距离 = 回撤距离。**
           价格必须朝有利方向走过 TRAIL_ATR 个 ATR，才允许移动止损；
           没走出来就保持原止损不动。触发距离与回撤距离取同一个值，
           武装的那一刻止损正好落在成本线上——不会出现
            "刚走 1 个 ATR 就把止损收紧到成本下方 0.4 ATR" 那种
           一有回抽就出局的窄口袋。

        移动后仍保证止损在现价的正确一侧（多头的止损必须低于现价）。
        """
        atr = _num(pos.atr_at_entry) or _num(ctx.get("atr"))
        if not atr or atr <= 0:
            return
        long_ = pos.direction == "long"
        arm = atr * TRAIL_ATR
        # 未武装：价格还没走出来，不动止损
        if long_:
            if pos.peak < pos.entry + arm:
                return
            candidate = pos.peak - arm
            if candidate > pos.sl and candidate < pos.mark:
                pos.sl = _round(candidate, 4)
                pos.trailing = True
                pos.sl_pct = round((pos.entry - pos.sl) / pos.entry * 100, 2)
        else:
            if pos.peak > pos.entry - arm:
                return
            candidate = pos.peak + arm
            if candidate < pos.sl and candidate > pos.mark:
                pos.sl = _round(candidate, 4)
                pos.trailing = True
                pos.sl_pct = round((pos.sl - pos.entry) / pos.entry * 100, 2)

    def _close(self, pos: PaperPosition, price: float, reason: str, ctx: dict,
               recovered: bool = False, settle_bars: int = 0,
               settle_approx: bool = False) -> None:
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
            "entryTs": pos.entry_ts,
            "gap": pos.gap,
            "pattern": pos.pattern,
            "regime": pos.regime,
            "atrAtEntry": pos.atr_at_entry,
            "winrateParts": pos.winrate_parts or {},
            "heldMs": max(0, int(time.time() * 1000) - pos.entry_ts),
            "closedAt": int(time.time() * 1000),
            "trailing": pos.trailing,
            # 重启补结算的仓位要标出来：它不是实时盯盘盯出来的离场价
            "recovered": bool(recovered),
            # 开仓单调序号：与 entryTs 一起构成 trade_key 的稳定去重键
            "openSeq": int(_num(pos.open_seq) or 0),
            # 补结算质量：参与判断的K线根数 / 离场价是否近似（实时平仓为 0/False）
            "settleBars": max(0, int(_num(settle_bars) or 0)),
            "settleApprox": bool(settle_approx),
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
        # 先记平仓流水、再删未平仓行：中途崩了最多多一条仓，不会丢一笔成交。
        # 流水写失败时**保留未平仓行并挂起重试**：宁可重启后按旧仓再结算一次，
        # 也不能让这笔成交凭空消失（旧实现吞异常后照删，等于永久丢单）。
        if self._save_trade(record):
            if not self._forget(pos.symbol):
                self._pending_deletes.add(pos.symbol)
        elif self._store_usable():
            # 库可用但这次写失败：挂起重试（内存账本继续跑）
            self._pending_trades.append(record)
