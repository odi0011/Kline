"""storage.py 第三批持久化测试：K线归档 / Agent 状态 / 连接超时 / 模拟仓配套列。

自包含：SQLite 适配器复制自 tests/test_paper_store.py（不 import 其他测试文件），
只适配 MySQL 专有语法（ENGINE/CHARSET、AUTO_INCREMENT、行内 INDEX、%s 占位符），
建表、REPLACE、SHOW COLUMNS 迁移、排序/LIMIT、保留期 DELETE 都在真实 SQL 引擎执行；
不读 config.yaml，也不连接生产 MySQL（pymysql.connect 被 monkeypatch 到内存库）。

覆盖：
  1. 启动建表 / 参数归一化 / 幂等
  2. MySQL 连接读写超时 kwargs（默认 30、覆盖 5/7、非法回退 30、0 -> None）
  3. paper_trades 新增 open_seq/settle_bars/settle_approx：往返 / 缺省 / trade_key 两种格式 / 老表迁移
  4. candle_archive：单根 / 批量（坏数据跳过）/ 覆盖 / range 排序范围 limit / 截断 / disabled
  5. agent_state：往返 / 部分行 / 三行全无 -> None / 坏 JSON / 序列化失败 / 异常 -> None
  6. agent_chat：往返升序 / limit 夹取 / 覆盖 / 无 id 跳过 / 坏 data 重建 / 截断
  7. prune：归档与聊天保留期、0 = 永久保留、只清理配置了保留期的表
  8. 无持久化模式全部安全返回
"""
from __future__ import annotations

import json
import re
import sqlite3

import pytest

import storage
from core.candle import Candle
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


def _new_connection() -> SQLConnection:
    """带空 raw 权益表的内存库：_init() 的 _backfill_equity_daily() 要读它。"""
    conn = SQLConnection()
    conn.db.execute(
        "CREATE TABLE equity(acct TEXT,ts BIGINT,bal DOUBLE,upnl DOUBLE,"
        "PRIMARY KEY(acct,ts))")
    return conn


@pytest.fixture
def make_store(monkeypatch):
    connections = []

    def create(**kwargs):
        conn = _new_connection()
        connections.append(conn)
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


def columns(store, table: str) -> list:
    return [row[1] for row in store._conn.db.execute(f"PRAGMA table_info('{table}')")]


def count(store, table: str) -> int:
    return int(store._qall(f"SELECT COUNT(*) FROM {table}")[0][0])


def candle(open_time, open_, high, low, close, volume=1.0, trades=3) -> Candle:
    return Candle(open_time=open_time, open=open_, high=high, low=low,
                  close=close, volume=volume, trades=trades)


# ---- 1. 启动建表 / 参数归一化 / 幂等 ----

def test_startup_creates_new_tables_and_is_idempotent(make_store):
    s = make_store()
    assert s.enabled is True
    names = table_names(s)
    assert {"candle_archive", "agent_state", "agent_chat"} <= names

    assert columns(s, "candle_archive") == [
        "symbol", "interval", "open_time", "open", "high", "low", "close",
        "volume", "trades"]
    assert columns(s, "agent_state") == ["name", "ts", "data"]
    assert columns(s, "agent_chat") == ["msg_id", "ts", "seq", "role", "content", "data"]
    assert {"open_seq", "settle_bars", "settle_approx"} <= set(
        columns(s, "paper_trades"))

    # 重复启动（CREATE TABLE IF NOT EXISTS + 列迁移探测）必须幂等
    s._init()
    s._init()
    assert {"candle_archive", "agent_state", "agent_chat"} <= table_names(s)
    assert s.load_agent_state() is None
    assert s.load_agent_chat() == []
    assert s.archive_range("BTCUSDT", "1m", 0, NOW) == []
    assert s.paper_trades() == []


def test_new_retention_params_default_and_normalized(make_store):
    s = make_store()
    assert (s.candle_archive_days, s.agent_chat_days) == (0, 0)   # 默认永久
    assert make_store(candle_archive_days=-5).candle_archive_days == 0
    assert make_store(agent_chat_days="9").agent_chat_days == 9
    assert make_store(candle_archive_days="bad").candle_archive_days == 0
    assert make_store(agent_chat_days=None).agent_chat_days == 0
    assert make_store(candle_archive_days=30, agent_chat_days=7).candle_archive_days == 30


# ---- 2. MySQL 连接读写超时 ----

def test_mysql_connect_timeout_kwargs(monkeypatch):
    captured = {}
    connections = []

    def fake_connect(**kwargs):
        captured.clear()
        captured.update(kwargs)
        conn = _new_connection()
        connections.append(conn)
        return conn

    monkeypatch.setattr(storage.pymysql, "connect", fake_connect)
    try:
        # 默认 30 秒
        Storage({"user": "isolated"})
        assert (captured["read_timeout"], captured["write_timeout"]) == (30, 30)

        # 显式覆盖（数字/数字字符串都接受）
        Storage({"user": "isolated", "read_timeout": 5, "write_timeout": "7"})
        assert (captured["read_timeout"], captured["write_timeout"]) == (5, 7)

        # 非法值回退 30
        Storage({"user": "isolated", "read_timeout": "abc", "write_timeout": "12s"})
        assert (captured["read_timeout"], captured["write_timeout"]) == (30, 30)

        # 0 -> None（不设超时）
        Storage({"user": "isolated", "read_timeout": 0, "write_timeout": 0})
        assert (captured["read_timeout"], captured["write_timeout"]) == (None, None)

        # 负数属于非法值 -> 回退 30（pymysql 不接受负超时）
        Storage({"user": "isolated", "read_timeout": -5, "write_timeout": "-1"})
        assert (captured["read_timeout"], captured["write_timeout"]) == (30, 30)
    finally:
        for conn in connections:
            conn.db.close()


# ---- 3. paper_trades 新增三列 ----

def test_paper_trade_new_columns_round_trip(make_store, clock):
    s = make_store()
    clock.now = NOW
    s.save_paper_trade({
        "symbol": "BTCUSDT", "entryTs": NOW - 60_000, "closedAt": NOW,
        "openSeq": 3, "settleBars": 12, "settleApprox": True,
        "reason": "timeout", "pnl": 1.5,
        "winrateParts": {"baseline": 0.3},
    })
    row = s.paper_trades()[0]
    assert (row["openSeq"], row["settleBars"], row["settleApprox"]) == (3, 12, 1)
    assert row["winrateParts"] == {"baseline": 0.3}       # 老字段不受列序变化影响
    assert s._qall(
        "SELECT open_seq,settle_bars,settle_approx FROM paper_trades") == [(3, 12, 1)]

    # 缺省为 0
    s.save_paper_trade({"symbol": "ETHUSDT", "closedAt": NOW + 1000})
    row = s.paper_trades()[0]
    assert (row["openSeq"], row["settleBars"], row["settleApprox"]) == (0, 0, 0)

    # 脏值安全回落
    s.save_paper_trade({"symbol": "SOLUSDT", "closedAt": NOW + 2000,
                        "openSeq": "bad", "settleBars": None,
                        "settleApprox": "x"})
    row = s.paper_trades()[0]
    assert (row["openSeq"], row["settleBars"], row["settleApprox"]) == (0, 0, 0)


def test_trade_key_formats_and_open_seq_idempotency(make_store):
    s = make_store()
    entry = NOW - 5000
    # openSeq>0：幂等键带第三段，同毫秒不同开仓序号是不同的仓位实例
    s.save_paper_trade({"symbol": "BTCUSDT", "entryTs": entry, "openSeq": 7,
                        "closedAt": NOW, "pnl": 1})
    assert s._qall("SELECT trade_key FROM paper_trades") == [
        (f"BTCUSDT:{entry}:7",)]
    s.save_paper_trade({"symbol": "BTCUSDT", "entryTs": entry, "openSeq": 8,
                        "closedAt": NOW + 1, "pnl": 2})
    assert len(s.paper_trades()) == 2
    # 同一仓位实例重试：INSERT IGNORE 幂等，不会写出第二条
    s.save_paper_trade({"symbol": "BTCUSDT", "entryTs": entry, "openSeq": 7,
                        "closedAt": NOW + 2, "pnl": 3})
    assert len(s.paper_trades()) == 2
    # 无 openSeq 的老格式与带序号的新格式互不冲突
    s.save_paper_trade({"symbol": "BTCUSDT", "entryTs": entry,
                        "closedAt": NOW + 3, "pnl": 4})
    assert sorted(r[0] for r in s._qall("SELECT trade_key FROM paper_trades")) == [
        f"BTCUSDT:{entry}", f"BTCUSDT:{entry}:7", f"BTCUSDT:{entry}:8"]
    # entryTs<=0 没有幂等键（NULL 可多行）
    s.save_paper_trade({"symbol": "SOLUSDT", "closedAt": NOW + 4})
    s.save_paper_trade({"symbol": "SOLUSDT", "closedAt": NOW + 5})
    assert s._qall(
        "SELECT trade_key FROM paper_trades WHERE symbol=%s ORDER BY id",
        ("SOLUSDT",)) == [(None,), (None,)]


def test_paper_trade_new_columns_migrate_from_old_table(make_store):
    """老库 paper_trades 没有 open_seq/settle_bars/settle_approx：_init 逐个补列。"""
    s = make_store()
    s.save_paper_trade({"symbol": "OLD", "entryTs": 111, "closedAt": NOW, "pnl": 1})
    for column in ("open_seq", "settle_bars", "settle_approx"):
        s._conn.db.execute(f"ALTER TABLE paper_trades DROP COLUMN {column}")
    assert not ({"open_seq", "settle_bars", "settle_approx"} & set(
        columns(s, "paper_trades")))

    s._init()                     # 触发老库迁移
    s._init()                     # 迁移必须幂等
    assert {"open_seq", "settle_bars", "settle_approx"} <= set(
        columns(s, "paper_trades"))
    assert [r["symbol"] for r in s.paper_trades()] == ["OLD"]     # 历史还在

    s.save_paper_trade({"symbol": "NEW", "entryTs": 222, "closedAt": NOW + 1000,
                        "openSeq": 2, "settleBars": 3, "settleApprox": True})
    row = s.paper_trades()[0]
    assert row["symbol"] == "NEW"
    assert (row["openSeq"], row["settleBars"], row["settleApprox"]) == (2, 3, 1)


# ---- 4. K线长期归档 ----

def test_archive_candle_round_trip_and_overwrite(make_store, clock):
    s = make_store()
    clock.now = NOW
    s.archive_candle("BTCUSDT", "1m",
                     candle(60_000, 100.5, 101.25, 99.75, 100.0, 12.5, 7))
    assert s.archive_range("BTCUSDT", "1m", 0, NOW) == [{
        "t": 60_000, "o": 100.5, "h": 101.25, "l": 99.75,
        "c": 100.0, "v": 12.5, "trades": 7}]
    # 同主键 REPLACE 覆盖，不新增行
    s.archive_candle("BTCUSDT", "1m", candle(60_000, 1.0, 2.0, 0.5, 1.5, 0.0, 0))
    rows = s.archive_range("BTCUSDT", "1m", 0, NOW)
    assert len(rows) == 1
    assert (rows[0]["o"], rows[0]["h"], rows[0]["l"], rows[0]["c"]) == (
        1.0, 2.0, 0.5, 1.5)
    assert count(s, "candle_archive") == 1


def test_archive_candles_batch_skips_bad_data(make_store):
    s = make_store()
    assert s.archive_candles("BTCUSDT", "1m", []) == 0
    assert s.archive_candles("", "1m", [candle(1000, 1, 1, 1, 1)]) == 0
    assert s.archive_candles("BTCUSDT", "", [candle(1000, 1, 1, 1, 1)]) == 0

    payload = [
        candle(10_000, 1.0, 2.0, 0.5, 1.5),
        None,                                       # 空对象跳过
        {"open_time": 20_000},                      # dict 没有属性，跳过
        candle(0, 1, 1, 1, 1),                      # open_time<=0 跳过
        candle("bad", 1, 1, 1, 1),                  # 坏 open_time 跳过
        candle(30_000, 3.0, 4.0, 2.5, 3.5, volume=None),   # 脏可选值回落 0
        candle(40_000, 5.0, 6.0, 4.5, 5.5),
    ]
    assert s.archive_candles("BTCUSDT", "1m", payload) == 3
    assert count(s, "candle_archive") == 3
    rows = s.archive_range("BTCUSDT", "1m", 0, NOW)
    assert [r["t"] for r in rows] == [10_000, 30_000, 40_000]
    assert rows[1]["v"] == 0.0
    # 逐根写入与批量写入等价（同主键覆盖）
    s.archive_candle("BTCUSDT", "1m", candle(40_000, 9.0, 9.0, 9.0, 9.0))
    assert count(s, "candle_archive") == 3


def test_archive_range_orders_filters_and_limits(make_store):
    s = make_store()
    for t in (5_000, 1_000, 3_000):
        s.archive_candle("BTCUSDT", "1m", candle(t, float(t), float(t),
                                                 float(t), float(t)))
    s.archive_candle("ETHUSDT", "1m", candle(2_000, 1.0, 1.0, 1.0, 1.0))
    s.archive_candle("BTCUSDT", "5m", candle(2_500, 1.0, 1.0, 1.0, 1.0))

    # 升序 + 时间范围含两端 + 按 symbol/interval 过滤
    assert [r["t"] for r in s.archive_range("BTCUSDT", "1m", 1_000, 5_000)] == [
        1_000, 3_000, 5_000]
    assert [r["t"] for r in s.archive_range("BTCUSDT", "1m", 2_000, 4_000)] == [3_000]
    assert [r["t"] for r in s.archive_range("BTCUSDT", "1m", 0, 10_000, limit=2)] == [
        1_000, 3_000]
    assert s.archive_range("BTCUSDT", "1m", 10_000, 20_000) == []

    # limit 夹取：1..200000（不真插 20 万行，直接看下传的 LIMIT 参数）
    captured = {}
    real_qall = s._qall

    def spy(sql, args=()):
        captured["limit"] = args[-1]
        return []

    s._qall = spy
    try:
        s.archive_range("BTCUSDT", "1m", 0, NOW, limit=10 ** 9)
        assert captured["limit"] == 200_000
        s.archive_range("BTCUSDT", "1m", 0, NOW, limit=0)
        assert captured["limit"] == 1
        s.archive_range("BTCUSDT", "1m", 0, NOW, limit=-5)
        assert captured["limit"] == 1
        s.archive_range("BTCUSDT", "1m", 0, NOW, limit="bad")
        assert captured["limit"] == 20_000      # 非法值回落默认
    finally:
        s._qall = real_qall


def test_archive_truncates_symbol_and_interval(make_store):
    s = make_store()
    s.archive_candle("Z" * 20, "I" * 12, candle(NOW - 1000, 1.0, 1.0, 1.0, 1.0))
    assert s._qall("SELECT symbol,`interval` FROM candle_archive") == [
        ("Z" * 16, "I" * 8)]
    # 用超长入参查询也能命中按列宽截断后的行
    assert s.archive_range("Z" * 20, "I" * 12, 0, NOW)[0]["t"] == NOW - 1000


# ---- 5. Agent 状态 ----

def test_agent_state_round_trip_and_overwrite(make_store, clock):
    s = make_store()
    clock.now = NOW
    tasks = [{"id": "t1", "title": "复盘今日行情", "done": False}]
    memory = {"note": "记忆", "count": 3, "nested": {"a": [1, 2]}}
    summary = "聊天摘要" * 1500                    # 6000 字 -> 截到 4000
    s.save_agent_state(tasks, memory, summary)

    state = s.load_agent_state()
    assert state["tasks"] == tasks
    assert state["memory"] == memory
    assert state["chatSummary"] == summary[:4000]
    assert count(s, "agent_state") == 3            # 三行：tasks/memory/meta

    # meta 带版本号；中文原样落库（ensure_ascii=False）
    meta_raw = s._qall("SELECT data FROM agent_state WHERE name=%s", ("meta",))[0][0]
    assert json.loads(meta_raw) == {"version": 2, "chatSummary": summary[:4000]}
    tasks_raw = s._qall("SELECT data FROM agent_state WHERE name=%s", ("tasks",))[0][0]
    assert "复盘今日行情" in tasks_raw

    # REPLACE 覆盖：同名行不新增
    s.save_agent_state([], {}, "")
    assert count(s, "agent_state") == 3
    assert s.load_agent_state() == {"tasks": [], "memory": {}, "chatSummary": ""}


def test_agent_state_partial_rows_empty_and_bad_json(make_store):
    s = make_store()
    assert s.load_agent_state() is None            # 三行全无 -> None

    # 只有部分行：缺的用空结构补
    s._q("REPLACE INTO agent_state(name,ts,data) VALUES(%s,%s,%s)",
         ("memory", NOW, json.dumps({"n": 1})))
    assert s.load_agent_state() == {"tasks": [], "memory": {"n": 1}, "chatSummary": ""}
    s._q("REPLACE INTO agent_state(name,ts,data) VALUES(%s,%s,%s)",
         ("tasks", NOW, json.dumps([{"id": 1}])))
    state = s.load_agent_state()
    assert state["tasks"] == [{"id": 1}] and state["memory"] == {"n": 1}

    # 只有未知 name：tasks/memory/meta 全无 -> None
    s._q("DELETE FROM agent_state")
    s._q("REPLACE INTO agent_state(name,ts,data) VALUES(%s,%s,%s)",
         ("other", NOW, "{}"))
    assert s.load_agent_state() is None

    # 坏 JSON / 类型不对 / null 都按空结构处理
    s._q("REPLACE INTO agent_state(name,ts,data) VALUES(%s,%s,%s)",
         ("tasks", NOW, "{oops"))
    s._q("REPLACE INTO agent_state(name,ts,data) VALUES(%s,%s,%s)",
         ("memory", NOW, "[1,2,3]"))
    s._q("REPLACE INTO agent_state(name,ts,data) VALUES(%s,%s,%s)",
         ("meta", NOW, "null"))
    assert s.load_agent_state() == {"tasks": [], "memory": {}, "chatSummary": ""}

    # 序列化失败：回落到空结构，仍能整体落库/恢复
    s.save_agent_state([object()], {"bad": object()}, "摘要")
    state = s.load_agent_state()
    assert state["tasks"] == [] and state["memory"] == {}
    assert state["chatSummary"] == "摘要"


def test_agent_state_returns_none_when_db_errors(make_store):
    s = make_store()

    def boom(sql, args=()):
        raise RuntimeError("db down")

    s._qall = boom
    assert s.load_agent_state() is None


# ---- 6. Agent 聊天归档 ----

def test_agent_chat_round_trip_order_limit_and_overwrite(make_store, clock):
    s = make_store()
    clock.now = NOW
    messages = [
        {"id": "m3", "ts": NOW + 3000, "role": "assistant", "content": "三",
         "kind": "text"},
        {"id": "m1", "ts": NOW + 1000, "role": "user", "content": "一"},
        {"id": "m2", "ts": NOW + 2000, "role": "assistant", "content": "二"},
    ]
    assert s.save_agent_chat(messages) == 3
    chat = s.load_agent_chat()
    assert [m["id"] for m in chat] == ["m1", "m2", "m3"]        # 时间升序
    assert chat[0]["content"] == "一" and chat[0]["role"] == "user"
    assert chat[2]["kind"] == "text"                            # 完整消息往返
    # 先取最新 limit 条，再反转为升序
    assert [m["id"] for m in s.load_agent_chat(limit=2)] == ["m2", "m3"]
    assert [m["id"] for m in s.load_agent_chat(limit=0)] == ["m3"]   # 下界夹到 1

    # 同 msg_id 覆盖（不新增行）
    s.save_agent_chat([{"id": "m1", "ts": NOW + 1000, "role": "user",
                        "content": "改"}])
    assert count(s, "agent_chat") == 3
    assert s.load_agent_chat()[0]["content"] == "改"

    # 无 id / 非 dict 跳过；空列表返回 0
    assert s.save_agent_chat([
        {"role": "user", "content": "无 id"},
        {"id": None, "content": "None id"},
        {"id": "", "content": "空 id"},
        "notadict", None,
    ]) == 0
    assert s.save_agent_chat([]) == 0
    assert count(s, "agent_chat") == 3

    # limit 上界夹到 500（看下传参数，不真插 500 行）
    captured = {}
    real_qall = s._qall

    def spy(sql, args=()):
        captured["limit"] = args[-1]
        return []

    s._qall = spy
    try:
        s.load_agent_chat(limit=10_000)
        assert captured["limit"] == 500
        s.load_agent_chat(limit="bad")
        assert captured["limit"] == 40
    finally:
        s._qall = real_qall


def test_agent_chat_same_ms_keeps_write_order(make_store):
    """同一毫秒的 user/assistant 消息必须保持写入顺序（seq 列），否则重启后颠倒。"""
    s = make_store()
    assert s.save_agent_chat([
        {"id": "zzzz-user", "ts": NOW, "role": "user", "content": "先问"},
        {"id": "aaaa-assistant", "ts": NOW, "role": "assistant", "content": "后答"},
    ]) == 2
    assert [m["role"] for m in s.load_agent_chat()] == ["user", "assistant"]
    # 后续保存时窗口整体重排，同一毫秒内的相对顺序仍由 seq 决定
    s.save_agent_chat([
        {"id": "aaaa-assistant", "ts": NOW, "role": "assistant", "content": "后答"},
        {"id": "zzzz-user", "ts": NOW, "role": "user", "content": "先问"},
    ])
    assert [m["role"] for m in s.load_agent_chat()] == ["assistant", "user"]


def test_agent_chat_clear_and_trim(make_store):
    """清空/压缩聊天时 trim_agent_chat 必须删掉窗口外消息，否则重启复活。"""
    s = make_store()
    s.save_agent_chat([{"id": "m%d" % i, "ts": NOW + i, "role": "user",
                        "content": "c%d" % i} for i in range(5)])
    assert s.trim_agent_chat(["m3", "m4"]) == 3
    assert [m["id"] for m in s.load_agent_chat()] == ["m3", "m4"]
    assert s.trim_agent_chat([]) == 0          # 空列表 = 清空
    assert s.load_agent_chat() == []
    s.save_agent_chat([{"id": "x", "ts": NOW, "role": "user", "content": "x"}])
    s.clear_agent_chat()
    assert s.load_agent_chat() == []

def test_agent_chat_truncates_columns_and_defaults_ts(make_store, clock):
    s = make_store()
    clock.now = NOW + 9000
    assert s.save_agent_chat([{"id": "Z" * 80, "ts": None, "role": "R" * 40,
                               "content": "x" * 200_010}]) == 1
    msg_id, ts, role, length = s._qall(
        "SELECT msg_id,ts,role,length(content) FROM agent_chat")[0]
    assert msg_id == "Z" * 64            # msg_id 截 64
    assert ts == NOW + 9000              # ts 缺省用当前时间
    assert role == "R" * 16              # role 截 16
    assert length == 200_000             # content 截 200000


def test_agent_chat_rebuilds_bad_data_rows(make_store):
    s = make_store()
    s.save_agent_chat([{"id": "ok", "ts": NOW, "role": "user", "content": "好"}])
    s._q("REPLACE INTO agent_chat(msg_id,ts,role,content,data) "
         "VALUES(%s,%s,%s,%s,%s)", ("bad", NOW + 1, "user", "坏", "{oops"))
    s._q("REPLACE INTO agent_chat(msg_id,ts,role,content,data) "
         "VALUES(%s,%s,%s,%s,%s)", ("arr", NOW + 2, "assistant", "数组", "[1,2,3]"))
    chat = s.load_agent_chat()
    assert [m["id"] for m in chat] == ["ok", "bad", "arr"]
    assert chat[1] == {"id": "bad", "role": "user", "content": "坏"}
    assert chat[2] == {"id": "arr", "role": "assistant", "content": "数组"}

    # 序列化失败的消息回落成 {id,role,content}，仍可保存并恢复
    assert s.save_agent_chat([{"id": "obj", "ts": NOW + 3, "role": "user",
                               "content": "c", "extra": object()}]) == 1
    loaded = {m["id"]: m for m in s.load_agent_chat()}
    assert loaded["obj"] == {"id": "obj", "role": "user", "content": "c"}


# ---- 7. 保留期清理 ----

def test_prune_archive_and_chat_retention(make_store):
    s = make_store(candle_archive_days=7, agent_chat_days=30)
    old_candle = NOW - 8 * DAY
    new_candle = NOW - DAY
    s.archive_candle("BTCUSDT", "1m", candle(old_candle, 1.0, 1.0, 1.0, 1.0))
    s.archive_candle("BTCUSDT", "1m", candle(new_candle, 2.0, 2.0, 2.0, 2.0))
    s.save_agent_chat([
        {"id": "old", "ts": NOW - 40 * DAY, "role": "user", "content": "旧"},
        {"id": "new", "ts": NOW - DAY, "role": "assistant", "content": "新"},
    ])
    s.save_agent_state([{"id": "keep"}], {"m": 1}, "摘要")

    s.prune(NOW)
    assert [r["t"] for r in s.archive_range("BTCUSDT", "1m", 0, NOW)] == [
        new_candle]
    assert [m["id"] for m in s.load_agent_chat()] == ["new"]
    # 状态快照不属于保留期清理范围
    assert s.load_agent_state()["tasks"] == [{"id": "keep"}]


def test_prune_zero_retention_keeps_archive_and_chat_forever(make_store):
    s = make_store()                       # 默认 0 = 永久保留
    ancient = NOW - 400 * DAY
    s.archive_candle("BTCUSDT", "1m", candle(ancient, 1.0, 1.0, 1.0, 1.0))
    s.save_agent_chat([{"id": "ancient", "ts": ancient, "role": "user",
                        "content": "老"}])
    s.prune(NOW)
    assert len(s.archive_range("BTCUSDT", "1m", 0, NOW)) == 1
    assert [m["id"] for m in s.load_agent_chat()] == ["ancient"]


def test_prune_zero_retention_skips_only_that_table(make_store):
    s = make_store(candle_archive_days=0, agent_chat_days=1)
    ancient = NOW - 30 * DAY
    s.archive_candle("BTCUSDT", "1m", candle(ancient, 1.0, 1.0, 1.0, 1.0))
    s.save_agent_chat([{"id": "ancient", "ts": ancient, "role": "user",
                        "content": "老"}])
    s.prune(NOW)
    assert len(s.archive_range("BTCUSDT", "1m", 0, NOW)) == 1   # 0=永久
    assert s.load_agent_chat() == []                            # 1 天照删


# ---- 8. 无持久化模式 ----

def test_disabled_storage_returns_safe_defaults(monkeypatch):
    def forbidden(**_):
        pytest.fail("disabled storage must not connect")

    monkeypatch.setattr(storage.pymysql, "connect", forbidden)
    s = Storage({}, candle_archive_days=7, agent_chat_days=7)
    assert s.enabled is False
    assert (s.candle_archive_days, s.agent_chat_days) == (7, 7)

    assert s.archive_candle("BTCUSDT", "1m", candle(1, 1, 1, 1, 1)) is None
    assert s.archive_candles("BTCUSDT", "1m", [candle(1, 1, 1, 1, 1)]) == 0
    assert s.archive_range("BTCUSDT", "1m", 0, NOW) == []

    s.save_agent_state([{"id": 1}], {"a": 1}, "摘要")
    assert s.load_agent_state() is None
    assert s.save_agent_chat([{"id": "m", "ts": 1, "role": "user",
                               "content": "x"}]) == 0
    assert s.load_agent_chat() == []

    s.prune(NOW)       # 不连库、不抛异常
    assert s.paper_trades() == []
