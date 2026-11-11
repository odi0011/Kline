"""模拟仓剩余风险修复：open_seq / 补算质量 / 取消监控清理。

对应 reports/paper-persistence-verification.md 的剩余开放项：
  · R2c：trade_key=symbol:entryTs 同毫秒碰撞 → 开仓单调序号（跨重启续号）；
  · P2-7：停机补结算的近似边界 → record 增加 settleBars / settleApprox 质量标记；
  · P2-5：drop_missing 无生产调用方 → PaperBook.close_missing + QuantEngine
    按 symbol_filter 清理监控范围外的模拟仓。
"""
from __future__ import annotations

import time

import pytest

import decision.paper as paper_mod
from decision.paper import PaperBook
from quant import QuantEngine
from tests.test_paper_persist import Bar, FakeStore

NOW = 1_800_000_000_000


class FakeClock:
    """替换 decision.paper.time：让"同一毫秒内平仓重开"这类边界可复现。"""

    def __init__(self, now_ms: int = NOW):
        self.now = int(now_ms)

    def time(self) -> float:
        return self.now / 1000.0


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(paper_mod, "time", fake)
    return fake


def ctx(price=100.0, atr=2.0, direction="bullish", score=70, gap=20,
        max_lev=20, bars=None):
    data = {"price": price, "atr": atr, "score": score, "gap": gap,
            "grade": "medium", "direction": direction, "reason": "测试",
            "regime": "trend", "pattern": "hammer",
            "patternStats": {}, "maxLeverage": max_lev}
    if bars is not None:
        data["bars"] = bars
    return data


def _expired_store(price=100.0, atr=1.0):
    """造一个"停机期间已到期"的库内仓位，返回 (store, 开仓快照, 库内数据)。"""
    store = FakeStore()
    book = PaperBook(store=store)
    opened = book.tick("btcusdt", ctx(price=price, atr=atr))
    saved = dict(store.open["btcusdt"])
    now = int(time.time() * 1000)
    saved["entry_ts"] = now - 20 * 60_000
    saved["expires_ts"] = now - 5 * 60_000
    store.open["btcusdt"] = saved
    return store, opened, saved


# ---------- A. open_seq：单调递增 / 重启续号 / 同毫秒不碰撞 ----------

def test_open_seq_increments_and_restores_after_restart(clock):
    store = FakeStore()
    book = PaperBook(store=store)
    first = book.tick("btcusdt", ctx(price=100.0, atr=1.0))
    assert first["open_seq"] == 1
    assert book._open_seq == 1
    assert store.open["btcusdt"]["open_seq"] == 1

    book.force("btcusdt", ctx(price=100.5, atr=1.0))     # 平 1 号 → 重开 2 号
    assert book.positions["btcusdt"].open_seq == 2
    assert store.trades[-1]["openSeq"] == 1
    # 实时平仓不是补结算：质量标记保持 0/False
    assert store.trades[-1]["settleBars"] == 0
    assert store.trades[-1]["settleApprox"] is False

    book.tick("ethusdt", ctx(price=50.0, atr=0.5))
    assert book.positions["ethusdt"].open_seq == 3
    assert book._open_seq == 3

    restarted = PaperBook(store=store)                   # 等同进程重启
    assert restarted.positions["btcusdt"].open_seq == 2
    assert restarted.positions["ethusdt"].open_seq == 3
    assert restarted._open_seq == 3, "重启后 open_seq 必须从恢复出的最大值续号"
    restarted.force("btcusdt", ctx(price=100.5, atr=1.0))
    assert restarted.positions["btcusdt"].open_seq == 4, "续号不能从 1 重来"
    assert restarted._open_seq == 4
    assert store.trades[-1]["openSeq"] == 2              # 恢复仓自己的序号


def test_open_seq_continues_from_history_when_no_open_positions():
    """全部仓位都已平掉（无未平仓行）时，用历史流水的 openSeq 续号。"""
    store = FakeStore()
    book = PaperBook(store=store)
    book.tick("btcusdt", ctx(price=100.0, atr=1.0))
    book.force("btcusdt", ctx(price=101.0, atr=1.0))
    book.force("btcusdt", ctx(price=102.0, atr=1.0))
    assert [r["openSeq"] for r in store.trades] == [1, 2]
    store.open.clear()                                   # 只剩流水

    restarted = PaperBook(store=store)
    assert restarted.positions == {}
    assert restarted._open_seq == 2
    pos = restarted.tick("btcusdt", ctx(price=100.0, atr=1.0))
    assert pos["open_seq"] == 3


def test_same_millisecond_reopen_gets_distinct_open_seq(clock):
    """R2c：同一毫秒内平仓重开，open_seq 保证 trade_key 不碰撞。"""
    store = FakeStore()
    book = PaperBook(store=store)
    book.tick("btcusdt", ctx(price=100.0, atr=1.0))
    for _ in range(3):
        book.force("btcusdt", ctx(price=100.0, atr=1.0))

    records = list(store.trades)
    assert len(records) == 3
    assert {r["entryTs"] for r in records} == {NOW}, "模拟时钟下三笔开仓同一毫秒"
    assert [r["openSeq"] for r in records] == [1, 2, 3]
    keys = {f"{r['symbol']}:{r['entryTs']}:{r['openSeq']}" for r in records}
    assert len(keys) == 3, "同毫秒重开的 trade_key 不能碰撞"
    assert book._open_seq == 4


# ---------- B. 补算质量：覆盖 / 无 bars / 未覆盖 / 命中 tp ----------

def test_recovered_timeout_covered_by_bars_marks_approx_false():
    store, opened, saved = _expired_store()
    restarted = PaperBook(store=store)
    # 窗口内 15 根K线 + 到期后仍有K线 → 覆盖完整（高低都不碰 tp/sl）
    bars = [Bar(saved["entry_ts"] + i * 60_000, high=100.4, low=99.6,
                close=100.2)
            for i in range(1, 20)]
    restarted.tick("btcusdt", ctx(price=100.3, atr=1.0, bars=bars))
    rec = restarted.history[-1]
    assert rec["reason"] == "timeout"
    assert rec["recovered"] is True
    assert rec["exit"] == 100.2
    assert rec["settleBars"] == 15, "entry_ts < bar_ts <= expires_ts 的K线根数"
    assert rec["settleApprox"] is False, "K线覆盖到到期时刻 → 离场价不算近似"
    assert rec["openSeq"] == opened["open_seq"]


def test_recovered_without_bars_falls_back_and_marks_approx_true():
    store, opened, _ = _expired_store()
    restarted = PaperBook(store=store)
    restarted.tick("btcusdt", ctx(price=100.3, atr=1.0))
    rec = restarted.history[-1]
    assert rec["reason"] == "timeout"
    assert rec["exit"] == 100.3, "没有K线只能用现价回落"
    assert rec["settleBars"] == 0
    assert rec["settleApprox"] is True
    assert rec["recovered"] is True
    assert rec["openSeq"] == opened["open_seq"]


def test_recovered_with_partial_bars_marks_approx_true():
    store, _, saved = _expired_store()
    restarted = PaperBook(store=store)
    # 只有窗口开头一根K线，离 expires_ts 还差很远
    bars = [Bar(saved["entry_ts"] + 60_000, high=100.4, low=99.6, close=100.1)]
    restarted.tick("btcusdt", ctx(price=100.3, atr=1.0, bars=bars))
    rec = restarted.history[-1]
    assert rec["reason"] == "timeout"
    assert rec["exit"] == 100.1
    assert rec["settleBars"] == 1
    assert rec["settleApprox"] is True, "K线没覆盖到 expires_ts → 近似结算"

def test_recovered_with_unusable_bars_falls_back_to_price():
    store, _, _ = _expired_store()
    restarted = PaperBook(store=store)
    restarted.tick("btcusdt", ctx(price=100.7, atr=1.0, bars=[object(), None]))
    rec = restarted.history[-1]
    assert rec["reason"] == "timeout"
    assert rec["exit"] == 100.7
    assert rec["settleBars"] == 0
    assert rec["settleApprox"] is True


def test_recovered_hit_tp_marks_approx_false_and_keeps_open_seq():
    store, opened, saved = _expired_store()
    restarted = PaperBook(store=store)
    bars = [Bar(saved["entry_ts"] + 60_000, high=opened["tp"] + 1,
                low=100.0, close=opened["tp"])]
    restarted.tick("btcusdt", ctx(price=100.5, atr=1.0, bars=bars))
    rec = restarted.history[-1]
    assert rec["reason"] == "tp"
    assert rec["exit"] == opened["tp"]
    assert rec["settleBars"] == 1
    assert rec["settleApprox"] is False, "命中 tp/sl 有明确触发价，不算近似"
    assert rec["recovered"] is True
    assert rec["openSeq"] == opened["open_seq"]


# ---------- C. close_missing：取消监控收尾 ----------

def test_close_missing_closes_with_mark_and_records_removed():
    store = FakeStore()
    book = PaperBook(store=store)
    book.tick("btcusdt", ctx(price=100.0, atr=1.0))
    book.tick("ethusdt", ctx(price=50.0, atr=0.5))

    handled = book.close_missing({"btcusdt"})
    assert handled == ["ethusdt"]
    assert "ethusdt" not in book.positions and "btcusdt" in book.positions
    assert "ethusdt" not in store.open and "btcusdt" in store.open
    rec = store.trades[-1]
    assert rec["symbol"] == "ethusdt"
    assert rec["reason"] == "removed"
    assert rec["exit"] == 50.0, "按最后一个已知 mark 平仓"
    assert rec["recovered"] is False
    assert rec["settleBars"] == 0 and rec["settleApprox"] is False
    assert rec["openSeq"] == 2
    assert book.stats["closed"] == 1
    # 正常记账：removed 也进统计（与 drop_missing 的"不留数据"不同）
    by = {row["symbol"]: row for row in book.summary()["bySymbol"]}
    assert by["ethusdt"]["closed"] == 1


def test_close_missing_without_mark_deletes_row_without_trade():
    store = FakeStore()
    book = PaperBook(store=store)
    book.tick("btcusdt", ctx(price=100.0, atr=1.0))
    book.positions["btcusdt"].mark = 0.0        # 没有任何可用价格
    trades_before = len(store.trades)

    handled = book.close_missing(set())
    assert handled == ["btcusdt"]
    assert book.positions == {}
    assert "btcusdt" not in store.open
    assert len(store.trades) == trades_before, "没有价格的仓位不能编流水"
    assert book.stats["closed"] == 0


def test_close_missing_noop_when_all_in_scope():
    store = FakeStore()
    book = PaperBook(store=store)
    book.tick("btcusdt", ctx(price=100.0, atr=1.0))
    book.tick("ethusdt", ctx(price=50.0, atr=0.5))
    assert book.close_missing({"btcusdt", "ethusdt"}) == []
    assert set(book.positions) == {"btcusdt", "ethusdt"}
    assert len(store.open) == 2
    assert store.trades == []


# ---------- D. QuantEngine 接线：按 symbol_filter 清理 ----------

def _engine(tmp_path):
    eng = QuantEngine({"list": []}, str(tmp_path / "q.json"))
    # 这一组用例测的是「显式开启时的范围收尾」；生产默认已改为永不因
    # 展示勾选平仓（见 tests/test_paper_scope.py）。DecisionConfig 是 frozen。
    from dataclasses import replace
    eng.decision_cfg = replace(eng.decision_cfg, paper_scope_cleanup=True)
    return eng


def test_quant_engine_settles_paper_out_of_scope(tmp_path):
    engine = _engine(tmp_path)
    engine.paper = PaperBook()
    engine.paper.tick("btcusdt", ctx(price=100.0, atr=1.0))
    engine.paper.tick("ethusdt", ctx(price=50.0, atr=0.5))

    # 启动初期还没拿到展示列表（None / 空集）→ 不能动账本
    assert engine._settle_paper_out_of_scope() == []
    engine.symbol_filter = set()
    assert engine._settle_paper_out_of_scope() == []
    assert set(engine.paper.positions) == {"btcusdt", "ethusdt"}

    # 范围收窄 → 只清理范围外的币
    engine.symbol_filter = {"btcusdt"}
    assert engine._settle_paper_out_of_scope() == ["ethusdt"]
    assert set(engine.paper.positions) == {"btcusdt"}
    assert engine.paper.history[-1]["reason"] == "removed"


def test_manage_cleans_out_of_scope_paper_positions(tmp_path):
    engine = _engine(tmp_path)
    engine.paper = PaperBook()
    engine.paper.tick("ethusdt", ctx(price=50.0, atr=0.5))
    engine.symbol_filter = {"btcusdt"}

    engine.manage()                 # manage 开头 refresh_indicators() 之后接管清理

    assert engine.paper.positions == {}
    assert engine.paper.history[-1]["reason"] == "removed"


def test_manage_survives_paper_scope_cleanup_errors(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    engine.paper = PaperBook()
    engine.paper.tick("ethusdt", ctx(price=50.0, atr=0.5))
    engine.symbol_filter = {"btcusdt"}

    def boom(keep, ctx=None):
        raise RuntimeError("清理失败")

    monkeypatch.setattr(engine.paper, "close_missing", boom)
    engine.manage()                 # 异常不能打断 5 秒一轮的 manage
    assert "ethusdt" in engine.paper.positions
