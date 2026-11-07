"""策略报表的「真实成交」明细必须和主数字用同一个时间窗。

实机发现：近 1 天报表的主数字 12 笔按 ts>=since 过滤，但按 reason 拆分的
明细没有时间条件，把全表历史（含修复前的 6 笔「保护单缺失且补挂失败」）
混了进来，看起来像新代码还在犯老毛病。
"""
import importlib.util
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "strategy_report", ROOT / "scripts" / "strategy_report.py")
SR = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(SR)


class FakeCursor:
    def __init__(self):
        self.calls = []
        self.last_sql = ""

    def execute(self, sql, args=()):
        self.calls.append((sql, tuple(args)))
        self.last_sql = sql

    def fetchall(self):
        # skip 原因那处是 `for reason, count in q(...)`，必须恰好两列；
        # 其余查询用 r[0..2]，多给几列不影响。
        if "quant_events" in self.last_sql and "GROUP BY reason" in self.last_sql:
            return [(0, 0)]
        return [(0, 0, 0, 0, 0)]


class FakeConn:
    def __init__(self):
        self.cur = FakeCursor()

    def cursor(self):
        return self.cur


def test_all_quant_trades_queries_share_the_days_window():
    before = int(time.time() * 1000)
    conn = FakeConn()
    SR.collect(conn, days=1)
    after = int(time.time() * 1000)

    live_queries = [(sql, args) for sql, args in conn.cur.calls
                    if "quant_trades" in sql]
    assert len(live_queries) >= 2, "应同时有主指标与按原因明细两条查询"
    for sql, args in live_queries:
        assert "ts>=%s" in sql, sql
        assert len(args) == 1, args
        since = args[0]
        assert before - 86_400_000 <= since <= after - 86_400_000, (since, before)


def test_collect_returns_live_reasons():
    conn = FakeConn()
    data = SR.collect(conn, days=7)
    assert "live_reasons" in data
    assert data["live"] == {"trades": 0, "pnl": 0.0, "wins": 0}
