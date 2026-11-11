"""eval_outcomes 回归：历史 pending 能被长期归档补判，不再永久卡住。

线上真实故障：判定窗口只读滚动表 candles（保留 30 分钟），服务重启或
窗口被 prune 后取不满 3 根就 continue，命中永远留在 pending。这里用不连库的
FakeStore 锁住修复后的行为：读归档、限流、无数据要记账、中性形态不占坑。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import storage
from storage import Storage

NOW = 1_800_000_000_000
EVAL_MS = 600_000


class _Clock:
    @staticmethod
    def time() -> float:
        return NOW / 1000.0


class FakeStore(Storage):
    """只替换 _q / _qall 的内存 Storage，不碰数据库。"""

    def __init__(self, hits, archive, eval_batch: int = 50):
        self.eval_ms = EVAL_MS
        self.eval_min_bars = 3
        self.eval_threshold = 0.0015
        self.eval_batch = eval_batch
        self.hit_ms = 90 * 86_400_000
        self._directionless_marked = False
        self.hits_rows = list(hits)
        self.archive_rows = list(archive)
        self.updates: list[tuple] = []
        self.selects: list[tuple] = []

    def _qall(self, sql, args=()):
        self.selects.append((sql, args))
        if "FROM hits" in sql:
            return list(self.hits_rows)
        if "FROM candle_archive" in sql:
            return list(self.archive_rows)
        return []

    def _q(self, sql, args=()):
        self.updates.append((sql, args))


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(storage, "time", _Clock())


WIN_BARS = [(100.30, 99.90, 100.20), (100.10, 99.95, 100.05),
            (100.08, 99.97, 100.02)]


def test_old_pending_is_backfilled_from_archive():
    """1 小时前的命中，滚动表早没数据了，仍应靠归档判出 win。"""
    old = NOW - 3_600_000
    s = FakeStore([(7, "bullish", 100.0, old)], WIN_BARS)

    s.eval_outcomes("BTCUSDT", "1m", 60_000, None)

    assert any("FROM candle_archive" in sql for sql, _ in s.selects)
    assert not any("FROM candles " in sql for sql, _ in s.selects)
    verdicts = [a for sql, a in s.updates if "outcome=%s" in sql]
    assert verdicts and verdicts[0][0] == "win"
    assert verdicts[0][2] == 7


def test_missing_archive_is_recorded_not_left_pending():
    """归档也缺窗口时明确记 no_data，不让它反复占批次名额。"""
    old = NOW - 3_600_000
    s = FakeStore([(8, "bearish", 100.0, old)], [(100.01, 99.99, 100.0)])

    s.eval_outcomes("BTCUSDT", "1m", 60_000, None)

    assert any("outcome='no_data'" in sql and a == (8,)
               for sql, a in s.updates)


def test_non_judged_hits_are_marked_once():
    s = FakeStore([], WIN_BARS)
    s.eval_outcomes("BTCUSDT", "1m", 60_000, None)
    s.eval_outcomes("BTCUSDT", "1m", 60_000, None)

    marks = [(sql, a) for sql, a in s.updates if "outcome='neutral'" in sql]
    assert len(marks) == 1
    assert "mode<>'confirmed'" in marks[0][0]


def test_query_is_batched_and_time_bounded():
    s = FakeStore([], WIN_BARS, eval_batch=25)
    s.eval_outcomes("BTCUSDT", "1m", 60_000, None)

    sql, args = next((sql, a) for sql, a in s.selects if "FROM hits" in sql)
    assert "LIMIT %s" in sql and "ts>=%s" in sql and "ts<=%s" in sql
    assert args[-1] == 25
    assert args[-2] == NOW - EVAL_MS


def test_not_due_hit_is_skipped():
    """FakeStore 不过滤 SQL；未到窗口的命中必须在 Python 侧跳过。"""
    fresh = NOW - 60_000
    s = FakeStore([(9, "bullish", 100.0, fresh)], WIN_BARS)

    s.eval_outcomes("BTCUSDT", "1m", 60_000, None)

    assert not any("outcome=%s" in sql for sql, _ in s.updates)


def _hit(mode, direction):
    return SimpleNamespace(
        ts=NOW - 3_600_000, symbol="BTCUSDT", interval="1m",
        pattern="doji", name_zh="十字星", direction=direction, mode=mode,
        price=100.0, bar_open_time=NOW - 3_600_000)


def test_save_hit_marks_live_as_neutral():
    s = FakeStore([], WIN_BARS)
    s.save_hit(_hit("live", "bullish"))
    sql, args = s.updates[-1]
    assert "outcome" in sql and args[-1] == "neutral"


def test_save_hit_marks_directionless_as_neutral():
    s = FakeStore([], WIN_BARS)
    s.save_hit(_hit("confirmed", "neutral"))
    assert s.updates[-1][1][-1] == "neutral"


def test_save_hit_keeps_judgeable_pending():
    s = FakeStore([], WIN_BARS)
    s.save_hit(_hit("confirmed", "bullish"))
    assert s.updates[-1][1][-1] == "pending"


def _hits_select_args(store):
    return [args for sql, args in store.selects if "FROM hits" in sql][-1]


def test_short_interval_keeps_ten_minute_window():
    """1m/3m 行为不变：仍是 10 分钟到期。"""
    s = FakeStore([], WIN_BARS)
    s.eval_outcomes("BTCUSDT", "1m", 60_000, None)
    assert _hits_select_args(s)[3] == NOW - EVAL_MS


def test_long_interval_expands_window_to_min_bars():
    """1h 信号固定 10 分钟窗口里一根 K 线都没有，须放宽到至少 3 根。"""
    s = FakeStore([], WIN_BARS)
    s.eval_outcomes("BTCUSDT", "1h", 3_600_000, None)
    assert _hits_select_args(s)[3] == NOW - 3 * 3_600_000


def test_30m_interval_expands_window():
    s = FakeStore([], WIN_BARS)
    s.eval_outcomes("BTCUSDT", "30m", 1_800_000, None)
    assert _hits_select_args(s)[3] == NOW - 3 * 1_800_000


def test_eval_min_bars_is_configurable():
    s = FakeStore([], WIN_BARS)
    s.eval_min_bars = 5
    s.eval_outcomes("BTCUSDT", "1h", 3_600_000, None)
    assert _hits_select_args(s)[3] == NOW - 5 * 3_600_000
