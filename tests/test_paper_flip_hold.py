"""反手最小持有时间：抑制开仓几分钟内被噪声反向信号打掉。

实机 7 天：check_flip 992 笔、合计 -248.70，71% 发生在 10 分钟内、
41.7% 在 5 分钟内，中位 6 分钟。文件头设计第 5 条本来就写着
「方向翻转不提前离场」，旧实现却在第一次自检就允许反手。
"""
import time

from decision.config import DecisionConfig
from decision.paper import CHECK_MS, PaperBook


def ctx(price=100.0, atr=2.0, direction="bullish", score=70, gap=20):
    return {"price": price, "atr": atr, "score": score, "gap": gap,
            "grade": "medium", "direction": direction, "reason": "测试",
            "regime": "trend", "pattern": "", "patternStats": {},
            "maxLeverage": 20}


def _age(book, symbol="btcusdt", ms=None):
    """把仓位造旧，让下一次 tick 触发自检。"""
    pos = book.positions[symbol]
    old = int(time.time() * 1000) - (CHECK_MS if ms is None else ms) - 1000
    pos.entry_ts = old
    pos.last_check_ts = 0
    return pos


def _reverse():
    return ctx(price=100.02, direction="bearish", score=70, gap=25)


def test_flip_is_suppressed_within_min_hold():
    book = PaperBook()                      # 默认 15 分钟
    book.tick("btcusdt", ctx(price=100.0))
    _age(book)                              # 持有约 3 分钟
    book.tick("btcusdt", _reverse())
    assert not book.history, "持有不足 15 分钟不应反手平仓"
    assert book.positions["btcusdt"].last_verdict == "hold"


def test_flip_allowed_after_min_hold():
    book = PaperBook()
    book.tick("btcusdt", ctx(price=100.0))
    _age(book, ms=20 * 60 * 1000)          # 持有 20 分钟
    book.tick("btcusdt", _reverse())
    assert book.history[-1]["reason"] == "check_flip"


def test_zero_min_hold_restores_legacy_behaviour():
    book = PaperBook(flip_min_hold_ms=0)
    book.tick("btcusdt", ctx(price=100.0))
    _age(book)
    book.tick("btcusdt", _reverse())
    assert book.history[-1]["reason"] == "check_flip"


def test_win_and_loss_verdicts_are_not_blocked_by_hold_gate():
    """最小持有只管反手，不能挡住止盈/止损性质的判赢判亏。"""
    book = PaperBook()
    book.tick("btcusdt", ctx(price=100.0))
    _age(book)
    book.tick("btcusdt", ctx(price=100.5))
    assert book.history[-1]["reason"] == "check_win"


def test_config_exposes_switch_with_sane_default():
    cfg = DecisionConfig()
    assert cfg.paper_flip_min_hold_min == 15.0
    assert DecisionConfig.from_dict({"paper_flip_min_hold_min": 0}).paper_flip_min_hold_min == 0.0
    assert DecisionConfig.from_dict({"paper_flip_min_hold_min": 30}).paper_flip_min_hold_min == 30.0
    # 负数/脏值夹到 0，避免把下限变成负数导致永远不反手
    assert DecisionConfig.from_dict({"paper_flip_min_hold_min": -5}).paper_flip_min_hold_min == 0.0
