"""storage.py 决策 trace / gate / 引擎状态持久化 + 保留期配置测试。

自包含：SQLite 适配器复制自 tests/test_paper_store.py（不 import 其他测试文件），
只适配 MySQL 专有语法（ENGINE/CHARSET、AUTO_INCREMENT、行内 INDEX、%s 占位符），
建表、索引迁移探测、UPSERT、ORDER BY/LIMIT、保留期 DELETE 都在真实 SQL 引擎上执行；
不读 config.yaml，也不连接生产 MySQL（pymysql.connect 被 monkeypatch 到内存库）。
"""
from __future__ import annotations

import json
import re
import sqlite3

import pytest

import storage
from storage import Storage

DAY = 86_400_000
NOW = 1_800_000_000_000       # 固定"当前时间"（毫秒），断言不受真实时钟影响


class FakeTime:
    """替换 storage.time：ts 生成与保留期窗口都可精确断言。"""

    def __init__(self, now_ms: int = NOW):
        self.now = now_ms

    def time(self) -> float:
        return self.now / 1000.0


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
            sql = f"SELECT name FROM pragma_table_info('{show[1]}') WHERE name='{show[2]}'"
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
    connections = []

    def create(**kwargs):
        conn = SQLConnection()
        connections.append(conn)
        # _init() 里的 _backfill_equity_daily() 会 SELECT 现存 raw 权益表，先建空表。
        conn.db.execute(
            "CREATE TABLE equity(acct TEXT,ts BIGINT,bal DOUBLE,upnl DOUBLE,"
            "PRIMARY KEY(acct,ts))")
        monkeypatch.setattr(storage.pymysql, "connect", lambda **_: conn)
        store = Storage({"user": "isolated-test"}, **kwargs)
        assert store.enabled, "启动建表必须成功（真实 DDL 走通）"
        return store

    yield create
    for conn in connections:
        conn.db.close()


@pytest.fixture
def clock(monkeypatch):
    fake = FakeTime()
    monkeypatch.setattr(storage, "time", fake)
    return fake


def table_names(store) -> set:
    return {row[0] for row in store._conn.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def count(store, table: str) -> int:
    return int(store._qall(f"SELECT COUNT(*) FROM {table}")[0][0])


# ---- 1. 启动建表 / 参数归一化 / 幂等 ----

def test_startup_creates_tables_and_normalizes_retention(make_store):
    s = make_store()
    assert s.enabled is True
    names = table_names(s)
    assert {"decision_traces", "quant_state"} <= names
    assert {"paper_positions", "paper_trades", "hits", "quant_trades",
            "quant_events", "news"} <= names
    assert s.decision_traces() == []
    assert s.quant_state("risk") is None

    # 默认保留期：命中 90 天（长期形态分析）、实盘成交 365、事件 30、新闻 7、trace 30
    assert (s.hit_days, s.hit_ms) == (90, 90 * DAY)
    assert (s.trade_keep_days, s.quant_event_days) == (365, 30)
    assert (s.news_days, s.trace_days, s.paper_keep_days) == (7, 30, 0)

    # 表结构：decision_traces 带自增 id + payload；quant_state 以 name 为主键
    dt_cols = [row[1] for row in s._conn.db.execute("PRAGMA table_info('decision_traces')")]
    assert dt_cols == ["id", "ts", "kind", "symbol", "status", "payload"]
    qs_cols = [row[1] for row in s._conn.db.execute("PRAGMA table_info('quant_state')")]
    assert qs_cols == ["name", "ts", "data"]


def test_init_is_idempotent_and_retention_normalized(make_store):
    s = make_store()
    s._init()   # 重复启动：CREATE TABLE IF NOT EXISTS + 索引探测都必须幂等
    s._init()
    assert {"decision_traces", "quant_state"} <= table_names(s)
    assert s.decision_traces() == [] and s.quant_state("x") is None

    s2 = make_store(hit_days=-5, quant_trade_days=-1, quant_event_days="0",
                    news_days="8", trace_days=None, paper_keep_days=-3)
    assert (s2.hit_days, s2.hit_ms) == (0, 0)
    assert s2.trade_keep_days == 0
    assert s2.quant_event_days == 0
    assert s2.news_days == 8
    assert s2.trace_days == 30       # None 回落到默认 30
    assert s2.paper_keep_days == 0


# ---- 2. 决策 trace / gate 拦截往返 ----

def test_decision_trace_and_gate_round_trip(make_store, clock):
    s = make_store()
    clock.now = NOW
    s.save_decision_trace({
        "ts": NOW - 3000, "symbol": "BTCUSDT", "status": "open",
        "stages": [{"name": "signal", "score": 0.72}],
        "reason": "放量突破", "factors": {"trend": 1, "news": 0.2},
    })
    clock.now = NOW
    s.save_decision_trace({"symbol": "ETHUSDT", "status": "skip",
                           "reason": "胜率不足"})
    # quant.py 实际按位置传 kind：trace_storage_fn(trace, "trace"/"gate")
    s.save_decision_trace({"code": "cooldown", "symbol": "SOLUSDT",
                           "detail": "冷却中"}, "gate")

    # 中文 payload 以 UTF-8 原样落库（ensure_ascii=False），不是转义串
    raw = s._qall("SELECT payload FROM decision_traces WHERE symbol=%s",
                  ("BTCUSDT",))[0][0]
    assert "放量突破" in raw and "stages" in raw

    traces = s.decision_traces()
    assert [t["symbol"] for t in traces] == ["ETHUSDT", "BTCUSDT"]   # newest-first
    latest, older = traces
    assert latest["reason"] == "胜率不足"
    assert latest["savedTs"] == NOW          # ts 缺省用当前时间
    assert latest["kind"] == "trace"
    assert isinstance(latest["id"], int) and latest["id"] > 0
    assert older["savedTs"] == NOW - 3000
    assert older["stages"] == [{"name": "signal", "score": 0.72}]
    assert older["factors"] == {"trend": 1, "news": 0.2}

    # kind 过滤：gate 记录默认不出现在 trace 列表里
    gates = s.decision_traces(kind="gate")
    assert len(gates) == 1
    gate = gates[0]
    assert gate["code"] == "cooldown"
    assert gate["symbol"] == "SOLUSDT"
    assert gate["kind"] == "gate"            # 列上的 kind 作为元数据补齐
    # status 列在 gate 记录里回落到 code；契约只往 payload 补 id/kind/savedTs，
    # 所以 status 这种列级元数据要从列上查
    assert s._qall("SELECT status FROM decision_traces WHERE kind=%s",
                   ("gate",)) == [("cooldown",)]
    assert s.decision_traces(kind="trace") == traces
    assert s.decision_traces(kind="gate", limit=1) == gates
    # quant.py 的 trace_read_fn(24, "trace") / (GATE_MAX_LOG, "gate") 按位置调用
    assert s.decision_traces(1, "gate") == gates


def test_decision_traces_limit_clamped(make_store):
    s = make_store()
    s._qmany("INSERT INTO decision_traces(ts,kind,symbol,status,payload) "
             "VALUES(%s,%s,%s,%s,%s)",
             [(NOW - i, "trace", f"S{i}", "",
               json.dumps({"symbol": f"S{i}"})) for i in range(3)])
    assert [x["symbol"] for x in s.decision_traces()] == ["S0", "S1", "S2"]
    assert [x["symbol"] for x in s.decision_traces(limit=2)] == ["S0", "S1"]
    assert len(s.decision_traces(limit=0)) == 1        # 下界夹到 1
    assert len(s.decision_traces(limit=-5)) == 1
    assert len(s.decision_traces(limit="2")) == 2      # 字符串数字也接受
    assert len(s.decision_traces(limit=10_000)) == 3
    # 上界夹到 1000：塞 1005 行后最多取 1000
    s._qmany("INSERT INTO decision_traces(ts,kind,symbol,status,payload) "
             "VALUES(%s,%s,%s,%s,%s)",
             [(NOW - 10_000 - i, "trace", "B", "", "{}") for i in range(1005)])
    assert len(s.decision_traces(limit=100_000)) == 1000


def test_decision_trace_truncates_columns_and_tolerates_bad_rows(make_store, clock):
    s = make_store()
    clock.now = NOW
    s.save_decision_trace({"symbol": "Z" * 20, "status": "S" * 40},
                          kind="k" * 20)
    kind, symbol, status = s._qall(
        "SELECT kind,symbol,status FROM decision_traces ORDER BY id DESC LIMIT 1")[0]
    assert (len(kind), len(symbol), len(status)) == (12, 16, 24)
    assert kind == ("k" * 20)[:12]

    # 坏 payload / 非 dict payload：不抛异常，仍返回带元数据的条目（不能清空整页）
    s._q("INSERT INTO decision_traces(ts,kind,symbol,status,payload) "
         "VALUES(%s,%s,%s,%s,%s)", (NOW + 1, "trace", "BROKEN", "", "{not-json"))
    s._q("INSERT INTO decision_traces(ts,kind,symbol,status,payload) "
         "VALUES(%s,%s,%s,%s,%s)", (NOW + 2, "trace", "NOTDICT", "", "[1,2,3]"))
    rows = s.decision_traces()
    assert [r["savedTs"] for r in rows] == [NOW + 2, NOW + 1]   # 坏 payload 仍返回元数据
    for row in rows:
        assert isinstance(row["id"], int) and row["kind"] == "trace"
    # 契约只往 payload 补 id/kind/savedTs；symbol 列本身仍有值，排障可直接查
    assert [r[0] for r in s._qall(
        "SELECT symbol FROM decision_traces WHERE kind=%s ORDER BY ts DESC",
        ("trace",))] == ["NOTDICT", "BROKEN"]

    # payload 无法 JSON 化时直接跳过，不抛异常；非 dict 的 trace 忽略
    before = count(s, "decision_traces")
    s.save_decision_trace({"status": "x", "bad": object()})
    assert count(s, "decision_traces") == before
    s.save_decision_trace(None)
    s.save_decision_trace([("a", 1)])
    s.save_decision_trace("text")
    assert count(s, "decision_traces") == before

    # payload 原样返回，id/kind/savedTs 只做 setdefault 补充（payload 原键优先）
    s.save_decision_trace({"ts": NOW + 10, "id": 999, "kind": "custom",
                           "savedTs": 12345, "status": "x"})
    top = s.decision_traces()[0]
    assert (top["id"], top["kind"], top["savedTs"]) == (999, "custom", 12345)


# ---- 3. 引擎 / 风控状态快照 ----

def test_quant_state_round_trip_upsert_and_tolerance(make_store, clock):
    s = make_store()
    clock.now = NOW
    assert s.quant_state("risk") is None

    state = {"dayPnl": -12.5, "consecLoss": 2, "note": "当日回撤限制"}
    s.save_quant_state("risk", state)
    assert s.quant_state("risk") == state
    ts, raw = s._qall("SELECT ts,data FROM quant_state WHERE name=%s", ("risk",))[0]
    assert ts == NOW
    assert raw == json.dumps(state, ensure_ascii=False)   # 中文原样落库

    clock.now = NOW + 5000
    s.save_quant_state("risk", {"dayPnl": 3})             # 同名覆盖（REPLACE）
    assert s.quant_state("risk") == {"dayPnl": 3}
    assert count(s, "quant_state") == 1

    # 空 name 跳过
    s.save_quant_state("", {"x": 1})
    s.save_quant_state(None, {"x": 1})
    assert count(s, "quant_state") == 1
    # name 按列宽截断到 32
    s.save_quant_state("N" * 40, {"ok": 1})
    assert s.quant_state("N" * 32) == {"ok": 1}

    # 坏 JSON / 非 dict JSON 返回 None，不抛
    s._q("REPLACE INTO quant_state(name,ts,data) VALUES(%s,%s,%s)",
         ("bad", 1, "{oops"))
    s._q("REPLACE INTO quant_state(name,ts,data) VALUES(%s,%s,%s)",
         ("arr", 1, "[1,2,3]"))
    assert s.quant_state("bad") is None
    assert s.quant_state("arr") is None
    assert s.quant_state("") is None

    # 序列化失败不落库、不抛
    s.save_quant_state("badobj", {"x": object()})
    assert s.quant_state("badobj") is None


# ---- 4. 保留期清理：小保留天数 vs 0=永久 ----

def test_prune_respects_configured_retention_days(make_store):
    s = make_store(hit_days=1, quant_trade_days=1, quant_event_days=1,
                   news_days=1, trace_days=1)
    old = NOW - 5 * DAY
    fresh = NOW - 1000
    s._q("INSERT INTO hits(ts,symbol,pattern) VALUES(%s,%s,%s)",
         (old, "BTCUSDT", "OLD"))
    s._q("INSERT INTO hits(ts,symbol,pattern) VALUES(%s,%s,%s)",
         (fresh, "BTCUSDT", "NEW"))
    s._q("INSERT INTO quant_trades(ts,symbol) VALUES(%s,%s)", (old, "OLD"))
    s._q("INSERT INTO quant_trades(ts,symbol) VALUES(%s,%s)", (fresh, "NEW"))
    s._q("INSERT INTO quant_events(ts,kind) VALUES(%s,%s)", (old, "OLD"))
    s._q("INSERT INTO quant_events(ts,kind) VALUES(%s,%s)", (fresh, "NEW"))
    s._q("INSERT INTO news(url,ts) VALUES(%s,%s)", ("http://old", old))
    s._q("INSERT INTO news(url,ts) VALUES(%s,%s)", ("http://new", fresh))
    s.save_decision_trace({"ts": old, "symbol": "OLD"})
    s.save_decision_trace({"ts": fresh, "symbol": "NEW"})

    s.prune(NOW)
    assert [r[0] for r in s._qall("SELECT pattern FROM hits")] == ["NEW"]
    assert [r[0] for r in s._qall("SELECT symbol FROM quant_trades")] == ["NEW"]
    assert [r[0] for r in s._qall("SELECT kind FROM quant_events")] == ["NEW"]
    assert [r[0] for r in s._qall("SELECT url FROM news")] == ["http://new"]
    assert [r[0] for r in s._qall("SELECT symbol FROM decision_traces")] == ["NEW"]


def test_prune_zero_days_keeps_rows_forever(make_store):
    s = make_store(hit_days=0, quant_trade_days=0, quant_event_days=0,
                   news_days=0, trace_days=0)
    ancient = NOW - 400 * DAY
    s._q("INSERT INTO hits(ts,symbol) VALUES(%s,%s)", (ancient, "H"))
    s._q("INSERT INTO quant_trades(ts,symbol) VALUES(%s,%s)", (ancient, "T"))
    s._q("INSERT INTO quant_events(ts,kind) VALUES(%s,%s)", (ancient, "E"))
    s._q("INSERT INTO news(url,ts) VALUES(%s,%s)", ("http://ancient", ancient))
    s.save_decision_trace({"ts": ancient, "symbol": "TR"})

    s.prune(NOW)
    assert count(s, "hits") == 1
    assert count(s, "quant_trades") == 1
    assert count(s, "quant_events") == 1
    assert count(s, "news") == 1
    assert count(s, "decision_traces") == 1


def test_prune_zero_days_skips_only_that_table(make_store):
    s = make_store(hit_days=0, quant_trade_days=1, quant_event_days=1,
                   news_days=1, trace_days=1)
    ancient = NOW - 30 * DAY
    s._q("INSERT INTO hits(ts,symbol) VALUES(%s,%s)", (ancient, "H"))
    s._q("INSERT INTO quant_trades(ts,symbol) VALUES(%s,%s)", (ancient, "T"))
    s._q("INSERT INTO quant_events(ts,kind) VALUES(%s,%s)", (ancient, "E"))
    s._q("INSERT INTO news(url,ts) VALUES(%s,%s)", ("http://ancient", ancient))
    s.save_decision_trace({"ts": ancient, "symbol": "TR"})

    s.prune(NOW)
    assert count(s, "hits") == 1          # hit_days=0 → 永久保留
    assert count(s, "quant_trades") == 0
    assert count(s, "quant_events") == 0
    assert count(s, "news") == 0
    assert count(s, "decision_traces") == 0


# ---- 5. hits ts 索引迁移：SQLite/MySQL 两种方言都不能崩 ----

def test_hits_ts_index_migration_is_dialect_safe(make_store):
    s = make_store()   # SQLite 不支持 SHOW INDEX：探测抛异常 → 跳过，_init 不崩
    assert "hits" in table_names(s)
    assert count(s, "hits") == 0

    real_qall, real_q = s._qall, s._q
    calls = {"probe": 0, "alter": 0}

    def probe(sql, args=()):
        if sql.startswith("SHOW INDEX FROM hits"):
            calls["probe"] += 1
            return []
        return real_qall(sql, args)

    def ddl(sql, args=()):
        if sql.startswith("ALTER TABLE hits ADD INDEX idx_hits_ts"):
            calls["alter"] += 1
            raise RuntimeError("sqlite: near ADD: syntax error")  # 方言不支持 → 静默忽略
        return real_q(sql, args)

    s._qall, s._q = probe, ddl
    s._init()          # MySQL 老库分支：探测成功且没有索引 → 尝试补索引
    assert calls == {"probe": 1, "alter": 1}
    assert "hits" in table_names(s)

    def indexed(sql, args=()):
        if sql.startswith("SHOW INDEX FROM hits"):
            return [("hits", 1, "idx_hits_ts")]
        return real_qall(sql, args)

    s._qall = indexed
    s._init()          # 索引已存在 → 不再 ALTER，重复启动幂等
    assert calls["alter"] == 1
    assert "hits" in table_names(s)


# ---- 6. 无持久化模式 ----

def test_disabled_storage_returns_safe_defaults(monkeypatch):
    def forbidden(**_):
        pytest.fail("disabled storage must not connect")
    monkeypatch.setattr(storage.pymysql, "connect", forbidden)
    s = Storage({}, hit_days=90, quant_trade_days=1, trace_days=0,
                paper_keep_days=7)
    assert s.enabled is False
    assert (s.hit_days, s.trade_keep_days, s.trace_days) == (90, 1, 0)

    assert s.save_decision_trace({"status": "x"}) is None
    assert s.decision_traces() == []
    assert s.decision_traces(kind="gate", limit=0) == []
    s.save_quant_state("risk", {"a": 1})
    assert s.quant_state("risk") is None
    s.prune(NOW)       # 不连库、不抛异常
