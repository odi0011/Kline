"""模拟仓 MySQL 持久化：开仓落库、重启恢复、停机期间到期补结算、DB 口径汇总。

这一组测试用 FakeStore 模拟 storage.Storage 的 7 个接口（鸭子类型），
不依赖真实 MySQL；真实的 SQL 适配由 tests/test_paper_store.py 覆盖。
核心断言：
  · 开仓/每轮更新会写 paper_positions；
  · 平仓先写 paper_trades 再删未平仓行；
  · 重启后仓位、历史、统计都能恢复（不再"重启清零"）；
  · 停机期间到期的仓位用期间的K线补算止盈/止损，不能拿重启现价硬平；
  · 数据库挂掉只是降级为内存账本，绝不打断 5 秒一轮的推演。
"""
import time

from decision.paper import MAX_HISTORY, PaperBook


class Bar:
    """最小K线：只带补结算要用的字段。"""

    def __init__(self, open_time, high, low, close):
        self.open_time = int(open_time)
        self.high = float(high)
        self.low = float(low)
        self.close = float(close)


class FakeStore:
    """按 storage.Storage 的模拟仓接口实现的进程内替身。"""

    def __init__(self):
        self.open = {}        # symbol -> 仓位 dict
        self.trades = []      # 平仓记录（按时间追加）
        self.fail = False
        # 按动作注入故障：验证"某类 DB 写失败"时账本不能静默丢数据
        self.fail_positions_read = False
        self.fail_trade_write = False
        self.fail_delete = False

    def _maybe_fail(self):
        if self.fail:
            raise RuntimeError("模拟数据库故障")

    # ---- 与 storage.py 契约一致 ----
    def paper_positions(self):
        self._maybe_fail()
        if self.fail_positions_read:
            raise RuntimeError("模拟读取未平仓失败")
        return [dict(v) for v in self.open.values()]

    def save_paper_position(self, pos):
        self._maybe_fail()
        self.open[pos["symbol"]] = dict(pos)

    def delete_paper_position(self, symbol):
        self._maybe_fail()
        if self.fail_delete:
            raise RuntimeError("模拟删除未平仓失败")
        self.open.pop(symbol, None)

    def save_paper_trade(self, trade):
        self._maybe_fail()
        if self.fail_trade_write:
            raise RuntimeError("模拟平仓流水写失败")
        self.trades.append(dict(trade))

    def paper_trades(self, limit=200):
        self._maybe_fail()
        # 模仿 paper_trades 表：ORDER BY closed_ts DESC
        rows = sorted(self.trades, key=lambda r: int(r.get("closedAt") or 0),
                      reverse=True)
        return rows[:limit]

    def paper_trade_stats(self, days=0):
        self._maybe_fail()
        rows = self._window(days)
        wins = sum(1 for r in rows if (r.get("pnl") or 0) > 0)
        losses = sum(1 for r in rows if (r.get("pnl") or 0) < 0)
        timeouts = sum(1 for r in rows if r.get("reason") == "timeout")
        pnl = round(sum(float(r.get("pnl") or 0) for r in rows), 4)
        return {"count": len(rows), "wins": wins, "losses": losses,
                "timeouts": timeouts, "pnl": pnl}

    def paper_symbol_stats(self, days=0):
        self._maybe_fail()
        out = {}
        for r in self._window(days):
            sym = str(r.get("symbol") or "")
            if not sym:
                continue
            item = out.setdefault(sym, {"symbol": sym, "closed": 0, "wins": 0,
                                        "losses": 0, "timeouts": 0, "netPnl": 0.0})
            pnl = float(r.get("pnl") or 0)
            item["closed"] += 1
            item["netPnl"] = round(item["netPnl"] + pnl, 4)
            if pnl > 0:
                item["wins"] += 1
            elif pnl < 0:
                item["losses"] += 1
            if r.get("reason") == "timeout":
                item["timeouts"] += 1
        return list(out.values())

    def _window(self, days):
        if not days:
            return list(self.trades)
        cutoff = int(time.time() * 1000) - int(days) * 86400_000
        return [r for r in self.trades if int(r.get("closedAt") or 0) >= cutoff]


def ctx(price=100.0, atr=2.0, direction="bullish", score=70, gap=20,
        max_lev=20, pattern="hammer", regime="trend", bars=None):
    data = {"price": price, "atr": atr, "score": score, "gap": gap,
            "grade": "medium", "direction": direction, "reason": "测试",
            "regime": regime, "pattern": pattern,
            "patternStats": {}, "maxLeverage": max_lev}
    if bars is not None:
        data["bars"] = bars
    return data


def trade(symbol="btcusdt", pnl=1.5, reason="tp", closed_at=None):
    return {"symbol": symbol, "direction": "long", "entry": 100.0, "exit": 101.0,
            "sl": 98.0, "tp": 102.0, "leverage": 20, "margin": 50.0, "qty": 10.0,
            "pnl": pnl, "pnlPct": 3.0, "reason": reason, "score": 70.0,
            "gap": 20.0, "winrate": 55.0, "grade": "medium", "pattern": "hammer",
            "regime": "trend", "atrAtEntry": 2.0, "heldMs": 900000,
            "closedAt": closed_at or int(time.time() * 1000),
            "entryTs": (closed_at or int(time.time() * 1000)) - 900000,
            "trailing": False, "winrateParts": {"final": 0.55}}


# ---------- 开仓落库 ----------

def test_open_writes_position_to_store():
    store = FakeStore()
    book = PaperBook(store=store)
    pos = book.tick("btcusdt", ctx())
    assert pos is not None
    saved = store.open["btcusdt"]
    assert saved["entry"] == pos["entry"]
    assert saved["expires_ts"] == pos["expires_ts"]
    assert saved["pattern"] == "hammer" and saved["regime"] == "trend"


def test_each_tick_updates_persisted_mark_and_trailing():
    store = FakeStore()
    book = PaperBook(store=store)
    book.tick("btcusdt", ctx(price=100.0, atr=1.0))
    book.tick("btcusdt", ctx(price=101.5, atr=1.0))   # 武装移动止损
    saved = store.open["btcusdt"]
    assert saved["mark"] == 101.5
    assert saved["ticks"] == 1
    assert saved["trailing"] is True
    assert saved["sl"] > 100.0


# ---------- 平仓落库 ----------

def test_close_appends_trade_and_reopens():
    store = FakeStore()
    book = PaperBook(store=store)
    first = book.tick("btcusdt", ctx(price=100.0))
    book.tick("btcusdt", ctx(price=first["tp"] + 1))
    assert len(store.trades) == 1
    rec = store.trades[0]
    assert rec["reason"] == "tp"
    assert rec["symbol"] == "btcusdt"
    assert rec["pattern"] == "hammer" and rec["regime"] == "trend"
    assert rec["gap"] == 20.0
    assert rec["winrateParts"]
    assert rec["recovered"] is False
    # 平完立刻重开：库里同一币只剩新仓，不是旧的
    assert store.open["btcusdt"]["entry"] == first["tp"] + 1
    assert store.open["btcusdt"]["entry_ts"] >= rec["entryTs"]


def test_force_closes_and_reopens_in_store():
    store = FakeStore()
    book = PaperBook(store=store)
    book.tick("btcusdt", ctx(price=100.0))
    book.force("btcusdt", ctx(price=105.0))
    assert store.trades[-1]["reason"] == "manual"
    assert store.open["btcusdt"]["entry"] == 105.0


def test_drop_missing_removes_position_row():
    store = FakeStore()
    book = PaperBook(store=store)
    book.tick("btcusdt", ctx())
    book.tick("ethusdt", ctx())
    book.drop_missing({"btcusdt"})
    assert "ethusdt" not in store.open
    assert "btcusdt" in store.open


# ---------- 重启恢复 ----------

def test_restart_restores_open_position_and_history():
    store = FakeStore()
    store.trades = [trade(pnl=1.0, closed_at=int(time.time() * 1000) - 2000),
                    trade(pnl=-0.5, reason="sl", closed_at=int(time.time() * 1000) - 1000)]
    book = PaperBook(store=store)
    book.tick("btcusdt", ctx(price=100.0, atr=1.0))
    entry = book.positions["btcusdt"].entry

    restarted = PaperBook(store=store)     # 等同进程重启
    assert restarted.restore_info["positions"] == 1
    assert restarted.restore_info["closed"] == 2
    pos = restarted.positions["btcusdt"]
    assert pos.entry == entry
    assert restarted.history[-1]["pnl"] == -0.5
    assert restarted.stats["closed"] == 2 and restarted.stats["wins"] == 1
    # 恢复的仓位正常推进：没到期不该被平掉
    again = restarted.tick("btcusdt", ctx(price=100.2, atr=1.0))
    assert again is not None and again["entry"] == entry
    assert restarted.stats["closed"] == 2


def test_restored_position_expired_uses_bars_to_settle_tp():
    """停机期间已经到期：按期间的K线补算，先碰止盈就记 tp，而不是拿现价硬平。"""
    store = FakeStore()
    book = PaperBook(store=store)
    pos = book.tick("btcusdt", ctx(price=100.0, atr=1.0))
    saved = store.open["btcusdt"]
    # 模拟 20 分钟前开仓、5 分钟前到期的仓位（停机期间走完了整个窗口）
    now = int(time.time() * 1000)
    saved["entry_ts"] = now - 20 * 60_000
    saved["expires_ts"] = now - 5 * 60_000

    restarted = PaperBook(store=store)
    bars = [Bar(saved["entry_ts"] + 1000, high=pos["tp"] + 1, low=100.0, close=100.5),
            Bar(saved["entry_ts"] + 2000, high=101.0, low=99.0, close=100.0)]
    out = restarted.tick("btcusdt", ctx(price=100.5, atr=1.0, bars=bars))
    assert restarted.history[-1]["reason"] == "tp"
    assert restarted.history[-1]["exit"] == pos["tp"]
    assert restarted.history[-1]["recovered"] is True
    # 补结算后立刻按现价重开
    assert out is not None and out["entry"] == 100.5
    assert store.open["btcusdt"]["entry"] == 100.5


def test_restored_position_expired_settles_sl_first():
    store = FakeStore()
    book = PaperBook(store=store)
    pos = book.tick("btcusdt", ctx(price=100.0, atr=1.0))
    saved = store.open["btcusdt"]
    now = int(time.time() * 1000)
    saved["entry_ts"] = now - 20 * 60_000
    saved["expires_ts"] = now - 5 * 60_000
    restarted = PaperBook(store=store)
    # 同一根K线里高低都碰到：保守判止损
    bars = [Bar(saved["entry_ts"] + 1000, high=pos["tp"] + 1, low=pos["sl"] - 1, close=100.0)]
    restarted.tick("btcusdt", ctx(price=100.0, atr=1.0, bars=bars))
    assert restarted.history[-1]["reason"] == "sl"
    assert restarted.history[-1]["exit"] == pos["sl"]


def test_restored_expired_without_bars_falls_back_to_price():
    store = FakeStore()
    book = PaperBook(store=store)
    book.tick("btcusdt", ctx(price=100.0, atr=1.0))
    now = int(time.time() * 1000)
    store.open["btcusdt"]["entry_ts"] = now - 20 * 60_000
    store.open["btcusdt"]["expires_ts"] = now - 5 * 60_000
    restarted = PaperBook(store=store)
    restarted.tick("btcusdt", ctx(price=100.3, atr=1.0))
    assert restarted.history[-1]["reason"] == "timeout"
    assert restarted.history[-1]["exit"] == 100.3


def test_history_restored_is_capped_to_memory_window():
    store = FakeStore()
    now = int(time.time() * 1000)
    store.trades = [trade(pnl=0.1, closed_at=now - (MAX_HISTORY + i) * 1000)
                    for i in range(MAX_HISTORY + 5)]
    book = PaperBook(store=store)
    assert len(book.history) == MAX_HISTORY
    assert book.history[-1]["closedAt"] > book.history[0]["closedAt"]


# ---------- 汇总口径 ----------

def test_summary_uses_database_aggregate():
    store = FakeStore()
    store.trades = [
        trade("btcusdt", pnl=2.0, closed_at=int(time.time() * 1000)),
        trade("btcusdt", pnl=-1.0, reason="sl", closed_at=int(time.time() * 1000)),
        trade("ethusdt", pnl=3.0, closed_at=int(time.time() * 1000)),
    ]
    book = PaperBook(store=store)
    summary = book.summary()
    assert summary["source"] == "mysql"
    assert summary["session"]["closed"] == 3
    assert summary["session"]["wins"] == 2
    assert summary["session"]["netPnl"] == 4.0
    top = summary["bySymbol"][0]
    assert top["symbol"] == "ethusdt" and top["winrate"] == 100.0
    assert "重启不会清零" in summary["note"]


def test_summary_falls_back_to_memory_when_store_fails():
    store = FakeStore()
    book = PaperBook(store=store)
    book.tick("btcusdt", ctx(price=100.0))
    store.fail = True
    summary = book.summary()
    assert summary["source"] == "memory"
    assert summary["openCount"] == 1


# ---------- 容错 ----------

def test_broken_store_never_breaks_tick():
    store = FakeStore()
    store.fail = True
    book = PaperBook(store=store)          # 恢复读取失败也只打日志
    pos = book.tick("btcusdt", ctx())
    assert pos is not None and pos["symbol"] == "btcusdt"
    assert "btcusdt" in book.positions
    # 平仓写库失败也不能抛给引擎的 5 秒循环
    tp = pos["tp"]
    book.tick("btcusdt", ctx(price=tp + 1))
    assert book.history[-1]["reason"] == "tp"


def test_corrupt_position_rows_are_skipped():
    store = FakeStore()
    store.open = {"bad": {"symbol": "bad"},                 # 缺必填字段
                  "good": {"symbol": "good"}}               # 也缺字段
    book = PaperBook(store=store)
    assert book.positions == {}
    assert book.restore_info["positions"] == 0



# ---------- 落库失败时的数据可靠性（P1） ----------

def test_failed_trade_write_keeps_old_row_and_retries():
    """流水写失败时不能删旧行，也不能让新仓覆盖它；恢复后重试补写。"""
    store = FakeStore()
    book = PaperBook(store=store)
    opened = book.tick("btcusdt", ctx(price=100.0, atr=1.0))
    old_entry = opened["entry"]
    store.fail_trade_write = True
    book.tick("btcusdt", ctx(price=opened["tp"] + 1, atr=1.0))
    # 内存里已经平仓重开，但库里的旧仓位行必须还在、不能被新仓 REPLACE 掉
    assert book.history[-1]["reason"] == "tp"
    assert store.trades == []
    assert store.open["btcusdt"]["entry"] == old_entry

    store.fail_trade_write = False
    new_entry = book.positions["btcusdt"].entry
    book.tick("btcusdt", ctx(price=new_entry + 0.05, atr=1.0))
    assert len(store.trades) == 1 and store.trades[0]["reason"] == "tp"
    assert store.open["btcusdt"]["entry"] == new_entry


def test_failed_delete_does_not_duplicate_trade_after_restart():
    store = FakeStore()
    book = PaperBook(store=store)
    opened = book.tick("btcusdt", ctx(price=100.0, atr=1.0))
    store.fail_delete = True
    book.tick("btcusdt", ctx(price=opened["tp"] + 1, atr=1.0))
    assert len(store.trades) == 1
    # 重开的新仓 REPLACE 掉旧行；删除失败不会留下能被再次结算的旧仓位
    assert store.open["btcusdt"]["entry"] == opened["tp"] + 1
    restarted = PaperBook(store=store)
    assert restarted.positions["btcusdt"].entry == opened["tp"] + 1
    assert len(store.trades) == 1


def test_recovered_flag_only_marks_outage_settlement():
    """recovered=True 只标"重启补结算"，正常实时 tick 平掉的不许乱标。"""
    store = FakeStore()
    book = PaperBook(store=store)
    pos = book.tick("btcusdt", ctx(price=100.0, atr=1.0))
    restarted = PaperBook(store=store)      # 仓位还没到期
    restarted.tick("btcusdt", ctx(price=pos["tp"] + 1, atr=1.0))
    assert restarted.history[-1]["reason"] == "tp"
    assert restarted.history[-1]["recovered"] is False


def test_corrupt_position_field_types_are_skipped():
    """类型坏掉的行要跳过，不能让 tick 抛异常导致该币模拟仓永久停摆。"""
    store = FakeStore()
    common = {"entry_ts": 1, "expires_ts": 2, "leverage": 20, "notional": 1000.0,
              "margin": 50.0, "qty": 10.0, "liq": 95.0, "sl": 98.0, "tp": 102.0}
    store.open = {
        "badnum": {"symbol": "badnum", "direction": "long", "entry": "oops", **common},
        "baddir": {"symbol": "baddir", "direction": "sideways", "entry": 100.0, **common},
        # 运行中字段被改坏（peak 字符串）也会让 tick 抛 TypeError 被静默吞掉
        "badpeak": {"symbol": "badpeak", "direction": "long", "entry": 100.0,
                    "peak": "oops", **common},
    }
    book = PaperBook(store=store)
    assert book.positions == {}


def test_disabled_store_is_treated_as_pure_memory():
    """Storage.enabled=False 时必须等同于没接库，不能误报 source=mysql。"""
    class DisabledStore(FakeStore):
        enabled = False

    book = PaperBook(store=DisabledStore())
    assert book.store is None
    pos = book.tick("btcusdt", ctx())
    assert pos is not None
    assert book.summary()["source"] == "memory"


def test_restore_read_failure_runs_memory_only_without_overwrite():
    """启动读不到未平仓行时不能当成空账本写库——本进程退化为纯内存。"""
    store = FakeStore()
    book = PaperBook(store=store)
    opened = book.tick("btcusdt", ctx(price=100.0, atr=1.0))
    entry = opened["entry"]
    store.fail_positions_read = True
    restarted = PaperBook(store=store)
    assert restarted.positions == {}
    assert restarted._store_suspended is True
    # 读失败后照常推演（不能因为数据库抖了就停摆），但不写库、不覆盖旧行
    pos = restarted.tick("btcusdt", ctx(price=entry + 0.05, atr=1.0))
    assert pos is not None and pos["entry"] > 0
    assert store.open["btcusdt"]["entry"] == entry
    assert store.trades == []
    assert restarted.summary()["source"] == "memory"
    # 平仓也不写库（本进程纯内存），旧行原样保留给下次重启恢复
    restarted.tick("btcusdt", ctx(price=pos["tp"] + 1, atr=1.0))
    assert restarted.history[-1]["reason"] == "tp"
    assert store.open["btcusdt"]["entry"] == entry
    assert store.trades == []



# ---------- 第 3 轮复验修复：R1b / R3b ----------

def test_numeric_string_fields_are_coerced_on_restore():
    """JSON 里存成字符串的数值（"1.5"）要强转成 float，否则 tick 会 TypeError。"""
    store = FakeStore()
    book = PaperBook(store=store)
    book.tick("btcusdt", ctx(price=100.0, atr=1.0))
    saved = store.open["btcusdt"]
    saved["peak"] = "101.2"
    saved["mark"] = "100.5"
    saved["entry_ts"] = str(saved["entry_ts"])
    restarted = PaperBook(store=store)
    pos = restarted.positions["btcusdt"]
    assert isinstance(pos.peak, float) and pos.peak == 101.2
    assert isinstance(pos.mark, float) and pos.mark == 100.5
    assert isinstance(pos.entry_ts, int)
    # 能正常推进（不因 "101.2" 这种字符串抛 TypeError）
    assert restarted.tick("btcusdt", ctx(price=101.0, atr=1.0)) is not None


def test_summary_flush_is_thread_safe():
    """summary 与 5 秒 tick 并发补写 pending 时不能对同一列表 remove 两次。"""
    import threading
    import time as _time

    store = FakeStore()
    book = PaperBook(store=store)
    pos = book.tick("btcusdt", ctx(price=100.0, atr=1.0))
    store.fail_trade_write = True
    book.tick("btcusdt", ctx(price=pos["tp"] + 1, atr=1.0))   # 挂起一条流水
    store.fail_trade_write = False
    real_save = store.save_paper_trade

    def slow_save(trade):
        _time.sleep(0.02)          # 拉大两个线程同时 flush 的窗口
        real_save(trade)

    store.save_paper_trade = slow_save
    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def run(fn):
        try:
            barrier.wait(timeout=3)
            fn()
        except Exception as exc:       # noqa: BLE001 - 测试就是要抓所有异常
            errors.append(exc)

    entry = book.positions["btcusdt"].entry
    first = threading.Thread(target=run, args=(book.summary,))
    second = threading.Thread(target=run, args=(
        lambda: book.tick("btcusdt", ctx(price=entry + 0.05, atr=1.0)),))
    first.start()
    second.start()
    first.join(5)
    second.join(5)
    assert errors == [], f"并发 flush 不应抛异常: {errors}"
    # FakeStore 不做幂等去重，只有锁能保证 pending 只被补写一次
    assert len(store.trades) == 1
