"""模拟仓：15 分钟周期、动态止损、胜率公式、强平价。

这些测试是"策略不会乱来"的证据：止盈止损不能被放大、
强平价不能算错、胜率公式要有可解释的分量。
"""
import time

from decision.paper import (HOLD_MS, MAX_PAPER_LEV, NOTIONAL, PaperBook,
                            _liq_price, estimate_winrate)


def ctx(price=100.0, atr=2.0, direction="bullish", score=70, gap=20,
        max_lev=20, pattern_stats=None, pattern=""):
    return {"price": price, "atr": atr, "score": score, "gap": gap,
            "grade": "medium", "direction": direction, "reason": "测试",
            "regime": "trend", "pattern": pattern,
            "patternStats": pattern_stats or {}, "maxLeverage": max_lev}


# ---------- 胜率公式 ----------

def test_winrate_is_between_0_and_100():
    for score in (0, 30, 50, 70, 100):
        r = estimate_winrate(score, 10, 0.008, 0.019)
        if r["prob"] is not None:
            assert 0 <= r["prob"] <= 100


def test_higher_score_gives_higher_winrate():
    low = estimate_winrate(52, 3, 0.008, 0.019)["prob"]
    high = estimate_winrate(85, 40, 0.008, 0.019)["prob"]
    assert high > low, f"高分应更高: {high} vs {low}"


def test_better_rr_lowers_baseline():
    """止盈越远，随机游走下先碰到它的概率越低（baseline 下降）。"""
    tight = estimate_winrate(70, 20, 0.008, 0.010)["parts"]["baseline"]
    wide = estimate_winrate(70, 20, 0.008, 0.030)["parts"]["baseline"]
    assert tight > wide


def test_winrate_reports_components():
    """每个分量都要能看到，便于解释"这个数字怎么来的"。"""
    r = estimate_winrate(75, 30, 0.008, 0.019)
    parts = r["parts"]
    assert "baseline" in parts and "edge" in parts and "final" in parts


def test_pattern_history_pulls_winrate_down():
    """形态历史很差时，胜率要真的被拉低。"""
    bad = {"hammer": {"weightedWinrate": 0.2, "effectiveSamples": 20}}
    good = {"hammer": {"weightedWinrate": 0.8, "effectiveSamples": 20}}
    low = estimate_winrate(70, 20, 0.008, 0.019, bad, "hammer")["prob"]
    high = estimate_winrate(70, 20, 0.008, 0.019, good, "hammer")["prob"]
    assert high > low


def test_low_sample_pattern_is_ignored():
    """样本太少不参与，避免 1胜0负 这种把胜率抬到 100%。"""
    tiny = {"hammer": {"weightedWinrate": 1.0, "effectiveSamples": 1}}
    base = estimate_winrate(70, 20, 0.008, 0.019)["prob"]
    with_tiny = estimate_winrate(70, 20, 0.008, 0.019, tiny, "hammer")["prob"]
    assert base == with_tiny


# ---------- 强平价 ----------

def test_liquidation_price_direction():
    assert _liq_price(100, 10, "long") < 100
    assert _liq_price(100, 10, "short") > 100


def test_higher_leverage_moves_liquidation_closer():
    l2 = _liq_price(100, 2, "long")
    l20 = _liq_price(100, 20, "long")
    assert l20 > l2, f"20x 的强平应更靠近入场: {l20} vs {l2}"


# ---------- 开仓与生命周期 ----------

def test_opens_position_with_all_fields():
    book = PaperBook()
    pos = book.tick("btcusdt", ctx())
    assert pos is not None
    assert pos["direction"] == "long"
    assert pos["leverage"] <= MAX_PAPER_LEV
    assert pos["notional"] == NOTIONAL
    assert pos["margin"] == round(NOTIONAL / pos["leverage"], 2)
    # 多头：止损在下、止盈在上
    assert pos["sl"] < pos["entry"] < pos["tp"]
    assert pos["liq"] < pos["entry"]
    assert pos["winrate"] is not None
    # 15 分钟周期
    assert pos["expires_ts"] - pos["entry_ts"] == HOLD_MS


def test_short_position_levels_are_mirrored():
    book = PaperBook()
    pos = book.tick("btcusdt", ctx(direction="bearish"))
    assert pos["direction"] == "short"
    assert pos["tp"] < pos["entry"] < pos["sl"]
    assert pos["liq"] > pos["entry"]


def test_no_direction_means_no_position():
    """方向不明时不能硬开一个仓。"""
    book = PaperBook()
    assert book.tick("btcusdt", {**ctx(), "direction": ""}) is None
    assert book.tick("btcusdt", {**ctx(), "price": 0}) is None


def test_take_profit_closes_and_reopens():
    book = PaperBook()
    pos = book.tick("btcusdt", ctx(price=100.0))
    tp = pos["tp"]
    # 价格冲到止盈上方
    book.tick("btcusdt", ctx(price=tp + 1))
    assert book.stats["closed"] == 1
    assert book.history[-1]["reason"] == "tp"
    # 平完立刻重开，仓位永不停
    assert "btcusdt" in book.positions
    assert book.stats["opened"] == 2


def test_stop_loss_closes():
    book = PaperBook()
    pos = book.tick("btcusdt", ctx(price=100.0))
    sl = pos["sl"]
    book.tick("btcusdt", ctx(price=sl - 1))
    assert book.history[-1]["reason"] == "sl"
    assert book.stats["losses"] == 1


def test_direction_flip_does_not_close():
    """方向翻转不是离场信号：15 分钟的窗口必须走完。

    线上实测：把 flip 当离场条件时，11 小时平了 9762 笔、100% 是 flip、
    中位持有 6 秒——统计全是噪声。只由止盈/止损/到期离场。
    """
    book = PaperBook()
    first = book.tick("btcusdt", ctx(direction="bullish"))
    book.tick("btcusdt", ctx(direction="bearish"))
    assert book.stats["closed"] == 0
    assert book.positions["btcusdt"].entry == first["entry"], "还是同一个仓"
    assert book.positions["btcusdt"].direction == "long"


def test_timeout_closes():
    book = PaperBook()
    pos = book.tick("btcusdt", ctx(price=100.0))
    # 手动把到期时间拨到过去
    book.positions["btcusdt"].expires_ts = int(time.time() * 1000) - 1
    book.tick("btcusdt", ctx(price=100.5))
    assert book.history[-1]["reason"] == "timeout"


def test_trailing_stop_only_moves_in_favor():
    """移动止损只能往盈利方向移，绝不放大风险。

    先要走出 TRAIL_ATR 个 ATR 才「武装」，之后才允许上移；涨幅也不能
    碰到止盈，否则旧仓会先止盈平掉、重开一个，就测不到同一个仓了。
    """
    book = PaperBook()
    pos = book.tick("btcusdt", ctx(price=100.0, atr=1.0))   # SL 98.5，TP 102.4
    sl0 = pos["sl"]
    armed = 101.5                                           # > 1.4 ATR，仍低于止盈
    book.tick("btcusdt", ctx(price=armed, atr=1.0))
    after = book.positions.get("btcusdt")
    assert after is not None, "这一涨不该触发止盈"
    assert after.entry == 100.0, "应该是同一个仓，不是重开的"
    assert after.sl > sl0, "多头的止损应该上移"
    assert after.trailing is True
    assert after.sl > 100.0, "武装时止损应落在成本之上"
    # 价格回落，止损不能跟着下移
    sl1 = after.sl
    book.tick("btcusdt", ctx(price=armed - 0.3, atr=1.0))
    assert book.positions["btcusdt"].sl >= sl1


def test_trailing_not_armed_before_trail_atr():
    """没走出 TRAIL_ATR 之前不许抬止损——否则等于一开仓就被窄止损罩住。"""
    book = PaperBook()
    pos = book.tick("btcusdt", ctx(price=100.0, atr=1.0))
    sl0 = pos["sl"]
    book.tick("btcusdt", ctx(price=101.2, atr=1.0))   # < entry + 1.4 ATR
    after = book.positions["btcusdt"]
    assert after.sl == sl0 and after.trailing is False


def test_trailing_uses_entry_atr_not_current_atr():
    """行情转静、当前 ATR 塌掉时，止损不能被拽到贴价。

    线上实测：BTC 5 分钟 ATR 从 60 掉到 8，用当前 ATR 算
    「peak − 1.4 ATR」会把止损压到 0.01%–0.17%，价格一抖就出局。
    基准必须是开仓时的 ATR。
    """
    book = PaperBook()
    pos = book.tick("btcusdt", ctx(price=100.0, atr=1.0))
    sl0 = pos["sl"]
    book.tick("btcusdt", ctx(price=101.5, atr=1.0))    # 武装，止损上移到 100.1
    armed_sl = book.positions["btcusdt"].sl
    assert armed_sl > sl0
    # 下一轮 ATR 塌到 0.05：止损不该被拉到 101.6 − 0.07
    book.tick("btcusdt", ctx(price=101.6, atr=0.05))
    after = book.positions["btcusdt"]
    assert after.sl >= armed_sl, "止损只能往盈利方向移"
    assert after.sl < 102.0, f"止损不该贴到现价: {after.sl}"


def test_upl_and_upl_pct_are_real():
    book = PaperBook()
    pos = book.tick("btcusdt", ctx(price=100.0, max_lev=10))
    margin = pos["margin"]
    qty = pos["qty"]
    book.tick("btcusdt", ctx(price=101.0, max_lev=10))
    after = book.positions["btcusdt"]
    expected = (101.0 - 100.0) * qty
    assert abs(after.upnl - expected) < 0.01, f"{after.upnl} vs {expected}"
    assert abs(after.upnl_pct - expected / margin * 100) < 0.5


def test_force_refresh_closes_and_reopens():
    book = PaperBook()
    first = book.tick("btcusdt", ctx(price=100.0))
    again = book.force("btcusdt", ctx(price=105.0))
    assert book.stats["closed"] == 1
    assert book.history[-1]["reason"] == "manual"
    assert again["entry"] == 105.0, "刷新后应该按新价格开仓"


def test_snapshot_returns_candles():
    book = PaperBook()
    book.tick("btcusdt", ctx())

    class C:
        open_time = 1; open = 1.0; high = 2.0; low = 0.5; close = 1.5

    snap = book.snapshot(["btcusdt"], lambda s: [C()] * 12)
    assert len(snap) == 1
    assert len(snap[0]["candles"]) == 10, "只带最近 10 根"
    assert set(snap[0]["candles"][0]) == {"t", "o", "h", "l", "c"}


def test_leverage_is_capped():
    """模拟仓不放大杠杆：验证策略，不是赌爆仓。"""
    book = PaperBook()
    pos = book.tick("btcusdt", ctx(max_lev=150))
    assert pos["leverage"] == MAX_PAPER_LEV


def test_drop_missing_clears_position():
    book = PaperBook()
    book.tick("btcusdt", ctx())
    book.drop_missing({"ethusdt"})
    assert "btcusdt" not in book.positions
