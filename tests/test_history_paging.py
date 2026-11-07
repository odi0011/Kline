"""历史仓分页读 / 分块写的回归测试。

背景：3m 九年 ≈120 万根（实测 1,197,600），而 candle_history_range 单次
LIMIT 上限 50 万；直接用大 limit 会静默只返回最早一段，回测跑在错误区间。
"""
from storage import Storage


def _store_with_range(fn) -> Storage:
    """绕开 DB 初始化，只替换分页读依赖的 query 方法。"""
    s = object.__new__(Storage)
    s.enabled = True
    s.candle_history_range = fn
    return s


def test_paging_returns_all_rows_in_order_across_pages():
    rows = [{"t": i * 1000, "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0,
             "v": 1.0, "trades": 0, "buyVol": 0.0, "sellVol": 0.0,
             "source": "futures"} for i in range(1, 8)]
    calls = []

    def fake(sym, iv, start, end, limit):
        calls.append((start, limit))
        return [r for r in rows if start <= r["t"] <= end][:limit]

    got = _store_with_range(fake).candle_history_all("btcusdt", "3m", page=2)
    assert [r["t"] for r in got] == [r["t"] for r in rows]
    # 游标 = 上一页最后一根 t + 1（毫秒整数，主键唯一不会漏行）
    assert calls == [(0, 2), (2001, 2), (4001, 2), (6001, 2)]


def test_paging_handles_over_one_million_rows_without_truncation():
    """模拟 120 万根：不能用真实 list，按游标生成即可验证不截断/不重复。"""
    total = 1_200_000
    page = 100_000
    calls = []

    def fake(sym, iv, start, end, limit):
        calls.append((start, limit))
        first = max(0, start)
        last = min(first + limit, total)
        if first >= last:
            return []
        return [{"t": t} for t in range(first, last)]

    got = _store_with_range(fake).candle_history_all("btcusdt", "3m", page=page)
    assert len(got) == total
    assert got[0]["t"] == 0 and got[-1]["t"] == total - 1
    # 12 页满 + 1 次空探测；每页游标严格递增，无重复查询
    assert len(calls) == total // page + 1
    assert [c[0] for c in calls] == [i * page for i in range(total // page + 1)]


def test_paging_respects_start_and_end_bounds():
    seen = []

    def fake(sym, iv, start, end, limit):
        seen.append((start, end))
        return [{"t": t} for t in range(start, min(start + limit, end + 1))][:limit]

    got = _store_with_range(fake).candle_history_all(
        "btcusdt", "3m", start_ms=10, end_ms=25, page=10)
    assert [r["t"] for r in got] == list(range(10, 26))


def test_save_candle_history_writes_in_bounded_batches():
    """一次 executemany 吞 120 万行会撑爆 max_allowed_packet。"""
    s = object.__new__(Storage)
    s.enabled = True
    batches = []
    s._qmany = lambda sql, rows: batches.append((sql, len(rows)))

    rows = [{"t": i + 1, "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0, "v": 1.0,
             "trades": 0, "buyVol": 0.0, "sellVol": 0.0} for i in range(45_001)]
    n = s.save_candle_history("btcusdt", "3m", rows, "futures")
    assert n == 45_001
    assert [b[1] for b in batches] == [20_000, 20_000, 5_001]
    assert all("REPLACE INTO candle_history" in b[0] for b in batches)


def test_save_candle_history_skips_invalid_timestamps():
    s = object.__new__(Storage)
    s.enabled = True
    batches = []
    s._qmany = lambda sql, rows: batches.append(len(rows))
    rows = [{"t": 0, "o": 1.0}, {"t": 1000, "o": 1.0, "c": 2.0}]
    assert s.save_candle_history("btcusdt", "3m", rows) == 1
    assert batches == [1]


def test_history_interval_table_covers_production_intervals():
    from feed.history import IV_MS
    for iv in ("1m", "3m", "15m", "1h", "4h"):
        assert iv in IV_MS, iv
    assert IV_MS["3m"] == 180_000
