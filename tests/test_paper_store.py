"""模拟仓（纸上交易）持久化层测试：真实 SQL 跑在隔离的 SQLite 内存库上。

只适配 MySQL 专有语法（ENGINE/CHARSET、AUTO_INCREMENT、行内 INDEX、%s 占位符），
建表、UPSERT、排序、聚合、保留期清理都是真实 SQL，不用 Python 仿写存储逻辑；
不读 config.yaml，也不连接生产 MySQL（pymysql.connect 被 monkeypatch 到内存库）。

适配代码与 tests/test_equity_daily.py 顶部思路一致，复制在此保持自包含，
不改动 test_equity_daily.py。
"""
from __future__ import annotations

import re
import sqlite3

import pytest

import storage
from storage import Storage

DAY = 86_400_000
NOW = 1_800_000_000_000       # 固定"当前时间"（毫秒），断言不受真实时钟影响


class FakeTime:
    """替换 storage.time：ts 生成与 days 过滤窗口都可精确断言。"""

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
        if self.connection.fail and self.connection.fail in sql:
            raise RuntimeError("injected SQL failure")
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
        self.fail = None

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


# ---- 1. 启动建表 + 参数归一化 ----

def test_startup_creates_paper_tables_and_enables(make_store):
    s = make_store()
    assert s.enabled is True
    assert s.paper_keep_days == 0

    def tables():
        return {row[0] for row in s._conn.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}

    assert {"paper_positions", "paper_trades"} <= tables()
    cols = [row[1] for row in s._conn.db.execute("PRAGMA table_info('paper_trades')")]
    assert "recovered" in cols and "trade_key" in cols   # 两个新列都在
    s._init()  # 迁移/重启重复执行必须幂等
    assert {"paper_positions", "paper_trades"} <= tables()
    assert s.paper_positions() == []
    assert s.paper_trades() == []


def test_paper_keep_days_is_normalized(make_store):
    assert make_store(paper_keep_days=-3).paper_keep_days == 0
    assert make_store(paper_keep_days="8").paper_keep_days == 8
    assert make_store(paper_keep_days=7).paper_keep_days == 7


# ---- 2. 未平仓仓位：覆盖 / 顺序 / 删除 / 脏数据 ----

def test_positions_upsert_delete_order_and_bad_json(make_store, clock):
    s = make_store()
    assert s.paper_positions() == []

    clock.now = NOW
    s.save_paper_position({"symbol": "BTCUSDT", "entry": 100, "direction": "long"})
    clock.now = NOW + 1000
    s.save_paper_position({"symbol": "ETHUSDT", "entry": 200})
    clock.now = NOW + 2000
    # 同 symbol 覆盖：只留最新一条，ts 也刷新（恢复时的先后顺序看它）
    s.save_paper_position({"symbol": "BTCUSDT", "entry": 150, "direction": "short"})
    rows = s.paper_positions()
    assert [r["symbol"] for r in rows] == ["ETHUSDT", "BTCUSDT"]
    assert rows[1]["entry"] == 150 and rows[1]["direction"] == "short"
    assert s._qall("SELECT symbol,ts FROM paper_positions ORDER BY ts") == [
        ("ETHUSDT", NOW + 1000), ("BTCUSDT", NOW + 2000)]

    s.delete_paper_position("ETHUSDT")
    s.delete_paper_position("ETHUSDT")  # 幂等
    assert [r["symbol"] for r in s.paper_positions()] == ["BTCUSDT"]

    # 空 symbol / 非 dict 不落库
    s.save_paper_position({})
    s.save_paper_position({"symbol": ""})
    assert [r["symbol"] for r in s.paper_positions()] == ["BTCUSDT"]

    # 坏 JSON / 非 dict JSON 的行跳过，不让一条脏数据拖垮恢复
    s._q("REPLACE INTO paper_positions(symbol,ts,data) VALUES(%s,%s,%s)",
         ("BROKEN", 1, "{not-json"))
    s._q("REPLACE INTO paper_positions(symbol,ts,data) VALUES(%s,%s,%s)",
         ("NOTDICT", 2, "[1,2,3]"))
    assert [r["symbol"] for r in s.paper_positions()] == ["BTCUSDT"]

    # symbol 落列时截断到 VARCHAR(16)
    clock.now = NOW + 3000
    s.save_paper_position({"symbol": "Z" * 20, "entry": 9})
    assert s._qall("SELECT symbol FROM paper_positions WHERE symbol=%s",
                   ("Z" * 16,)) == [("Z" * 16,)]


# ---- 3. 平仓记录：全字段往返 / 截断 / 缺省值 / 倒序 / limit ----

def test_trade_full_field_round_trip(make_store, clock):
    s = make_store()
    clock.now = NOW
    trade = {
        "symbol": "BTCUSDT", "direction": "long",
        "entry": 50000.123456, "exit": 50100.5,
        "sl": 49500.25, "tp": 50500.75,
        "leverage": 10, "margin": 100.0, "qty": 0.02,
        "pnl": 12.3456, "pnlPct": 12.35,
        "reason": "超时离场" * 60,              # 240 字 → 截到 200
        "winrate": 41.7, "score": 72.5, "gap": 120.0,
        "winrateParts": {"baseline": 0.3, "edge": 0.45, "final": 0.66},
        "grade": "A", "pattern": "double_top", "regime": "trend",
        "atrAtEntry": 0.0021, "heldMs": 900_000, "trailing": True,
        "closedAt": NOW, "entryTs": NOW - 900_000,
        "recovered": True,                      # 重启补结算的仓位要能持久化标记
    }
    s.save_paper_trade(trade)
    rows = s.paper_trades()
    assert len(rows) == 1
    r = rows[0]
    assert isinstance(r["id"], int) and r["id"] > 0
    assert (r["symbol"], r["direction"]) == ("BTCUSDT", "long")
    assert (r["entry"], r["exit"], r["sl"], r["tp"]) == (
        50000.123456, 50100.5, 49500.25, 50500.75)
    assert (r["leverage"], r["margin"], r["qty"]) == (10, 100.0, 0.02)
    assert (r["pnl"], r["pnlPct"]) == (12.3456, 12.35)
    assert len(r["reason"]) == 200 and r["reason"] == trade["reason"][:200]
    assert (r["winrate"], r["score"], r["gap"]) == (41.7, 72.5, 120.0)
    assert r["winrateParts"] == trade["winrateParts"]
    assert (r["grade"], r["pattern"], r["regime"]) == ("A", "double_top", "trend")
    assert r["atrAtEntry"] == 0.0021
    assert r["heldMs"] == 900_000
    assert r["trailing"] is True
    assert r["recovered"] is True
    assert (r["closedAt"], r["entryTs"]) == (NOW, NOW - 900_000)

    # 字符串字段按列宽截断；缺省字段安全回落
    s.save_paper_trade({
        "symbol": "S" * 20, "direction": "d" * 10,
        "grade": "G" * 20, "pattern": "P" * 60, "regime": "R" * 20,
        "closedAt": NOW + 1000,
    })
    ordered = s.paper_trades()
    assert [x["symbol"] for x in ordered] == ["S" * 16, "BTCUSDT"]  # 倒序
    r = ordered[0]
    assert (len(r["symbol"]), len(r["direction"]), len(r["grade"]),
            len(r["pattern"]), len(r["regime"])) == (16, 8, 16, 40, 16)
    assert (r["entry"], r["leverage"], r["heldMs"]) == (0, 0, 0)
    assert (r["winrate"], r["score"], r["gap"]) == (0, 0, 0)
    assert (r["reason"], r["winrateParts"], r["trailing"]) == ("", {}, False)
    assert r["recovered"] is False
    assert r["entryTs"] == 0

    # closedAt 缺省时用当前时间
    clock.now = NOW + 5000
    s.save_paper_trade({"symbol": "DEFAULT"})
    assert s.paper_trades(limit=1)[0]["closedAt"] == NOW + 5000


def test_recovered_column_migration_from_old_table(make_store):
    """老库 paper_trades 没有 recovered 列：启动 _init 要补列、历史不能丢。"""
    s = make_store()
    s.save_paper_trade({"symbol": "OLD", "pnl": 1, "closedAt": NOW})
    s._conn.db.execute("ALTER TABLE paper_trades DROP COLUMN recovered")
    assert "recovered" not in [row[1] for row in
                               s._conn.db.execute("PRAGMA table_info('paper_trades')")]
    s._init()                      # 触发老库迁移
    assert "recovered" in [row[1] for row in
                           s._conn.db.execute("PRAGMA table_info('paper_trades')")]
    assert [r["symbol"] for r in s.paper_trades()] == ["OLD"]   # 历史还在
    s.save_paper_trade({"symbol": "NEW", "recovered": True, "closedAt": NOW + 1000})
    assert s.paper_trades()[0]["recovered"] is True


def test_trade_limit_clamped_and_bad_winrate_parts_tolerated(make_store):
    s = make_store()
    for i in range(3):
        s.save_paper_trade({"symbol": f"S{i}", "closedAt": NOW + i * 1000})
    assert [r["symbol"] for r in s.paper_trades()] == ["S2", "S1", "S0"]
    assert [r["symbol"] for r in s.paper_trades(limit=2)] == ["S2", "S1"]
    assert len(s.paper_trades(limit=0)) == 1        # 下界夹到 1
    assert len(s.paper_trades(limit=-5)) == 1
    assert len(s.paper_trades(limit=10_000)) == 3
    # 上界夹到 2000：塞 2050 行后最多只取 2000
    s._qmany("INSERT INTO paper_trades(closed_ts) VALUES(%s)",
             [(NOW - 10_000 - i,) for i in range(2050)])
    assert len(s.paper_trades(limit=100_000)) == 2000

    # winrate_parts 的 JSON 解析失败只丢这个可选字段，不影响整行
    s.save_paper_trade({"symbol": "PARTS", "closedAt": NOW + 9000,
                        "winrateParts": {"baseline": 0.3}})
    row = s.paper_trades(limit=1)[0]
    assert row["winrateParts"] == {"baseline": 0.3}
    s._q("UPDATE paper_trades SET winrate_parts=%s WHERE id=%s",
         ("{oops", row["id"]))
    assert s.paper_trades(limit=1)[0]["winrateParts"] == {}


# ---- 4. 平仓汇总 ----

def test_trade_stats_empty_counts_and_days_filter(make_store, clock):
    s = make_store()
    now = NOW
    clock.now = now
    empty = {"count": 0, "wins": 0, "losses": 0, "timeouts": 0, "pnl": 0}
    assert s.paper_trade_stats() == empty
    assert s.paper_trade_stats(days=7) == empty

    s.save_paper_trade({"symbol": "BTCUSDT", "pnl": 10.5,
                        "reason": "tp", "closedAt": now - 1 * DAY})
    s.save_paper_trade({"symbol": "ETHUSDT", "pnl": -4.25,
                        "reason": "sl", "closedAt": now - 2 * DAY})
    s.save_paper_trade({"symbol": "BTCUSDT", "pnl": 0,
                        "reason": "timeout", "closedAt": now - 3 * DAY})
    s.save_paper_trade({"symbol": "BTCUSDT", "pnl": 6,
                        "reason": "timeout", "closedAt": now - 8 * DAY})

    assert s.paper_trade_stats() == {
        "count": 4, "wins": 2, "losses": 1, "timeouts": 2,
        "pnl": round(10.5 - 4.25 + 0 + 6, 4)}
    assert s.paper_trade_stats(days=7) == {
        "count": 3, "wins": 1, "losses": 1, "timeouts": 1,
        "pnl": round(10.5 - 4.25, 4)}


# ---- 5. 按币种汇总 ----

def test_symbol_stats_groups_and_filters(make_store, clock):
    s = make_store()
    now = NOW
    clock.now = now
    assert s.paper_symbol_stats() == []
    for sym, pnl, reason, ago_days in [
        ("BTCUSDT", 10, "tp", 1),
        ("BTCUSDT", -3, "sl", 2),
        ("BTCUSDT", 0, "timeout", 3),
        ("ETHUSDT", 2.5, "tp", 4),
        ("ETHUSDT", -1, "timeout", 12),   # 超出 7 天窗口
        ("", 5, "tp", 1),                 # 空 symbol 跳过
    ]:
        s.save_paper_trade({"symbol": sym, "pnl": pnl, "reason": reason,
                            "closedAt": now - ago_days * DAY})
    assert s.paper_symbol_stats() == [
        # decided = wins + losses：平局/超时不算输赢，胜率要以它为分母
        {"symbol": "BTCUSDT", "closed": 3, "wins": 1, "losses": 1, "decided": 2,
         "timeouts": 1, "netPnl": 7},
        {"symbol": "ETHUSDT", "closed": 2, "wins": 1, "losses": 1, "decided": 2,
         "timeouts": 1, "netPnl": 1.5},
    ]
    assert s.paper_symbol_stats(days=7) == [
        {"symbol": "BTCUSDT", "closed": 3, "wins": 1, "losses": 1, "decided": 2,
         "timeouts": 1, "netPnl": 7},
        {"symbol": "ETHUSDT", "closed": 1, "wins": 1, "losses": 0, "decided": 1,
         "timeouts": 0, "netPnl": 2.5},
    ]


def test_trade_insert_is_idempotent_per_position(make_store):
    """同一仓位实例（symbol+entryTs）的重试不能写出第二条流水。

    INSERT 已提交但客户端收到异常时，PaperBook 会挂起重试；trade_key 唯一键
    + INSERT IGNORE 把重试变成幂等操作，否则胜率/盈亏统计会翻倍。
    """
    s = make_store()
    trade = {"symbol": "BTCUSDT", "entryTs": NOW - 900_000, "closedAt": NOW,
             "reason": "tp", "pnl": 5.0}
    s.save_paper_trade(trade)
    s.save_paper_trade(dict(trade))                       # 重试：必须被忽略
    assert len(s.paper_trades()) == 1
    assert s.paper_trade_stats()["count"] == 1
    # 不同仓位实例（entryTs 不同）照常各记一条
    s.save_paper_trade({**trade, "entryTs": NOW - 1_800_000, "closedAt": NOW + 1})
    assert len(s.paper_trades()) == 2


def test_paper_trades_filter_by_symbol(make_store):
    """按币查库必须只返回该币，且最新在前。

    线上实测：先取全市场最近 12 条再按币筛，BTC 121 笔里只剩 3 笔，
    Agent 于是答"查不到 5 笔记录"。
    """
    s = make_store()
    # 10 条 ETH 夹在中间，把 BTC 挤出任何"全市场最近 N 条"的窗口
    for i in range(10):
        s.save_paper_trade({"symbol": "ETHUSDT", "pnl": 1, "closedAt": NOW - i * 1000})
    for i in range(6):
        s.save_paper_trade({"symbol": "BTCUSDT", "pnl": i, "closedAt": NOW - (20 + i) * 1000})
    rows = s.paper_trades(5, "btcusdt")
    assert len(rows) == 5
    assert {r["symbol"] for r in rows} == {"BTCUSDT"}
    # 时间倒序：closedAt 越新越靠前
    stamps = [r["closedAt"] for r in rows]
    assert stamps == sorted(stamps, reverse=True)
    # 全量查不受 symbol 影响
    assert len(s.paper_trades(200)) == 16
    # 查不到就是空，不能退化成"所有币"
    assert s.paper_trades(5, "solusdt") == []


# ---- 6. 保留期清理 ----

def test_prune_deletes_old_paper_trades_but_keeps_open_positions(make_store):
    s = make_store(paper_keep_days=7)
    s.save_paper_trade({"symbol": "OLD", "pnl": 1, "closedAt": NOW - 8 * DAY})
    s.save_paper_trade({"symbol": "NEW", "pnl": 2, "closedAt": NOW - 6 * DAY})
    s.save_paper_position({"symbol": "KEEPUSDT", "entry": 5})
    s.prune(NOW)
    assert [r["symbol"] for r in s.paper_trades()] == ["NEW"]
    # 未平仓仓位不随保留期删除：恢复后要由 PaperBook 正常平掉
    assert [p["symbol"] for p in s.paper_positions()] == ["KEEPUSDT"]


def test_prune_keeps_paper_trades_when_keep_days_is_zero(make_store):
    s = make_store()
    s.save_paper_trade({"symbol": "ANCIENT", "pnl": 1,
                        "closedAt": NOW - 400 * DAY})
    s.prune(NOW)
    assert [r["symbol"] for r in s.paper_trades()] == ["ANCIENT"]


# ---- 7. 无持久化模式 ----

def test_disabled_storage_returns_safe_defaults(monkeypatch):
    def forbidden(**_):
        pytest.fail("disabled storage must not connect")
    monkeypatch.setattr(storage.pymysql, "connect", forbidden)
    s = Storage({}, paper_keep_days=7)
    assert s.enabled is False
    assert s.paper_keep_days == 7

    assert s.paper_positions() == []
    assert s.delete_paper_position("BTCUSDT") is None
    s.save_paper_position({"symbol": "BTCUSDT"})
    assert s.paper_positions() == []

    s.save_paper_trade({"symbol": "BTCUSDT", "pnl": 1})
    assert s.paper_trades() == []
    empty = {"count": 0, "wins": 0, "losses": 0, "timeouts": 0, "pnl": 0}
    assert s.paper_trade_stats() == empty
    assert s.paper_trade_stats(days=7) == empty
    assert s.paper_symbol_stats() == []
    assert s.paper_symbol_stats(days=7) == []

    s.prune(NOW)  # 不连库、不抛异常
