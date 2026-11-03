"""模拟仓持久化端到端验证：真实 storage.Storage（真实 SQL）+ 真实 PaperBook。

隔离方式：pymysql.connect 被 monkeypatch 到 in-memory SQLite，复用
tests/test_paper_store.py 的 MySQL→SQLite 适配器（只翻译引擎/自增/行内索引/
SHOW COLUMNS/%s 占位符）。建表、REPLACE、INSERT、聚合查询都真实执行，
不是 FakeStore 的进程内字典。

覆盖场景：
  A. 开仓 → paper_positions 一行；每轮 tick 的 mark / 移动止损 sl 写回；
     止盈平仓 → paper_trades 落行（recovered=False、pattern/regime/
     winrateParts 字段往返）→ 未平仓行删除且新仓落库。
  B. 重启（同 store 新建 PaperBook）→ 未平仓仓位、历史、统计、summary
     全部从数据库恢复，summary.source=mysql，DB 列值逐字段往返。
  C. 停机期间到期 → 按 bars 补结算 tp / sl / timeout、recovered=True 且
     真实列持久化；移动止损抬过的 sl 在补结算里生效；bars 不覆盖窗口 /
     完全没有 bars 时按现价回落。
  D. MySQL disabled（Storage({})）→ 按 main.py 的注入规则降级纯内存，
     summary.source=memory。
  E. 数据库写入异常不打断 5 秒一轮的 tick（容错）。
  F. P1 复验：平仓流水 INSERT 失败时旧 paper_positions 行必须保留、新仓不得
     REPLACE 覆盖；故障恢复后下一次 tick 自动补写且只写一条，再正常落新仓。
  G. P2-4 复验：启动恢复读 paper_positions 失败 → 本进程纯内存（不写库、不覆盖
     旧行、summary.source=memory），旧行留给下次重启恢复。
  H. P2-3 复验：删除未平仓行失败 → 新仓 REPLACE 旧行，重启后不会出现同一仓的
     第二条平仓流水。
"""
from __future__ import annotations

import json

import pytest

import decision.paper as paper_mod
import storage
from core.candle import Candle
from decision.paper import HOLD_MS, PaperBook
from storage import Storage
from tests.test_paper_store import SQLConnection

NOW = 1_800_000_000_000
MINUTE = 60_000


class FakeClock:
    """替换 storage.time 与 decision.paper.time：开仓/到期/时间戳可精确控制。"""

    def __init__(self, now_ms: int = NOW):
        self.now = int(now_ms)

    def time(self) -> float:
        return self.now / 1000.0

    def advance(self, ms: int) -> int:
        self.now += int(ms)
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(storage, "time", fake)
    monkeypatch.setattr(paper_mod, "time", fake)
    return fake


@pytest.fixture
def make_store(monkeypatch):
    """真实 Storage：pymysql.connect 指向内存 SQLite（每个 store 独立连接）。"""
    connections = []

    def create(**kwargs):
        conn = SQLConnection()
        connections.append(conn)
        # _init() 里的 _backfill_equity_daily() 会 SELECT 现存 raw 权益表，先建空表。
        conn.db.execute(
            "CREATE TABLE equity(acct TEXT,ts BIGINT,bal DOUBLE,upnl DOUBLE,"
            "PRIMARY KEY(acct,ts))")
        monkeypatch.setattr(storage.pymysql, "connect", lambda **_: conn)
        store = Storage({"user": "isolated-e2e"}, **kwargs)
        assert store.enabled, "真实 DDL 必须走通，否则这条测试没验到 SQL"
        return store

    yield create
    for conn in connections:
        conn.db.close()


def ctx(price, atr=1.0, direction="bullish", score=70.0, gap=20.0,
        pattern="hammer", regime="trend", max_lev=20, bars=None):
    """一轮 tick 的上下文；字段与 quant._paper_context 的返回一致。"""
    data = {
        "price": price, "atr": atr, "score": score, "gap": gap,
        "grade": "medium", "direction": direction, "reason": "e2e",
        "regime": regime, "pattern": pattern, "patternStats": {},
        "maxLeverage": max_lev,
    }
    if bars is not None:
        data["bars"] = bars
    return data


def sql(store, query, args=()):
    """走 Storage._qall 的真实 SQL 通道（适配器只翻译占位符）。"""
    return store._qall(query, args)


def position_json(store, symbol):
    rows = sql(store, "SELECT data FROM paper_positions WHERE symbol=%s", (symbol,))
    assert len(rows) == 1, f"{symbol} 应有且仅有一条未平仓快照"
    return json.loads(rows[0][0])


def raw_position_entry(store, symbol):
    """绕过 fail 注入直接读 SQLite：用于"数据库正在故障"期间核对库内旧行。"""
    row = store._conn.db.execute(
        "SELECT data FROM paper_positions WHERE symbol=?", (symbol,)).fetchone()
    return json.loads(row[0])["entry"] if row else None


# ---------- A. 开仓 / tick / 止盈平仓：真实 SQL 读写 ----------

def test_open_tick_close_persists_through_real_sql(make_store, clock):
    store = make_store()
    book = PaperBook(store=store)
    assert book.restore_info == {"positions": 0, "trades": 0, "closed": 0}

    opened = book.tick("btcusdt", ctx(100.0, atr=1.0))
    assert opened is not None
    # 开仓即落库：paper_positions 恰好一行，JSON 快照字段完整
    assert sql(store, "SELECT COUNT(*) FROM paper_positions") == [(1,)]
    saved = position_json(store, "btcusdt")
    assert saved["entry"] == 100.0
    assert saved["sl"] == 98.5 and saved["tp"] == 102.5
    assert saved["pattern"] == "hammer" and saved["regime"] == "trend"
    assert saved["mark"] == 100.0

    # 第二轮：浮盈 + 移动止损（peak 101.5 - 1.4*ATR=1.0）写回同一行
    book.tick("btcusdt", ctx(101.5, atr=1.0))
    assert sql(store, "SELECT COUNT(*) FROM paper_positions") == [(1,)]
    saved = position_json(store, "btcusdt")
    assert saved["mark"] == 101.5
    assert saved["ticks"] == 1
    assert saved["trailing"] is True
    assert saved["sl"] == 100.1
    assert saved["upnl"] == 15.0

    # 止盈平仓 + 立刻重开（开仓价 103 越过 tp=102.5）
    clock.advance(5_000)
    book.tick("btcusdt", ctx(103.0, atr=1.0))

    trades = store.paper_trades()
    assert len(trades) == 1
    rec = trades[0]
    assert rec["symbol"] == "btcusdt"
    assert rec["reason"] == "tp"
    assert rec["recovered"] is False            # 实时止盈，不是重启补结算
    # 平仓记录要带上平仓那一刻的移动止损状态与抬过的 sl（上一轮已武装）
    assert rec["trailing"] is True
    assert (rec["entry"], rec["exit"]) == (100.0, 103.0)
    assert (rec["sl"], rec["tp"]) == (100.1, 102.5)
    assert (rec["leverage"], rec["margin"], rec["qty"]) == (20, 50.0, 10.0)
    assert (rec["pnl"], rec["pnlPct"]) == (30.0, 60.0)
    assert (rec["pattern"], rec["regime"], rec["gap"]) == ("hammer", "trend", 20.0)
    assert rec["winrate"] == opened["winrate"]
    assert rec["winrateParts"] == opened["winrate_parts"]
    assert rec["winrateParts"]  # 胜率分量不是空 dict
    assert (rec["entryTs"], rec["closedAt"]) == (NOW, NOW + 5_000)
    assert rec["score"] == 70.0 and rec["grade"] == "medium"

    # 未平仓行已删（不是留下旧仓），新仓已落库
    assert sql(store, "SELECT COUNT(*) FROM paper_positions") == [(1,)]
    fresh = position_json(store, "btcusdt")
    assert fresh["entry"] == 103.0
    assert fresh["entry_ts"] == NOW + 5_000
    assert fresh["entry"] != saved["entry"]
    assert book.history[-1]["recovered"] is False
    assert book.stats["closed"] == 1 and book.stats["wins"] == 1


# ---------- B. 重启恢复：仓位 / 历史 / 统计 / DB 口径 summary ----------

def test_restart_restores_positions_history_stats_and_mysql_summary(make_store, clock):
    store = make_store()
    book = PaperBook(store=store)
    btc = book.tick("btcusdt", ctx(100.0, atr=1.0))
    book.tick("ethusdt", ctx(200.0, atr=2.0))
    book.tick("ethusdt", ctx(206.0, atr=2.0))     # 止盈平仓 → 206 重开
    closed = book.history[-1]
    assert closed["symbol"] == "ethusdt" and closed["reason"] == "tp"

    # 重启：同一个 Storage 连接上新建 PaperBook
    restarted = PaperBook(store=store)
    assert restarted.restore_info == {"positions": 2, "trades": 1, "closed": 1}
    assert set(restarted.positions) == {"btcusdt", "ethusdt"}
    assert restarted.positions["btcusdt"].entry == btc["entry"]
    assert restarted.positions["btcusdt"].sl == btc["sl"]
    assert restarted.positions["ethusdt"].entry == 206.0

    # 历史记录逐字段从 25 列真实列还原
    hist = restarted.history[-1]
    for key in ("symbol", "direction", "entry", "exit", "sl", "tp", "leverage",
                "margin", "qty", "pnl", "pnlPct", "reason", "score", "gap",
                "winrate", "grade", "pattern", "regime", "atrAtEntry",
                "heldMs", "entryTs", "closedAt", "trailing", "recovered"):
        assert hist[key] == closed[key], key
    assert hist["winrateParts"] == closed["winrateParts"]
    assert hist["recovered"] is False

    # 累计统计从数据库全量聚合恢复（不是进程内从 0 开始）
    assert restarted.stats["closed"] == 1
    assert restarted.stats["wins"] == 1
    assert restarted.stats["losses"] == 0
    assert restarted.stats["netPnl"] == closed["pnl"]
    assert restarted.stats["opened"] == 3          # closed + 未平仓近似口径

    summary = restarted.summary()
    assert summary["source"] == "mysql"
    assert summary["openCount"] == 2
    assert summary["session"]["closed"] == 1
    assert summary["session"]["wins"] == 1
    assert summary["session"]["winrate"] == 100.0
    assert summary["session"]["netPnl"] == closed["pnl"]
    assert summary["window"]["records"] == 1
    assert summary["window"]["source"] == "memory-cache"
    assert [row["symbol"] for row in summary["bySymbol"]] == ["ethusdt"]
    assert summary["bySymbol"][0]["closed"] == 1
    assert summary["bySymbol"][0]["winrate"] == 100.0

    # 未到期的恢复仓正常推进，不会被误补结算；tick 的 mark 写回库
    advanced = restarted.tick("btcusdt", ctx(100.5, atr=1.0))
    assert advanced is not None and advanced["entry"] == btc["entry"]
    assert restarted.stats["closed"] == 1
    assert position_json(store, "btcusdt")["mark"] == 100.5

    # P2-1 复验：恢复的仓位由实时 tick 平掉时 recovered 必须是 False（只有停机
    # 期间到期补结算才允许 True）
    restarted.tick("btcusdt", ctx(btc["tp"] + 1, atr=1.0))
    live = restarted.history[-1]
    assert live["symbol"] == "btcusdt" and live["reason"] == "tp"
    assert live["recovered"] is False
    newest = store.paper_trades()[0]
    assert newest["symbol"] == "btcusdt" and newest["recovered"] is False


# ---------- C. 停机期间到期：bars 补结算 tp / sl / timeout + recovered 落库 ----------

def test_downtime_expired_settles_tp_sl_timeout_and_recovered_column(make_store, clock):
    store = make_store()
    book = PaperBook(store=store)
    btc = book.tick("btcusdt", ctx(100.0, atr=1.0))
    eth = book.tick("ethusdt", ctx(200.0, atr=2.0))
    sol = book.tick("solusdt", ctx(300.0, atr=3.0))
    bnb = book.tick("bnbusdt", ctx(150.0, atr=1.0))
    entry_ts = btc["entry_ts"]
    clock.advance(HOLD_MS + 5 * MINUTE)            # 停机 20 分钟，四仓都已到期

    restarted = PaperBook(store=store)
    assert restarted.restore_info["positions"] == 4

    bars_tp = [Candle(open_time=entry_ts + MINUTE, open=100.0,
                      high=btc["tp"] + 1, low=100.0, close=100.5, interval="1m")]
    bars_sl = [Candle(open_time=entry_ts + MINUTE, open=200.0,
                      high=200.0, low=eth["sl"] - 1, close=199.0, interval="1m")]
    bars_timeout = [
        Candle(open_time=entry_ts + MINUTE, open=300.0, high=303.0,
               low=299.0, close=301.0, interval="1m"),
        Candle(open_time=entry_ts + 2 * MINUTE, open=301.0, high=304.0,
               low=300.0, close=302.5, interval="1m"),
    ]
    restarted.tick("btcusdt", ctx(101.0, atr=1.0, bars=bars_tp))
    restarted.tick("ethusdt", ctx(199.5, atr=2.0, bars=bars_sl))
    restarted.tick("solusdt", ctx(301.0, atr=3.0, bars=bars_timeout))
    # bnb：同一根 bar 内高低同时碰到 tp 和 sl → 保守判 sl（与胜率评估口径一致）
    restarted.tick("bnbusdt", ctx(150.5, atr=1.0, bars=[
        Candle(open_time=entry_ts + MINUTE, open=150.0, high=bnb["tp"] + 0.5,
               low=bnb["sl"] - 0.5, close=150.0, interval="1m")]))

    by_symbol = {rec["symbol"]: rec for rec in restarted.history}
    assert by_symbol["btcusdt"]["reason"] == "tp"
    assert by_symbol["btcusdt"]["exit"] == btc["tp"]       # 用 tp 价，不用重启现价
    assert by_symbol["ethusdt"]["reason"] == "sl"
    assert by_symbol["ethusdt"]["exit"] == eth["sl"]
    assert by_symbol["solusdt"]["reason"] == "timeout"
    assert by_symbol["solusdt"]["exit"] == 302.5           # 窗口内最后一根收盘
    assert by_symbol["bnbusdt"]["reason"] == "sl"
    assert by_symbol["bnbusdt"]["exit"] == bnb["sl"]
    assert all(rec["recovered"] is True for rec in by_symbol.values())

    # recovered 是 paper_trades 的真实列，不是只在内存里的标记
    raw = {row[0]: row for row in sql(
        store, "SELECT symbol,reason,recovered,exit_price FROM paper_trades")}
    assert len(raw) == 4
    assert raw["btcusdt"] == ("btcusdt", "tp", 1, 102.5)
    assert raw["ethusdt"] == ("ethusdt", "sl", 1, 197.0)
    assert raw["solusdt"] == ("solusdt", "timeout", 1, 302.5)
    assert raw["bnbusdt"] == ("bnbusdt", "sl", 1, 148.5)

    # 补结算后立刻按现价重开，未平仓表里是新仓
    assert sql(store, "SELECT COUNT(*) FROM paper_positions") == [(4,)]
    for symbol in ("btcusdt", "ethusdt", "solusdt", "bnbusdt"):
        fresh = position_json(store, symbol)
        assert fresh["entry"] > 0

    # 再重启一次：recovered=True 能从数据库列读回（列落库验证）
    again = PaperBook(store=store)
    assert again.restore_info["closed"] == 4
    back = {rec["symbol"]: rec for rec in again.history}
    assert len(back) == 4
    assert all(rec["recovered"] is True for rec in back.values())
    assert again.summary()["source"] == "mysql"
    assert again.summary()["session"]["closed"] == 4


# ---------- C2. 补结算边界：移动止损后的 sl、bars 不覆盖窗口、无 bars ----------

def test_downtime_settle_uses_trailing_sl_and_falls_back_to_price(make_store, clock):
    store = make_store()
    book = PaperBook(store=store)
    xrp = book.tick("xrpusdt", ctx(400.0, atr=3.0))
    ada = book.tick("adausdt", ctx(500.0, atr=5.0))
    doge = book.tick("dogeusdt", ctx(250.0, atr=2.0))
    link = book.tick("linkusdt", ctx(200.0, atr=2.0, direction="bearish"))
    assert link["direction"] == "short"
    entry_ts = xrp["entry_ts"]

    # 模拟运行中移动止损：把恢复出来的 sl 抬到成本上方（保留到重启）
    saved = position_json(store, "xrpusdt")
    saved["sl"] = 402.0
    saved["trailing"] = True
    store.save_paper_position(saved)
    assert position_json(store, "xrpusdt")["sl"] == 402.0

    clock.advance(HOLD_MS + 5 * MINUTE)
    restarted = PaperBook(store=store)
    assert restarted.restore_info["positions"] == 4

    # xrp：bar 触及抬高后的 sl（401 <= 402），且未到 tp；应按 sl=402 离场
    restarted.tick("xrpusdt", ctx(404.0, atr=3.0, bars=[
        Candle(open_time=entry_ts + MINUTE, open=400.0, high=405.0,
               low=401.0, close=404.0, interval="1m")]))
    # ada：bars 全都晚于 expires_ts（不覆盖停机窗口）→ 回落到重启现价
    restarted.tick("adausdt", ctx(501.25, atr=5.0, bars=[
        Candle(open_time=ada["expires_ts"] + MINUTE, open=500.0, high=520.0,
               low=480.0, close=500.0, interval="1m")]))
    # doge：完全没有 bars → 回落到重启现价
    restarted.tick("dogeusdt", ctx(251.5, atr=2.0))
    # link 是空仓：bar 最高价越过 sl（204 >= 203）→ 应按 sl 离场（方向分支验证）
    restarted.tick("linkusdt", ctx(198.0, atr=2.0, direction="bearish", bars=[
        Candle(open_time=entry_ts + MINUTE, open=200.0, high=link["sl"] + 1,
               low=198.0, close=199.0, interval="1m")]))

    by_symbol = {rec["symbol"]: rec for rec in restarted.history}
    assert by_symbol["xrpusdt"]["reason"] == "sl"
    assert by_symbol["xrpusdt"]["exit"] == 402.0           # 移动止损价，不是原始 sl
    assert by_symbol["xrpusdt"]["pnl"] == 5.0              # (402-400)*qty=2.5
    assert by_symbol["adausdt"]["reason"] == "timeout"
    assert by_symbol["adausdt"]["exit"] == 501.25
    assert by_symbol["dogeusdt"]["reason"] == "timeout"
    assert by_symbol["dogeusdt"]["exit"] == 251.5
    assert by_symbol["linkusdt"]["direction"] == "short"
    assert by_symbol["linkusdt"]["reason"] == "sl"
    assert by_symbol["linkusdt"]["exit"] == link["sl"]
    assert all(rec["recovered"] is True for rec in by_symbol.values())

    rows = sql(store, "SELECT symbol,recovered FROM paper_trades")
    assert {row[0]: row[1] for row in rows} == {
        "xrpusdt": 1, "adausdt": 1, "dogeusdt": 1, "linkusdt": 1}


# ---------- D. 数据库 disabled：按 main 的注入规则降级纯内存 ----------

def test_disabled_storage_degrades_to_memory(monkeypatch):
    def forbidden(**_):
        pytest.fail("disabled storage 不允许发起连接")

    monkeypatch.setattr(storage.pymysql, "connect", forbidden)
    disabled = Storage({})
    assert disabled.enabled is False
    # 无持久化时这些接口必须安全返回空值（main 才能在没库的机器上跑）
    assert disabled.paper_positions() == []
    assert disabled.paper_trades() == []
    assert disabled.paper_trade_stats() == {
        "count": 0, "wins": 0, "losses": 0, "timeouts": 0, "pnl": 0}
    assert disabled.paper_symbol_stats() == []

    # main.py:458 的注入规则：只有 enabled 的 Storage 才注入，否则 None
    book = PaperBook(store=disabled if disabled.enabled else None)
    assert book.store is None
    assert book.restore_info == {"positions": 0, "trades": 0, "closed": 0}

    pos = book.tick("btcusdt", ctx(100.0, atr=1.0))
    book.tick("btcusdt", ctx(pos["tp"] + 1, atr=1.0))
    summary = book.summary()
    assert summary["source"] == "memory"
    assert summary["session"]["closed"] == 1
    assert summary["session"]["wins"] == 1
    assert summary["window"]["records"] == 1
    assert [row["symbol"] for row in summary["bySymbol"]] == ["btcusdt"]
    assert "纯内存降级" in summary["note"]

    # P2-6 复验：即使调用方绕过 main 直接把 disabled Storage 交给 PaperBook，
    # 也必须按"没接库"处理，不能误报 source=mysql
    direct = PaperBook(store=disabled)
    assert direct.store is None
    assert direct.summary()["source"] == "memory"


# ---------- E. 数据库写入异常不能打断 5 秒一轮的 tick ----------

def test_sql_write_failure_does_not_break_tick(make_store, clock):
    store = make_store()
    book = PaperBook(store=store)
    opened = book.tick("btcusdt", ctx(100.0, atr=1.0))
    assert opened is not None

    # 平仓流水 INSERT 失败：tick 仍必须返回仓位、内存账本照常工作
    store._conn.fail = "INTO paper_trades"   # 匹配 INSERT IGNORE INTO paper_trades
    out = book.tick("btcusdt", ctx(opened["tp"] + 1, atr=1.0))
    assert out is not None and out["symbol"] == "btcusdt"
    assert book.history[-1]["reason"] == "tp"
    assert book.stats["closed"] == 1


# ---------- F. P1 复验：流水写失败不丢单，故障恢复后自动补写 ----------

def test_failed_trade_write_keeps_old_row_then_retries(make_store, clock):
    store = make_store()
    book = PaperBook(store=store)
    opened = book.tick("btcusdt", ctx(100.0, atr=1.0))
    old_entry = opened["entry"]
    assert old_entry == 100.0

    # 流水 INSERT 失败：内存里已平仓重开，但库里旧行必须还在、不能被新仓覆盖
    store._conn.fail = "INTO paper_trades"   # 匹配 INSERT IGNORE INTO paper_trades
    book.tick("btcusdt", ctx(opened["tp"] + 1, atr=1.0))
    assert book.history[-1]["reason"] == "tp"
    assert book.stats["closed"] == 1
    assert book.positions["btcusdt"].entry == opened["tp"] + 1
    assert store.paper_trades() == []                       # 流水确实没写进去
    assert sql(store, "SELECT COUNT(*) FROM paper_positions") == [(1,)]
    assert position_json(store, "btcusdt")["entry"] == old_entry   # 旧行保留

    # 故障恢复：下一次 tick 先补写流水、删旧行，再把新仓落库；只写一条
    new_entry = book.positions["btcusdt"].entry
    store._conn.fail = None
    book.tick("btcusdt", ctx(new_entry + 0.05, atr=1.0))
    trades = store.paper_trades()
    assert len(trades) == 1
    assert trades[0]["symbol"] == "btcusdt"
    assert trades[0]["reason"] == "tp"
    assert trades[0]["entry"] == old_entry
    assert trades[0]["recovered"] is False
    saved = position_json(store, "btcusdt")
    assert saved["entry"] == new_entry                      # 新仓已 REPLACE 旧行
    assert saved["entry"] != old_entry
    # 再 tick 一次不能重复补写
    book.tick("btcusdt", ctx(new_entry + 0.10, atr=1.0))
    assert len(store.paper_trades()) == 1


# ---------- F2. R2：歧义失败（写已提交但客户端报错）重试必须幂等 ----------

def test_ambiguous_trade_write_retry_is_idempotent(make_store, clock):
    """INSERT 已提交但客户端收到异常时，重试不能写出第二条平仓流水。

    这是 P1 修复引入的 at-least-once 语义的兜底：paper_trades 的
    trade_key(symbol+entryTs) 唯一键 + INSERT IGNORE 让重试变成幂等操作。
    """
    store = make_store()
    book = PaperBook(store=store)
    opened = book.tick("btcusdt", ctx(100.0, atr=1.0))
    real_save = store.save_paper_trade
    calls = {"n": 0}

    def ambiguous(trade):
        calls["n"] += 1
        real_save(trade)                    # 真实写库（已提交）
        if calls["n"] == 1:
            raise RuntimeError("客户端丢响应（写已提交）")

    store.save_paper_trade = ambiguous
    book.tick("btcusdt", ctx(opened["tp"] + 1, atr=1.0))
    # 第一次写入已进库、但客户端认为失败 → 挂起重试；重试被唯一键忽略
    assert len(store.paper_trades()) == 1
    assert calls["n"] >= 2                  # close 之后至少重试过一次（同 tick 随 _persist）
    retries_before = calls["n"]
    new_entry = book.positions["btcusdt"].entry
    book.tick("btcusdt", ctx(new_entry + 0.05, atr=1.0))
    # 之后的 tick 也不能再补出重复流水，且不再继续重试
    assert len(store.paper_trades()) == 1
    assert store.paper_trade_stats()["count"] == 1
    assert calls["n"] == retries_before
    assert position_json(store, "btcusdt")["entry"] == new_entry


# ---------- F3. R2 跨重启复验：流水已提交但未删未平仓行就崩溃，重启补结算不得重复 ----------

def test_restart_settlement_does_not_duplicate_committed_trade(make_store, clock):
    """崩溃点：流水 INSERT 已提交、未平仓行还没删 → 重启后应按旧仓补结算。

    修复前这会写第二条流水（同一次开仓被记两次）；trade_key 唯一键 +
    INSERT IGNORE 之后，补结算的写入被忽略，库里只有崩溃前那一条。
    """
    store = make_store()
    book = PaperBook(store=store)
    opened = book.tick("btcusdt", ctx(100.0, atr=1.0))
    assert opened is not None

    # 模拟"流水已进库、进程在删未平仓行之前崩溃"：手工写入同一仓位的流水
    store.save_paper_trade({
        "symbol": "btcusdt", "entryTs": opened["entry_ts"],
        "closedAt": opened["entry_ts"], "reason": "tp",
        "entry": opened["entry"], "exit": opened["tp"],
        "pnl": 25.0, "pnlPct": 50.0, "recovered": False,
    })
    assert len(store.paper_trades()) == 1

    # 重启时旧仓位还在且已到期 → bars 补结算会再次尝试写流水，必须被去重
    clock.advance(HOLD_MS + MINUTE)
    restarted = PaperBook(store=store)
    assert restarted.restore_info["positions"] == 1
    bars = [Candle(open_time=opened["entry_ts"] + MINUTE, open=100.0,
                   high=opened["tp"] + 1, low=100.0, close=100.5, interval="1m")]
    restarted.tick("btcusdt", ctx(101.0, atr=1.0, bars=bars))

    assert len(store.paper_trades()) == 1             # 没有第二条流水
    assert store.paper_trade_stats()["count"] == 1
    assert position_json(store, "btcusdt")["entry"] == 101.0   # 补结算后新仓已落库


# ---------- G. P2-4 复验：启动恢复读失败 → 纯内存、不覆盖旧行 ----------

def test_restore_read_failure_degrades_to_memory_without_overwrite(make_store, clock):
    store = make_store()
    book = PaperBook(store=store)
    opened = book.tick("btcusdt", ctx(100.0, atr=1.0))
    entry = opened["entry"]

    # 模拟启动瞬间读 paper_positions 失败（连接正常但查询报错）
    store._conn.fail = "SELECT data FROM paper_positions"
    restarted = PaperBook(store=store)
    assert restarted._store_suspended is True
    assert restarted.positions == {}
    assert restarted.restore_info["positions"] == 0

    # 纯内存模式照常推演，但绝不写库/不覆盖：库里旧行 entry 保持原值
    pos = restarted.tick("btcusdt", ctx(entry + 0.5, atr=1.0))
    assert pos is not None and pos["symbol"] == "btcusdt"
    assert restarted.summary()["source"] == "memory"
    assert raw_position_entry(store, "btcusdt") == entry   # 旧行没被覆盖

    # 平仓也只进内存，库里仍是旧仓，留给下次重启恢复
    restarted.tick("btcusdt", ctx(pos["tp"] + 1, atr=1.0))
    assert restarted.history[-1]["reason"] == "tp"
    store._conn.fail = None
    assert store.paper_trades() == []
    assert position_json(store, "btcusdt")["entry"] == entry
    assert restarted.summary()["source"] == "memory"


# ---------- H. P2-3 复验：删除未平仓行失败不产生重复流水 ----------

def test_failed_position_delete_does_not_duplicate_trade(make_store, clock):
    store = make_store()
    book = PaperBook(store=store)
    opened = book.tick("btcusdt", ctx(100.0, atr=1.0))

    # DELETE 失败但 INSERT 成功：重开的新仓 REPLACE 掉旧行，旧行不能残留
    store._conn.fail = "DELETE FROM paper_positions"
    book.tick("btcusdt", ctx(opened["tp"] + 1, atr=1.0))
    assert len(store.paper_trades()) == 1
    new_entry = book.positions["btcusdt"].entry
    assert position_json(store, "btcusdt")["entry"] == new_entry
    store._conn.fail = None

    # 重启后不会把已平过的旧仓恢复出来再结算一次（不产生重复流水）
    restarted = PaperBook(store=store)
    assert restarted.positions["btcusdt"].entry == new_entry
    assert len(store.paper_trades()) == 1
    restarted.tick("btcusdt", ctx(new_entry + 0.2, atr=1.0))
    assert len(store.paper_trades()) == 1
