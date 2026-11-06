"""独立验证：审计修复的端到端回归（真实 storage.Storage + 真实 QuantEngine/RiskEngine）。

验证目标（全部独立编写，不复制实现者测试的断言）：
  1. 决策 trace / gate：QuantEngine 真实 _finish_trace / _append_gate → storage.save_decision_trace
     落 decision_traces 表；新引擎用 storage.decision_traces + restore_persistent_state()
     恢复 decision_traces / gate_log / latest_traces，顺序为内部时间升序（库内 newest-first）。
  2. 风控状态：RiskEngine 产生连亏暂停 / 冷却 / 日亏限额 → engine.save_persistent_state(force=True)
     写 quant_state 的 risk；新引擎 restore 后对同一 RiskInput 的判定结论与旧引擎完全一致。
     机制漂移 caution/shadow 同样持久化并恢复。
  3. 保留期：storage.prune 的 90/365/30/7/30 默认值与 0=永久语义；candles/equity/paper 逻辑不变。
  4. JSON 原子写：quant._atomic_write_json 成功路径合法 JSON、无 .tmp 残留；
     os.replace 失败时返回 False、不抛、旧文件不被破坏。
  5. 降级：storage.enabled=False 时，保存/恢复全部安全 no-op，引擎 restore 返回全 0。

SQLite 适配器自包含（复制自 tests/test_paper_store.py 的思路，不 import 其他测试文件），
monkeypatch storage.pymysql.connect，绝不连接真实 MySQL；不读 config.yaml，不启动服务。
"""
from __future__ import annotations

import json
import pathlib
import re
import sqlite3
import time

import pytest

import quant as quant_mod
import storage
from decision.risk import RiskInput, TradeRecord
from quant import GATE_MAX_LOG, QuantEngine
from storage import Storage

DAY = 86_400_000
NOW = 1_800_000_000_000          # 固定"当前时间"（毫秒），保留期断言不受真实时钟影响


# ---------------------------------------------------------------------------
# SQLite 适配器：只把 MySQL 专有语法翻译成 SQLite 能跑的等价写法
# ---------------------------------------------------------------------------

class SQLCursor:
    def __init__(self, connection):
        self.connection = connection
        self.cursor = connection.db.cursor()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.cursor.close()

    def execute(self, sql, args=()):
        sql = re.sub(r"ENGINE=InnoDB DEFAULT CHARSET=utf8mb4", "", sql)
        sql = sql.replace("BIGINT AUTO_INCREMENT PRIMARY KEY",
                          "INTEGER PRIMARY KEY AUTOINCREMENT")
        sql = re.sub(r",\s*INDEX \w+\([^)]*\)", "", sql)
        show = re.fullmatch(r"SHOW COLUMNS FROM (\w+) LIKE '(\w+)'", sql)
        if show:
            sql = (f"SELECT name FROM pragma_table_info('{show[1]}') "
                   f"WHERE name='{show[2]}'")
        sql = sql.replace("INSERT IGNORE", "INSERT OR IGNORE").replace("%s", "?")
        return self.cursor.execute(sql, args)

    def executemany(self, sql, rows):
        sql = sql.replace("INSERT IGNORE", "INSERT OR IGNORE").replace("%s", "?")
        return self.cursor.executemany(sql, rows)

    def fetchall(self):
        return self.cursor.fetchall()


class SQLConnection:
    def __init__(self):
        self.db = sqlite3.connect(":memory:", isolation_level=None,
                                  check_same_thread=False)
        self.db.create_function("GREATEST", 2, max)
        self.db.create_function("LEAST", 2, min)
        self.db.create_function("MOD", 2, lambda a, b: a % b)

    def cursor(self):
        return SQLCursor(self)

    def ping(self, **_):
        pass

    def begin(self):
        self.db.execute("BEGIN")

    def commit(self):
        self.db.commit()

    def rollback(self):
        self.db.rollback()


@pytest.fixture
def make_store(monkeypatch):
    """真实 Storage：pymysql.connect 被替换成内存 SQLite（隔离，不连生产库）。"""
    connections = []

    def create(**kwargs):
        conn = SQLConnection()
        connections.append(conn)
        # _init() 的 _backfill_equity_daily() 会读现存 raw 权益表；先建空表兜底。
        conn.db.execute(
            "CREATE TABLE equity(acct TEXT,ts BIGINT,bal DOUBLE,upnl DOUBLE,"
            "PRIMARY KEY(acct,ts))")
        monkeypatch.setattr(storage.pymysql, "connect", lambda **_: conn)
        store = Storage({"user": "isolated-audit"}, **kwargs)
        assert store.enabled, "真实建表 DDL 必须走通"
        return store

    yield create
    for conn in connections:
        conn.db.close()


def _engine(tmp_path, name: str) -> QuantEngine:
    """最小引擎：不接交易所、不接 storage，仅用于真实 trace/gate/restore 流程。"""
    return QuantEngine({"list": []}, str(tmp_path / f"{name}.json"))


def _count(store, table: str) -> int:
    return int(store._qall(f"SELECT COUNT(*) FROM {table}")[0][0])


def _risk_req(account: str, symbol: str, now_ms: int) -> RiskInput:
    return RiskInput(account=account, symbol=symbol, side="BUY", entry=100.0,
                     sl=98.0, equity=10_000.0, available=10_000.0, now_ms=now_ms)


# ---------------------------------------------------------------------------
# 1. 决策 trace / gate：真实 _finish_trace / _append_gate → 表 → 新引擎恢复
# ---------------------------------------------------------------------------

def test_trace_and_gate_persist_through_real_engine(tmp_path, make_store):
    store = make_store()
    assert store.decision_traces() == [] and store.decision_traces(kind="gate") == []

    eng = _engine(tmp_path, "trace-writer")
    eng.trace_storage_fn = store.save_decision_trace   # 与 main.py 注入方式一致

    # 真实 _trace_start -> _trace_stage -> _finish_trace 三段式：两条同币 + 一条异币
    hits = [
        {"symbol": "BTCUSDT", "pattern": "hammer", "name_zh": "锤子线", "direction": "bullish"},
        {"symbol": "BTCUSDT", "pattern": "engulfing", "name_zh": "吞没", "direction": "bullish"},
        {"symbol": "ETHUSDT", "pattern": "doji", "name_zh": "十字星", "direction": "bearish"},
    ]
    traces = []
    for index, hit in enumerate(hits):
        trace = eng._trace_start(hit, 100.0 + index)
        trace["ts"] = NOW + index * 1_000       # 固定时间，排序断言不受真实时钟影响
        eng._trace_stage(trace, "signal", "信号评分", "passed", "评分通过", score=80 + index)
        eng._trace_stage(trace, "risk", "统一风控", "passed", "风控通过",
                         risk={"dailyReturnPct": 0.1})
        eng._finish_trace(trace, "open", "已开仓", "第%d笔" % index,
                          execution={"orderId": index + 1})
        traces.append(trace)
    gate_old = {"ts": NOW + 10_000, "symbol": "BTCUSDT", "direction": "bullish",
                "reason": "评分未通过", "code": "score_gate", "score": 42.0,
                "gap": 3.5, "grade": "C"}
    gate_new = {"ts": NOW + 11_000, "symbol": "ETHUSDT", "direction": "bearish",
                "reason": "环境过滤：ATR 过高"}
    eng._append_gate(gate_old)
    eng._append_gate(gate_new)

    # --- 落库断言：行数 / 列值 / payload 完整（中文原样，非转义） ---
    assert _count(store, "decision_traces") == 5
    rows = store._qall(
        "SELECT ts,kind,symbol,status,payload FROM decision_traces "
        "WHERE kind=%s ORDER BY ts ASC", ("trace",))
    assert len(rows) == 3
    for row, trace in zip(rows, traces):
        ts, kind, symbol, status, payload = row
        assert (ts, kind, symbol, status) == (
            trace["ts"], "trace", trace["symbol"], "open")
        assert json.loads(payload) == trace          # 完整 dict 原样往返
        assert "stages" in payload and "execution" in payload
    assert "评分通过" in rows[0][4]                  # ensure_ascii=False
    gate_rows = store._qall(
        "SELECT ts,status,payload FROM decision_traces WHERE kind=%s ORDER BY ts ASC",
        ("gate",))
    assert gate_rows[0][1] == "score_gate"           # gate 的 status 回落到 code
    assert json.loads(gate_rows[0][2]) == gate_old
    assert json.loads(gate_rows[1][2]) == gate_new

    # --- 重启恢复：新引擎注入 read/store 函数后 restore_persistent_state() ---
    eng2 = _engine(tmp_path, "trace-reader")
    eng2.trace_read_fn = store.decision_traces
    eng2.trace_storage_fn = store.save_decision_trace
    eng2.state_read_fn = store.quant_state
    eng2.state_storage_fn = store.save_quant_state
    restored = eng2.restore_persistent_state()
    assert restored == {"traces": 3, "gates": 2, "risk": False, "drift": False}

    # 内部顺序：storage 返回 newest-first（ETH 最新），恢复后必须按时间升序
    assert [t["ts"] for t in eng2.decision_traces] == [NOW, NOW + 1_000, NOW + 2_000]
    assert [t["symbol"] for t in eng2.decision_traces] == ["btcusdt", "btcusdt", "ethusdt"]
    assert [g["ts"] for g in eng2.gate_log] == [NOW + 10_000, NOW + 11_000]
    # 同一币的多条 trace：latest_traces 只保留最新一条
    assert eng2.latest_traces["btcusdt"]["id"] == traces[1]["id"]
    assert eng2.latest_traces["ethusdt"]["id"] == traces[2]["id"]
    # 恢复后的 trace 仍是完整 payload，元数据（kind/savedTs）由 storage 补齐
    restored_old = eng2.decision_traces[0]
    assert restored_old["id"] == traces[0]["id"]          # payload 原 id 优先
    assert restored_old["kind"] == "trace"
    assert restored_old["savedTs"] == NOW
    assert restored_old["stages"][1]["id"] == "risk"
    assert restored_old["stages"][1]["status"] == "passed"
    assert restored_old["stages"][1]["risk"] == {"dailyReturnPct": 0.1}
    assert restored_old["execution"] == {"orderId": 1}


def test_restore_caps_traces_at_24_and_gates_at_max_log(tmp_path, make_store):
    store = make_store()
    # 直接经 storage 写 30 条 trace + 45 条 gate（模拟长期运行后的库）
    for index in range(30):
        # 真实 _trace_start 会 lower() 币种，这里保持同样口径
        store.save_decision_trace({"ts": NOW + index, "symbol": "btcusdt",
                                   "status": "blocked", "seq": index})
    for index in range(45):
        store.save_decision_trace({"ts": NOW + index, "symbol": "BTCUSDT",
                                   "reason": "gate-%d" % index, "code": "x" * 20},
                                  kind="gate")

    eng = _engine(tmp_path, "trace-cap")
    eng.trace_read_fn = store.decision_traces
    restored = eng.restore_persistent_state()
    assert restored["traces"] == 24 and restored["gates"] == GATE_MAX_LOG
    # deque maxlen：内存里保留最新的 24/40 条，且按时间升序回填
    assert [t["seq"] for t in eng.decision_traces] == list(range(6, 30))
    assert [g["reason"] for g in eng.gate_log] == [
        "gate-%d" % i for i in range(5, 45)]
    assert eng.latest_traces["btcusdt"]["seq"] == 29
    assert eng.decision_traces.maxlen == 24 and eng.gate_log.maxlen == GATE_MAX_LOG


# ---------------------------------------------------------------------------
# 2. 风控 / 漂移状态：真实 save_persistent_state → quant_state → 新引擎恢复后判定一致
# ---------------------------------------------------------------------------

def test_risk_state_restored_gives_identical_verdicts(tmp_path, make_store):
    store = make_store()
    eng = _engine(tmp_path, "risk-old")
    eng.state_storage_fn = store.save_quant_state

    now = int(time.time() * 1000)
    # A: 4 连亏 -> 触发连亏熔断（pause_until = 最后亏损 + 60 分钟）
    for index in range(4):
        eng.risk.record_trade("acct_pause", TradeRecord(
            ts=now - (4 - index) * 60_000, pnl=-5.0, symbol="BTCUSDT"))
    # B: 单笔亏损 -> 未熔断，但同币进入冷却
    eng.risk.record_trade("acct_cool", TradeRecord(
        ts=now - 5_000, pnl=-3.0, symbol="BTCUSDT"))
    # C: 当日 3 笔亏损共 -450（3 连亏 < 4 不熔断）-> 触及 3% 日亏限额
    for index in range(3):
        eng.risk.record_trade("acct_daily", TradeRecord(
            ts=now - (3 - index) * 60_000, pnl=-150.0, symbol="ETHUSDT"))
    snap_before = eng.risk.snapshot()
    assert json.loads(json.dumps(snap_before)) == snap_before    # 必须可 JSON 序列化

    eng.save_persistent_state(force=True)
    assert store.quant_state("risk") == snap_before

    # 漂移保护：caution 先触发，清掉限流后再触发一次 -> shadow
    detector = eng._drift_for("acct_pause")["winrate"]
    eng._handle_drift("acct_pause", "winrate", detector)
    eng._drift_last_alert.clear()
    eng._drift_last_global_alert = 0
    eng._handle_drift("acct_pause", "winrate", detector)
    assert eng._drift_caution_until > now and eng._drift_shadow_until > now
    assert store.quant_state("drift") == {
        "cautionUntil": eng._drift_caution_until,
        "shadowUntil": eng._drift_shadow_until,
    }

    # 新引擎（模拟服务重启）恢复
    eng2 = _engine(tmp_path, "risk-new")
    eng2.state_read_fn = store.quant_state
    eng2.state_storage_fn = store.save_quant_state
    restored = eng2.restore_persistent_state()
    assert restored["risk"] is True and restored["drift"] is True
    assert eng2.risk.snapshot() == snap_before                  # 状态逐字段一致
    assert (eng2._drift_caution_until, eng2._drift_shadow_until) == (
        eng._drift_caution_until, eng._drift_shadow_until)

    # 同一 RiskInput 在新旧引擎上的结论必须完全一致
    cases = [
        ("acct_pause", "ETHUSDT", "loss_streak_pause"),
        ("acct_cool", "BTCUSDT", "cooldown"),
        ("acct_daily", "SOLUSDT", "daily_loss_limit"),
    ]
    for account, symbol, expected_code in cases:
        old = eng.risk.evaluate(_risk_req(account, symbol, now))
        new = eng2.risk.evaluate(_risk_req(account, symbol, now))
        assert old.code == expected_code, "%s: %s != %s" % (account, old.code, expected_code)
        assert (new.allowed, new.code) == (old.allowed, old.code)
        assert new.reason == old.reason
        assert new.cooldown_sec == old.cooldown_sec


def test_restore_tolerates_corrupt_quant_state(tmp_path, make_store):
    store = make_store()
    # 库里是坏 JSON / 非 dict：恢复必须安全返回、不抛
    store._q("REPLACE INTO quant_state(name,ts,data) VALUES(%s,%s,%s)",
             ("risk", NOW, "{not-json"))
    store._q("REPLACE INTO quant_state(name,ts,data) VALUES(%s,%s,%s)",
             ("drift", NOW, "[1,2,3]"))
    eng = _engine(tmp_path, "corrupt")
    eng.state_read_fn = store.quant_state
    assert eng.restore_persistent_state() == {
        "traces": 0, "gates": 0, "risk": False, "drift": False}
    assert eng.risk.snapshot() == {"accounts": {}}
    assert eng._drift_caution_until == 0 and eng._drift_shadow_until == 0


# ---------------------------------------------------------------------------
# 3. 保留期：默认 90/365/30/7/30 + 0=永久 + candles/equity/paper 逻辑不变
# ---------------------------------------------------------------------------

def _insert_rows(store, rows):
    """按给定 (表, ts, 标记) 插入最小行。"""
    for table, ts, marker in rows:
        if table == "hits":
            store._q("INSERT INTO hits(ts,symbol,pattern) VALUES(%s,%s,%s)",
                     (ts, marker, marker))
        elif table == "quant_trades":
            store._q("INSERT INTO quant_trades(ts,symbol) VALUES(%s,%s)", (ts, marker))
        elif table == "quant_events":
            store._q("INSERT INTO quant_events(ts,kind) VALUES(%s,%s)", (ts, marker))
        elif table == "news":
            store._q("INSERT INTO news(url,ts) VALUES(%s,%s)", ("u-" + marker, ts))
        elif table == "candles":
            store._q("INSERT INTO candles(symbol,interval,open_time) "
                     "VALUES(%s,%s,%s)", (marker, "15m", ts))
        elif table == "equity":
            store._q("INSERT INTO equity(acct,ts,bal,upnl) VALUES(%s,%s,%s,%s)",
                     (marker, ts, 1.0, 0.0))
        else:  # pragma: no cover - 测试自身写错表名时直接失败
            raise AssertionError(table)


def _markers(store, table: str, ts_col: str = "ts", col: str = "symbol") -> list:
    return [row[0] for row in store._qall(
        "SELECT %s FROM %s ORDER BY %s" % (col, table, ts_col))]


def test_prune_defaults_keep_critical_data_forever(make_store):
    default_store = make_store()
    # 关键量化数据（命中/成交/事件/决策流水）默认永久，新闻仍 7 天
    assert (default_store.hit_days, default_store.trade_keep_days,
            default_store.quant_event_days, default_store.news_days,
            default_store.trace_days, default_store.paper_keep_days) == \
        (0, 0, 0, 7, 0, 0)
    assert default_store.equity_days == 30
    # 显式配旧保留期，验证按天清理的逻辑本身没坏
    store = make_store(hit_days=90, quant_trade_days=365, quant_event_days=30,
                       news_days=7, trace_days=30, equity_days=2)

    _insert_rows(store, [
        ("hits", NOW - 200 * DAY, "hit-old"), ("hits", NOW - 1 * DAY, "hit-new"),
        ("quant_trades", NOW - 400 * DAY, "trade-old"),
        ("quant_trades", NOW - 300 * DAY, "trade-mid"),
        ("quant_trades", NOW - 1 * DAY, "trade-new"),
        ("quant_events", NOW - 40 * DAY, "event-old"),
        ("quant_events", NOW - 20 * DAY, "event-new"),
        ("news", NOW - 10 * DAY, "news-old"), ("news", NOW - 5 * DAY, "news-new"),
        ("candles", NOW - 2 * 3600_000, "candle-old"),
        ("candles", NOW - 10 * 60_000, "candle-new"),
        ("equity", NOW - 3 * DAY, "eq-old"), ("equity", NOW - DAY // 2, "eq-new"),
    ])
    store.save_decision_trace({"ts": NOW - 40 * DAY, "symbol": "TRACE-OLD"})
    store.save_decision_trace({"ts": NOW - 20 * DAY, "symbol": "TRACE-NEW"})
    # 模拟仓默认永久：远古平仓流水 + 未平仓快照都不能被 prune 删掉
    store.save_paper_trade({"closedAt": NOW - 500 * DAY, "symbol": "PAPER-OLD",
                            "entryTs": 111})
    store.save_paper_position({"symbol": "LIVE", "entry": 100.0})

    store.prune(NOW)

    assert _markers(store, "hits", col="pattern") == ["hit-new"]
    assert _markers(store, "quant_trades") == ["trade-mid", "trade-new"]
    assert _markers(store, "quant_events", col="kind") == ["event-new"]
    assert [r[0] for r in store._qall("SELECT url FROM news ORDER BY ts")] == \
        ["u-news-new"]
    assert _markers(store, "decision_traces") == ["TRACE-NEW"]
    assert _markers(store, "candles", "open_time") == ["candle-new"]
    assert _markers(store, "equity", col="acct") == ["eq-new"]
    assert _count(store, "paper_trades") == 1
    assert [p["symbol"] for p in store.paper_positions()] == ["LIVE"]


def test_prune_zero_days_keeps_forever_but_candles_equity_still_clean(make_store):
    store = make_store(hit_days=0, quant_trade_days=0, quant_event_days=0,
                       news_days=0, trace_days=0, equity_days=2)
    ancient = NOW - 500 * DAY
    _insert_rows(store, [
        ("hits", ancient, "hit"),
        ("quant_trades", ancient, "trade"),
        ("quant_events", ancient, "event"),
        ("news", ancient, "news"),
        ("candles", NOW - 2 * 3600_000, "candle-old"),
        ("candles", NOW - 10 * 60_000, "candle-new"),
        ("equity", NOW - 3 * DAY, "eq-old"),
        ("equity", NOW - DAY // 2, "eq-new"),
    ])
    store.save_decision_trace({"ts": ancient, "symbol": "TRACE"})
    store.save_paper_trade({"closedAt": ancient, "symbol": "PAPER", "entryTs": 222})
    store.save_paper_position({"symbol": "LIVE", "entry": 1.0})

    store.prune(NOW)

    for table in ("hits", "quant_trades", "quant_events", "news",
                  "decision_traces", "paper_trades"):
        assert _count(store, table) == 1, table + " 在 0=永久配置下被误删"
    assert store.paper_positions()
    # candles / equity 的清理逻辑与保留期配置无关，必须保持原行为
    assert _markers(store, "candles", "open_time") == ["candle-new"]
    assert _markers(store, "equity", col="acct") == ["eq-new"]


def test_prune_zero_days_skips_only_that_table(make_store):
    store = make_store(hit_days=0, news_days=7, quant_event_days=30,
                       trace_days=30)       # 只让 hits 永久，其余显式按天清理
    ancient = NOW - 500 * DAY
    _insert_rows(store, [
        ("hits", ancient, "hit"), ("news", ancient, "news"),
        ("quant_events", ancient, "event"),
    ])
    store.prune(NOW)
    assert _count(store, "hits") == 1                 # 0 = 跳过该表
    assert _count(store, "news") == 0                 # 默认 7 天照删
    assert _count(store, "quant_events") == 0         # 默认 30 天照删


def test_prune_paper_keep_days_deletes_closed_only(make_store):
    store = make_store(paper_keep_days=7)
    store.save_paper_trade({"closedAt": NOW - 9 * DAY, "symbol": "P9", "entryTs": 1})
    store.save_paper_trade({"closedAt": NOW - 1 * DAY, "symbol": "P1", "entryTs": 2})
    store.save_paper_position({"symbol": "LIVE", "entry": 100.0})
    store.prune(NOW)
    assert [r[0] for r in store._qall(
        "SELECT symbol FROM paper_trades ORDER BY closed_ts")] == ["P1"]
    assert [p["symbol"] for p in store.paper_positions()] == ["LIVE"]


# ---------------------------------------------------------------------------
# 4. JSON 原子写：成功 / 失败路径
# ---------------------------------------------------------------------------

def test_atomic_write_success_no_tmp_left(tmp_path):
    path = tmp_path / "state.json"
    assert quant_mod._atomic_write_json(
        str(path), {"中文": "值", "nested": [1, 2, 3]}) is True
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "中文": "值", "nested": [1, 2, 3]}
    assert "中文" in path.read_text(encoding="utf-8")     # 非 ascii 转义
    assert not (tmp_path / "state.json.tmp").exists()

    # 引擎真实落盘路径（_save_positions）同样原子写、无 .tmp 残留
    eng = _engine(tmp_path, "atomic")
    eng._save_positions()
    assert json.loads((tmp_path / "quant-positions.json").read_text(
        encoding="utf-8")) == {}
    assert not (tmp_path / "quant-positions.json.tmp").exists()
    eng.state = {"acct": {"positions": {"BTCUSDT": {"entry": 100.0}}}}
    eng._save_positions()
    saved = json.loads((tmp_path / "quant-positions.json").read_text(encoding="utf-8"))
    assert saved == {"acct": {"BTCUSDT": {"entry": 100.0}}}


def test_atomic_write_failure_keeps_old_file_and_never_raises(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    assert quant_mod._atomic_write_json(str(path), {"old": 1}) is True
    before = path.read_text(encoding="utf-8")

    def boom_replace(src, dst):
        raise OSError("模拟磁盘故障 / 跨设备 replace 失败")

    monkeypatch.setattr(quant_mod.os, "replace", boom_replace)
    # 返回 False、不抛异常、旧文件内容不被破坏
    assert quant_mod._atomic_write_json(str(path), {"new": 2}) is False
    assert path.read_text(encoding="utf-8") == before
    assert not (tmp_path / "state.json.tmp").exists()     # 失败后清理临时文件

    # 数据本身不可 JSON 序列化：同样安全失败，不落半截文件
    assert quant_mod._atomic_write_json(str(path), {"bad": object()}) is False
    assert path.read_text(encoding="utf-8") == before

    # 目录不存在：不抛异常，返回 False
    assert quant_mod._atomic_write_json(
        str(tmp_path / "missing-dir" / "x.json"), {"a": 1}) is False


# ---------------------------------------------------------------------------
# 5. 降级：storage.enabled=False 全程安全 no-op
# ---------------------------------------------------------------------------

def test_disabled_storage_save_restore_is_safe_noop(tmp_path, monkeypatch):
    def forbidden(**_):
        pytest.fail("disabled storage must not connect to MySQL")

    monkeypatch.setattr(storage.pymysql, "connect", forbidden)
    store = Storage({})
    assert store.enabled is False

    # storage 新接口全部安全 no-op
    store.save_decision_trace({"ts": NOW, "symbol": "BTCUSDT", "status": "open"})
    assert store.decision_traces() == []
    assert store.decision_traces(limit=10, kind="gate") == []
    store.save_quant_state("risk", {"accounts": {}})
    assert store.quant_state("risk") is None
    store.prune(NOW)                        # 不连库、不抛

    # 引擎注入禁用 storage：内存逻辑照常，保存/恢复静默降级
    eng = _engine(tmp_path, "disabled-writer")
    eng.trace_storage_fn = store.save_decision_trace
    eng.trace_read_fn = store.decision_traces
    eng.state_storage_fn = store.save_quant_state
    eng.state_read_fn = store.quant_state
    trace = eng._trace_start({"symbol": "BTCUSDT", "pattern": "hammer"}, 100.0)
    eng._finish_trace(trace, "open", "已开仓", "x")
    eng._append_gate({"ts": NOW, "symbol": "BTCUSDT", "reason": "x"})
    eng.save_persistent_state(force=True)   # 不抛
    assert len(eng.decision_traces) == 1 and len(eng.gate_log) == 1

    eng2 = _engine(tmp_path, "disabled-reader")
    eng2.trace_read_fn = store.decision_traces
    eng2.state_read_fn = store.quant_state
    assert eng2.restore_persistent_state() == {
        "traces": 0, "gates": 0, "risk": False, "drift": False}
    assert list(eng2.decision_traces) == [] and list(eng2.gate_log) == []
    assert eng2.risk.snapshot() == {"accounts": {}}


# ---------------------------------------------------------------------------
# 6. 无 code 的 gate 记录 status 不能落成字符串 "None"（Lead 已修复）
#    修复：storage.save_decision_trace 里 status = str(status or code or "")[:24]
#    真实 _append_gate 的 4 类拦截里 3 类没有 code，均覆盖到。
# ---------------------------------------------------------------------------

def test_gate_without_code_stores_empty_status(make_store):
    store = make_store()
    # quant.py:1395 环境过滤 gate 的真实字段形状：没有 status / code
    store.save_decision_trace(
        {"ts": NOW, "symbol": "BTCUSDT", "direction": "bullish",
         "reason": "环境未通过"}, kind="gate")
    status = store._qall("SELECT status FROM decision_traces")[0][0]
    assert status == ""


def test_gate_status_fallback_matrix(make_store):
    """修复副作用矩阵：gate 有 code 时仍回落 code、有 status 时 status 优先；
    trace 记录走原分支不受影响（status 正常写入、无 status 时为空）。"""
    store = make_store()
    for trace, kind in [
        ({"ts": NOW, "symbol": "GATE-NONE", "reason": "无 code"}, "gate"),
        ({"ts": NOW + 1, "symbol": "GATE-CODE", "code": "cooldown"}, "gate"),
        ({"ts": NOW + 2, "symbol": "GATE-STATUS", "status": "blocked"}, "gate"),
        ({"ts": NOW + 3, "symbol": "GATE-BOTH", "status": "blocked",
          "code": "cooldown"}, "gate"),
        ({"ts": NOW + 4, "symbol": "TRACE-STATUS", "status": "open"}, "trace"),
        ({"ts": NOW + 5, "symbol": "TRACE-NONE"}, "trace"),
    ]:
        store.save_decision_trace(trace, kind=kind)

    rows = dict(store._qall("SELECT symbol, status FROM decision_traces"))
    assert rows["GATE-NONE"] == ""             # 修复点：不再落成 "None"
    assert rows["GATE-CODE"] == "cooldown"     # gate 无 status 时仍回落 code
    assert rows["GATE-STATUS"] == "blocked"
    assert rows["GATE-BOTH"] == "blocked"      # status 优先于 code（与修复前语义一致）
    assert rows["TRACE-STATUS"] == "open"      # trace 分支不受影响
    assert rows["TRACE-NONE"] == ""
    # payload 始终原样，不因 status 列回落逻辑而改变
    payloads = {r[0]: json.loads(r[1]) for r in store._qall(
        "SELECT symbol, payload FROM decision_traces")}
    assert payloads["GATE-NONE"] == {
        "ts": NOW, "symbol": "GATE-NONE", "reason": "无 code"}


# ---------------------------------------------------------------------------
# 7. 小修 lag_p50 死字段：静态回归守卫（main.py 必须真的给 hub.lag_p50 赋值）
# ---------------------------------------------------------------------------

def test_lag_p50_dead_field_is_actually_assigned():
    repo = pathlib.Path(__file__).resolve().parent.parent
    main_src = (repo / "main.py").read_text(encoding="utf-8")
    assert 'hub.lag_p50 = int(lag_stats["p50"])' in main_src, \
        "main.py 必须把 feed.lag_stats() 的 p50 写入 hub.lag_p50（审计死字段）"
    assert "feed_obj.lag_stats()" in main_src
    hub_src = (repo / "server" / "hub.py").read_text(encoding="utf-8")
    assert '"lag_p50": self.lag_p50' in hub_src
