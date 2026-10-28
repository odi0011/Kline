"""Execute storage SQL against an isolated SQLite database, never config/production.

Only MySQL DDL/placeholder/INSERT IGNORE syntax is adapted; the real daily SELECT,
CASE UPDATE, joins, conflict resolution and transactions execute in the SQL engine.
No Python imitation of daily aggregation/upsert is used. This is not a MySQL server
integration test; keep UPDATE endpoint assignments ahead of timestamp assignments.
"""
from __future__ import annotations

import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

import storage
from storage import Storage

DAY = 86_400_000


def ts(date):
    return int(datetime.fromisoformat(date).replace(tzinfo=timezone.utc).timestamp() * 1000)


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
            raise RuntimeError("injected transaction failure")
        sql = re.sub(r"ENGINE=InnoDB DEFAULT CHARSET=utf8mb4", "", sql)
        sql = sql.replace("BIGINT AUTO_INCREMENT PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
        sql = re.sub(r",\s*INDEX \w+\([^)]*\)", "", sql)
        show = re.fullmatch(r"SHOW COLUMNS FROM (\w+) LIKE '(\w+)'", sql)
        if show:
            sql = f"SELECT name FROM pragma_table_info('{show[1]}') WHERE name='{show[2]}'"
        sql = sql.replace("INSERT IGNORE", "INSERT OR IGNORE").replace("%s", "?")
        return self.cursor.execute(sql, args)

    def fetchall(self):
        return self.cursor.fetchall()


class SQLConnection:
    def __init__(self):
        self.db = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
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

    def create(raw=()):
        conn = SQLConnection()
        connections.append(conn)
        conn.db.execute("CREATE TABLE equity(acct TEXT,ts BIGINT,bal DOUBLE,upnl DOUBLE,PRIMARY KEY(acct,ts))")
        conn.db.executemany("INSERT INTO equity VALUES(?,?,?,?)", raw)
        monkeypatch.setattr(storage.pymysql, "connect", lambda **_: conn)
        result = Storage({"user": "isolated-test"})
        assert result.enabled, "the real startup DDL/backfill must complete"
        return result

    yield create
    for conn in connections:
        conn.db.close()


def test_startup_backfills_all_existing_raw_in_utc_and_only_real_days(make_store):
    d = ts("2024-02-28")
    s = make_store([
        ("a", d + DAY + 50, 50, 2),
        ("a", d + DAY - 1, 90, -10),
        ("a", d + 20, 100, 20),
        ("a", d + 10, 100, 5),
        ("a", d + DAY, 40, -2),
        ("a", d + 3 * DAY, 70, 0),
        ("other", d, 900, 90),
    ])
    records = s.equity_heatmap("a")
    assert [r["date"] for r in records] == ["2024-02-28", "2024-02-29", "2024-03-02"]
    assert records[0] == {
        "date": "2024-02-28", "firstTs": d + 10, "lastTs": d + DAY - 1,
        "open": 100, "close": 90, "high": 100, "low": 90,
    }
    assert records[1]["open"] == 40
    assert records[1]["close"] == 50
    assert s.equity_heatmap("other")[0]["close"] == 900
    assert s.equity_heatmap("absent") == []
    s._init()
    assert s.equity_heatmap("a") == records  # repeated startup is idempotent


def test_account_total_does_not_double_count_unrealized(make_store, monkeypatch):
    import accounts
    monkeypatch.setattr(accounts, "signed_request", lambda *args: [{"asset": "USDT", "balance": "100", "availableBalance": "80"}])
    monkeypatch.setattr(accounts, "fetch_positions", lambda *args: [{"unrealized": 10}])
    account = accounts.fetch_account("test", "test")
    store = make_store()
    store.save_equity("a", account["ts"], account["balance"], account["unrealized"])
    assert store.equity_heatmap("a")[0]["close"] == account["balance"] == 110


def test_live_samples_out_of_order_replace_equal_and_total_equity(make_store):
    s = make_store()
    d = ts("2025-01-01")
    for t, bal, upnl in [(20, 110, 10), (40, 80, -20), (30, 150, 50), (10, 60, -40)]:
        s.save_equity("a", d + t, bal, upnl)
    s.save_equity("a", d + 40, 90, -10)
    s.save_equity("a", d + 10, 70, -30)
    s.save_equity("a", d + DAY, 0, 0)
    s.save_equity("a", d + 2 * DAY, -5, -10)
    rows = s.equity_heatmap("a")
    assert rows[0] == {
        "date": "2025-01-01", "firstTs": d + 10, "lastTs": d + 40,
        "open": 70, "close": 90, "high": 150, "low": 60,
    }
    assert rows[1]["close"] == 0  # zero is not missing
    assert rows[2]["close"] == -5
    assert s._qall("SELECT bal,upnl FROM equity WHERE acct=%s AND ts=%s", ("a", d + 40)) == [(90, -10)]


def test_restart_partial_raw_never_shrinks_archive_or_replaces_newer_endpoint(make_store):
    s = make_store()
    d = ts("2024-01-01")
    for t, value in [(1, 10), (2, 200), (3, -10), (4, 100)]:
        s.save_equity("a", d + t, value, 0)
    before = s.equity_heatmap("a")
    s._q("DELETE FROM equity WHERE ts<%s", (d + 4,))
    # A stale duplicate must not replace a newer observation already archived.
    s._q("REPLACE INTO equity VALUES(%s,%s,%s,%s)", ("a", d + 4, 50, 0))
    s._init()
    assert s.equity_heatmap("a") == before


def test_backfill_uses_same_precision_as_live_total(make_store):
    s = make_store()
    s.save_equity("a", ts("2025-01-01"), 0.1, 0.2)
    before = s.equity_heatmap("a")
    s._init()
    assert s.equity_heatmap("a") == before
    assert before[0]["open"] == before[0]["close"] == before[0]["high"] == before[0]["low"] == 0.1


def test_prune_keeps_daily_permanently_and_raw_exact_two_day_boundary(make_store):
    s = make_store()
    now = ts("2026-05-01")
    for t in [now - 400 * DAY, now - 2 * DAY - 1, now - 2 * DAY, now]:
        s.save_equity("a", t, 100, 20)
    before = s.equity_heatmap("a")
    s.prune(now)
    assert s._qall("SELECT ts FROM equity ORDER BY ts") == [(now - 2 * DAY,), (now,)]
    assert s.equity_heatmap("a") == before
    s._init()
    assert s.equity_heatmap("a") == before


def test_migration_merges_archive_and_preserves_target_conflicts(make_store):
    s = make_store()
    d = ts("2024-01-01")
    s.save_equity("old", d - DAY, 10, 1)
    s.save_equity("old", d + 10, 40, 5)
    s.save_equity("old", d + 20, 80, 5)
    s.save_equity("old", d + 30, 30, 5)
    s.save_equity("stable", d + 10, 60, 1)
    s.save_equity("stable", d + 30, 70, 1)
    s.save_equity("stable", d + 40, 90, 1)
    s._q("DELETE FROM equity WHERE ts<%s", (d,))  # daily-only history survives
    assert s.migrate_equity(["", "stable", "old", "old"], "stable") == 3
    assert s.equity_heatmap("old") == []
    assert s._qall("SELECT ts FROM equity WHERE acct=%s", ("old",)) == []
    records = s.equity_heatmap("stable")
    assert records[0]["close"] == 10
    assert records[1] == {
        "date": "2024-01-01", "firstTs": d + 10, "lastTs": d + 40,
        "open": 60, "close": 90, "high": 90, "low": 30,
    }
    assert s._qall("SELECT bal,upnl FROM equity WHERE acct=%s AND ts=%s", ("stable", d + 10)) == [(60, 1)]
    assert s._qall("SELECT bal,upnl FROM equity WHERE acct=%s AND ts=%s", ("stable", d + 30)) == [(70, 1)]
    assert s.migrate_equity(["old"], "stable") == 0
    s._init()
    assert s.equity_heatmap("stable") == records


def test_migration_equal_endpoints_keep_stable_but_can_extend_both_ends(make_store):
    s = make_store()
    d = ts("2025-01-01")
    for acct, points in [("old", [(20, 2), (30, 3)]), ("new", [(20, 20), (30, 30)])]:
        for t, value in points:
            s.save_equity(acct, d + t, value, 0)
    s.migrate_equity(["old"], "new")
    row = s.equity_heatmap("new")[0]
    assert (row["open"], row["close"]) == (20, 30)
    s.save_equity("older-name", d + 10, 1, 0)
    s.save_equity("older-name", d + 40, 4, 0)
    s.migrate_equity(["older-name"], "new")
    row = s.equity_heatmap("new")[0]
    assert (row["firstTs"], row["lastTs"], row["open"], row["close"]) == (d + 10, d + 40, 1, 4)


def test_daily_only_alias_migrates_without_raw(make_store):
    s = make_store()
    s.save_equity("alias", ts("2020-01-01"), 123, 4)
    s.prune(ts("2026-01-01"))
    assert s.migrate_equity(["alias"], "id") == 0  # legacy return contract counts raw
    assert s.equity_heatmap("id")[0]["close"] == 123
    assert s.equity_heatmap("alias") == []


def test_save_failure_rolls_back_raw_replacement_and_archive(make_store):
    s = make_store()
    t = ts("2025-01-01")
    s.save_equity("a", t, 100, 0)
    s._conn.fail = "UPDATE equity_daily"
    with pytest.raises(RuntimeError):
        s.save_equity("a", t, 500, 0)
    s._conn.fail = None
    assert s._qall("SELECT bal FROM equity") == [(100,)]
    assert s.equity_heatmap("a")[0]["close"] == 100


def test_failed_migration_does_not_delete_either_history(make_store):
    s = make_store()
    t = ts("2025-01-01")
    s.save_equity("old", t, 100, 0)
    s.save_equity("new", t + 1, 110, 0)
    before = {a: s.equity_heatmap(a) for a in ["old", "new"]}
    s._conn.fail = "DELETE FROM equity_daily"
    assert s.migrate_equity(["old"], "new") == 0
    s._conn.fail = None
    assert {a: s.equity_heatmap(a) for a in before} == before
    assert s._qall("SELECT acct,ts FROM equity ORDER BY acct") == [("new", t + 1), ("old", t)]
    assert s.migrate_equity(["old"], "new") == 1


def test_concurrent_out_of_order_samples_are_serialized(make_store):
    s = make_store()
    d = ts("2025-01-01")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: s.save_equity("a", d + i, i, -2), reversed(range(100))))
    row = s.equity_heatmap("a")[0]
    assert (row["firstTs"], row["lastTs"], row["open"], row["close"], row["high"], row["low"]) == (
        d, d + 99, 0, 99, 99, 0)


def test_disabled_storage_never_connects_or_raises(monkeypatch):
    def forbidden(**_):
        pytest.fail("no connection should be attempted")
    monkeypatch.setattr(storage.pymysql, "connect", forbidden)
    s = Storage({})
    s.save_equity("a", 0, 0, 0)
    assert s.equity_heatmap("a") == []
    assert s.migrate_equity(["b"], "a") == 0
