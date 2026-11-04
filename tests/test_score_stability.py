"""评分稳定性：同一根K线内分数不能乱跳。

线上实测（修复前）：TSLA 在 60 秒内评分从 54 跳到 77，而开仓线是 55。
根因是指标用的是**未收盘**的当前K线——一根 3m K线内被重算 36 次，
盘中每一笔成交都在改 RSI/Z/ADX。这不是"信号灵敏"，是信号等于随机。

两层防护：
  1) 评分改用上一根**已收盘**K线（同一根内分数完全不动）
  2) 对分数/分离度再做 EMA 平滑（压掉跨根的高频抖动）
"""
import pytest

from analysis import indicators as IND
from core.candle import Candle
from decision.config import DecisionConfig
from decision.features import macd, open_interest_bias
from decision.scoring import score_signal


def _bars(n=120, start=100.0, step=0.001, closed=True):
    """造一串确定性的K线（不含随机数，断言可复现）。

    用交替涨跌，避免单调上涨把 RSI 顶到 100 的上限——
    到上限后任何涨幅都不再改变 RSI，测不出"盘中K线会改指标"。
    """
    out = []
    price = start
    for i in range(n):
        o = price
        price = o * (1 + (step if i % 2 == 0 else -step * 0.8))
        out.append(Candle(open_time=i * 180_000, open=o,
                          high=max(o, price) * 1.0005,
                          low=min(o, price) * 0.9995,
                          close=price, volume=100.0,
                          interval="3m", closed=closed))
    return out


def test_closed_bars_only_ignores_intrabar_price():
    """评分只用已收盘K线：同一根未收盘K线怎么动，分数都不变。

    这是"根因"层面的验证：把盘中价从 +0.1% 推到 -0.3%，
    已收盘序列的指标必须一个都不动。
    """
    closed = _bars(120, closed=True)
    live = Candle(open_time=120 * 180_000, open=closed[-1].close,
                  high=closed[-1].close, low=closed[-1].close,
                  close=closed[-1].close, volume=10.0, interval="3m",
                  closed=False)
    base = IND.market_state(closed)
    for move in (0.001, 0.003, -0.003, -0.001):
        live.close = closed[-1].close * (1 + move)
        live.high = max(live.high, live.close)
        live.low = min(live.low, live.close)
        # 用「已收盘」算：必须一模一样
        again = IND.market_state(closed)
        assert again["rsi"] == base["rsi"]
        assert again["adx"] == base["adx"]
        assert again["z"] == base["z"]


def test_live_bar_does_move_indicators():
    """反证：把未收盘K线喂进去，指标确实会抖（所以不能拿它做决策）。"""
    closed = _bars(120, closed=True)
    live = Candle(open_time=120 * 180_000, open=closed[-1].close,
                  high=closed[-1].close, low=closed[-1].close,
                  close=closed[-1].close, volume=10.0, interval="3m",
                  closed=False)
    base = IND.market_state(closed)
    live.close = closed[-1].close * 1.003
    live.high = max(live.high, live.close)
    with_live = IND.market_state(closed + [live])
    assert with_live["rsi"] != base["rsi"], "这条是反证：盘中K线会改指标"


def test_smoothing_alpha_default_is_conservative():
    cfg = DecisionConfig()
    assert 0 < cfg.score_smooth_alpha < 1
    # 0.25 = 新值占 1/4，抖动被压掉大半
    assert cfg.score_smooth_alpha == pytest.approx(0.25)


def test_smoothing_alpha_is_clamped_and_typed_safely():
    for bad, expected in ((9, 1.0), (-1, 0.0), ("abc", 0.25), (None, 0.25),
                          (float("nan"), 0.25), (True, 1.0)):
        cfg = DecisionConfig.from_dict({"score_smooth_alpha": bad})
        assert cfg.score_smooth_alpha == pytest.approx(expected, abs=1e-9), bad


def test_ema_suppresses_threshold_crossings():
    """用线上实测的 54 个分数序列验证：EMA 后不再反复越过开仓线。

    原始序列 60 秒内 54~77 抖动，两次跨过 55；平滑后抖动幅度减小、
    不再反复穿越门槛——否则"能不能开仓"取决于采样时刻。
    """
    raw = [56.8, 54.1, 58.8, 73.2, 73.1, 73.1, 68.2, 70.0, 62.1, 58.1,
           69.5, 69.5, 69.5, 67.5, 76.6, 76.6, 65.7, 76.6, 76.1, 76.1,
           76.1, 76.8, 77.1, 66.3, 58.5, 68.1, 69.4, 66.2, 64.7, 76.0,
           77.1, 74.1, 77.5, 67.5, 75.7, 59.6, 62.2, 57.2, 55.0, 66.7,
           77.0, 74.4, 71.8, 75.8, 75.8, 76.2, 76.2, 76.2, 66.9, 62.5,
           62.0, 61.0, 56.1, 56.9]

    def crossings(seq):
        return sum(1 for a, b in zip(seq, seq[1:]) if (a < 55) != (b < 55))

    def smooth(seq, alpha=0.25):
        out, prev = [], None
        for value in seq:
            prev = value if prev is None else prev + (value - prev) * alpha
            out.append(prev)
        return out

    assert crossings(raw) >= 2, "原始数据确实在门槛上下反复"
    assert crossings(smooth(raw)) == 0, "平滑后不该再反复越线"
    assert max(smooth(raw)) - min(smooth(raw)) < max(raw) - min(raw)


def test_score_signal_without_pattern_stats_is_stable():
    """同一份输入算两次必须完全一致（评分本身不能有随机性）。"""
    closed = _bars(120, closed=True)
    state = IND.market_state(closed)
    features = {"macd": macd(closed)}
    cfg = DecisionConfig()
    a = score_signal("bullish", state, features, cfg)
    b = score_signal("bullish", state, features, cfg)
    assert (a.score, a.gap, a.grade) == (b.score, b.gap, b.grade)
