"""模拟仓 3 分钟自检：判亏 / 判赢 / 继续持有或反手。

新语义（替代原来的"15 分钟到期平仓"）：
  每 3 分钟按**当时的方向性涨跌幅**判定一次
    ≤ -0.15% → 判失败，平仓并立刻按当前数据重开
    ≥ +0.20% → 判胜利，落袋并立刻重开
    中间地带 → 默认继续持有；只有"方向相反 + 分数和分离度都够"才反手

用价格涨跌幅而不是保证金收益率：后者只是被杠杆放大的同一个数
（20x 下 0.1% 的价格波动 = 2% 的保证金波动），拿它判成败会让正常波动
看起来像大亏。
"""
import time

import pytest

from decision.paper import (CHECK_LOSS_PCT, CHECK_MS, CHECK_WIN_PCT,
                            SAFETY_HOLD_MS, PaperBook)


def ctx(price=100.0, atr=2.0, direction="bullish", score=70, gap=20):
    return {"price": price, "atr": atr, "score": score, "gap": gap,
            "grade": "medium", "direction": direction, "reason": "测试",
            "regime": "trend", "pattern": "", "patternStats": {}, "maxLeverage": 20}


def _age(book, symbol="btcusdt", ms=None):
    """把仓位"造旧"，让下一次 tick 触发自检。"""
    pos = book.positions[symbol]
    old = int(time.time() * 1000) - (CHECK_MS if ms is None else ms) - 1000
    pos.entry_ts = old
    pos.last_check_ts = 0
    return pos


# ---------- 第一档：跌够 → 判失败 ----------

def test_drop_beyond_threshold_closes_as_loss():
    book = PaperBook()
    book.tick("btcusdt", ctx(price=100.0))
    _age(book)
    # 跌 0.5%，超过 -0.15% 的失败线
    book.tick("btcusdt", ctx(price=99.5))
    trade = book.history[-1]
    assert trade["reason"] == "check_loss"
    assert trade["checkVerdict"] == "loss"
    assert trade["checkMovePct"] == pytest.approx(-0.5, abs=0.01)
    assert trade["checkNo"] == 1
    assert trade["pnl"] < 0


def test_short_position_loss_is_price_up():
    """空头：价格涨 = 亏。方向性涨跌幅必须按方向算。"""
    book = PaperBook()
    book.tick("btcusdt", ctx(price=100.0, direction="bearish"))
    _age(book)
    book.tick("btcusdt", ctx(price=100.5, direction="bearish"))
    trade = book.history[-1]
    assert trade["reason"] == "check_loss"
    assert trade["checkMovePct"] == pytest.approx(-0.5, abs=0.01)


# ---------- 第二档：涨够 → 判胜利 ----------

def test_gain_beyond_threshold_closes_as_win():
    book = PaperBook()
    book.tick("btcusdt", ctx(price=100.0))
    _age(book)
    book.tick("btcusdt", ctx(price=100.6))     # +0.6%，超过 +0.20%
    trade = book.history[-1]
    assert trade["reason"] == "check_win"
    assert trade["checkVerdict"] == "win"
    assert trade["checkMovePct"] == pytest.approx(0.6, abs=0.01)
    assert trade["pnl"] > 0


def test_short_position_win_is_price_down():
    book = PaperBook()
    book.tick("btcusdt", ctx(price=100.0, direction="bearish"))
    _age(book)
    book.tick("btcusdt", ctx(price=99.0, direction="bearish"))
    trade = book.history[-1]
    assert trade["reason"] == "check_win"
    assert trade["checkMovePct"] == pytest.approx(1.0, abs=0.01)


# ---------- 第三档：中间地带 ----------

def test_middle_zone_holds_when_signal_still_agrees():
    """涨跌都不够、信号仍与持仓同向 → 继续持有，不平仓。"""
    book = PaperBook()
    first = book.tick("btcusdt", ctx(price=100.0))
    _age(book)
    pos = book.tick("btcusdt", ctx(price=100.05, direction="bullish", score=70, gap=20))
    assert pos["entry"] == first["entry"], "应该还是同一个仓"
    assert book.stats["closed"] == 0
    assert pos["checks"] == 1
    assert pos["last_verdict"] == "hold"
    assert pos["last_move_pct"] == pytest.approx(0.05, abs=0.01)


def test_middle_zone_holds_even_if_weak_opposite_signal():
    """中间地带：方向反了但分数不够 → 不能反手（否则反复翻向磨手续费）。"""
    book = PaperBook()
    book.tick("btcusdt", ctx(price=100.0))
    _age(book)
    pos = book.tick("btcusdt", ctx(price=100.05, direction="bearish", score=52, gap=8))
    assert pos["direction"] == "long", "弱反向信号不该触发反手"
    assert book.stats["closed"] == 0


def test_middle_zone_flips_on_strong_opposite_signal():
    """中间地带：方向相反 + 分数与分离度都够 → 反手。

    反手是先平掉原仓（记 check_flip），再用**新方向**开一仓。
    """
    # 这两个用例专门验证「强反向会反手」这条路径，显式关闭最小持有抑制；
    # 抑制行为本身由 test_paper_flip_hold.py 覆盖。
    book = PaperBook(flip_min_hold_ms=0)
    book.tick("btcusdt", ctx(price=100.0))
    _age(book)
    pos = book.tick("btcusdt", ctx(price=100.02, direction="bearish",
                                   score=70, gap=25))
    trade = book.history[-1]
    assert trade["reason"] == "check_flip"
    assert trade["checkVerdict"] == "flip"
    assert trade["direction"] == "long", "被平掉的是原来的多头"
    assert pos["direction"] == "short", "新仓是反手后的空头"
    assert book.stats["closed"] == 1


# ---------- 时间基准 ----------

def test_check_does_not_fire_before_interval():
    """不到 3 分钟不判——否则一开仓就被判一次，等于没有窗口。"""
    book = PaperBook()
    book.tick("btcusdt", ctx(price=100.0))
    # 只过了 10 秒，但价格已经跌很多
    book.positions["btcusdt"].entry_ts = int(time.time() * 1000) - 10_000
    book.tick("btcusdt", ctx(price=99.0))
    assert book.stats["closed"] == 0, "没过自检周期不该判"
    assert book.positions["btcusdt"].checks == 0


def test_check_anchors_on_open_time_not_zero():
    """首次自检从开仓时刻起算，不能因为 last_check_ts=0 就立刻判。"""
    book = PaperBook()
    book.tick("btcusdt", ctx(price=100.0))
    pos = book.positions["btcusdt"]
    assert pos.last_check_ts == 0
    book.tick("btcusdt", ctx(price=100.01))
    assert book.positions["btcusdt"].checks == 0


def test_each_check_updates_timestamp():
    """第二轮自检要等上一个周期，不能每 tick 都判。"""
    book = PaperBook()
    book.tick("btcusdt", ctx(price=100.0))
    _age(book)
    book.tick("btcusdt", ctx(price=100.02))
    assert book.positions["btcusdt"].checks == 1
    first_ts = book.positions["btcusdt"].last_check_ts
    # 立刻再 tick：距上次自检不够 3 分钟，不该再判
    book.tick("btcusdt", ctx(price=100.02))
    assert book.positions["btcusdt"].checks == 1
    assert book.positions["btcusdt"].last_check_ts == first_ts


# ---------- 止损/止盈优先于自检 ----------

def test_hard_stop_still_wins():
    """碰到止损必须立刻平，不能等 3 分钟自检。"""
    book = PaperBook()
    book.tick("btcusdt", ctx(price=100.0))
    pos = book.positions["btcusdt"]
    book.tick("btcusdt", ctx(price=pos.sl * 0.999))
    assert book.history[-1]["reason"] == "sl"


def test_hold_can_exceed_old_15min_window():
    """中间地带可以持有超过原来的 15 分钟——这正是新语义的目的。"""
    book = PaperBook()
    book.tick("btcusdt", ctx(price=100.0))
    # 造旧 20 分钟（原来早就 timeout 了）
    book.positions["btcusdt"].entry_ts = int(time.time() * 1000) - 20 * 60 * 1000
    book.positions["btcusdt"].last_check_ts = 0
    book.tick("btcusdt", ctx(price=100.02))
    assert book.stats["closed"] == 0, "20 分钟内小幅波动应该继续持有"
    assert book.positions["btcusdt"].checks == 1
    assert SAFETY_HOLD_MS > 20 * 60 * 1000


# ---------- 自检专项统计与回撤 ----------

def test_check_stats_counts_verdicts_and_drawdown():
    """判赢/判亏/反手各多少 + 累计盈亏回撤。"""
    book = PaperBook()

    def one_round(target):
        """开一仓 → 造旧 → 按目标价触发一次自检（并清掉重开的仓）。"""
        book.positions.clear()
        book.tick("btcusdt", ctx(price=100.0))
        _age(book)
        book.tick("btcusdt", ctx(price=target))
        book.positions.clear()

    for _ in range(3):
        one_round(101.0)      # +1% → 判赢
    for _ in range(2):
        one_round(99.0)       # -1% → 判亏

    stats = book.check_stats()
    assert stats["winChecks"] == 3
    assert stats["lossChecks"] == 2
    assert stats["checks"] == 5
    assert stats["checkWinrate"] == 60.0
    assert stats["samples"] == 5
    # 赢了 3 笔再亏 2 笔：曲线先涨后回，最大回撤必须为负金额
    assert stats["peakPnl"] > 0
    assert stats["maxDrawdown"] < 0
    assert len(stats["curve"]) == 5


def test_check_stats_empty_is_safe():
    stats = PaperBook().check_stats()
    assert stats["checks"] == 0
    assert stats["checkWinrate"] is None
    assert stats["netPnl"] == 0
    assert stats["maxDrawdown"] == 0


def test_flip_recorded_in_stats():
    # 同上：显式关闭最小持有抑制，专测反手统计路径
    book = PaperBook(flip_min_hold_ms=0)
    book.tick("btcusdt", ctx(price=100.0))
    _age(book)
    book.tick("btcusdt", ctx(price=100.02, direction="bearish", score=70, gap=25))
    stats = book.check_stats()
    assert stats["flipChecks"] == 1
    assert stats["winChecks"] == 0 and stats["lossChecks"] == 0


def test_thresholds_are_in_ratio_units():
    """阈值必须是**小数**，与 _directional_move 的返回值同量纲。

    第一版写成了 -0.15/0.20（当成百分比），结果 -0.15 等价于 -15%，
    阈值永远触发不了——自检形同不存在。
    """
    from decision.paper import CHECK_LOSS_RATIO, CHECK_WIN_RATIO
    assert -0.01 < CHECK_LOSS_RATIO < 0
    assert 0 < CHECK_WIN_RATIO < 0.01
    # 0.5% 的下跌必须能触发失败判定
    assert -0.005 <= CHECK_LOSS_RATIO
    # 0.6% 的上涨必须能触发胜利判定
    assert 0.006 >= CHECK_WIN_RATIO


def test_check_stats_reads_db_not_just_memory():
    """接了库时必须用全量流水算，不能只看内存里的 200 条。

    线上实测：进程刚启动、内存 history 为空，但库里有历史 →
    curve 全是 0、maxDrawdown 也是 0，看着像"从来没亏过"。
    """
    class FakeStore:
        enabled = True

        def paper_positions(self):
            return []

        def paper_trades(self, limit=200, symbol=""):
            return [
                {"symbol": "btcusdt", "pnl": 5.0, "closedAt": 1, "checkVerdict": "win"},
                {"symbol": "btcusdt", "pnl": -8.0, "closedAt": 2, "checkVerdict": "loss"},
            ]

        def save_paper_position(self, pos):
            pass

        def delete_paper_position(self, symbol):
            pass

        def save_paper_trade(self, trade):
            pass

        def paper_trade_stats(self, days=0):
            return {"count": 2, "wins": 1, "losses": 1, "timeouts": 0, "pnl": -3.0}

        def paper_symbol_stats(self, days=0):
            return []

    book = PaperBook(store=FakeStore())
    stats = book.check_stats()
    assert stats["samples"] == 2, "必须读到库里的流水"
    assert stats["winChecks"] == 1 and stats["lossChecks"] == 1
    assert stats["netPnl"] == -3.0
    # 先赚 5 再亏 8：峰值 5、当前 -3、最大回撤 -8
    assert stats["peakPnl"] == 5.0
    assert stats["maxDrawdown"] == -8.0
    assert stats["curve"] == [5.0, -3.0]
