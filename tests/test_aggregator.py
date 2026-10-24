import time

from core.aggregator import KlineAggregator
from core.candle import Candle


class Sink:
    def __init__(self):
        self.updates = []
        self.closes = []

    def __call__(self, sym, iv, candle, closed, history):
        if closed:
            self.closes.append(candle)
        else:
            self.updates.append((candle, list(history)))


def make_closed(ot, o, h, l, c, vol=1.0, iv="1m"):
    return Candle(ot, o, h, l, c, vol, 1, iv, True)


def test_buckets_and_rollover():
    sink = Sink()
    agg = KlineAggregator("btcusdt", "1m", sink, sink)
    t0 = 1_700_000_000_000            # 恰好是整分钟
    first_bucket = t0 - t0 % 60_000
    agg.on_trade(100.0, 1.0, t0)
    agg.on_trade(102.0, 2.0, t0 + 1)
    agg.on_trade(99.0, 1.5, t0 + 2)
    assert len(sink.closes) == 0
    assert agg.current.close == 99.0
    assert agg.current.high == 102.0 and agg.current.low == 99.0
    assert abs(agg.current.volume - 4.5) < 1e-9

    # 下一桶第一笔成交 → 上一根立即收线
    agg.on_trade(101.0, 1.0, t0 + 60_000)
    assert len(sink.closes) == 1
    closed = sink.closes[0]
    assert closed.open_time == first_bucket
    assert closed.open == 100.0 and closed.close == 99.0
    assert closed.closed is True
    assert closed in agg.history


def test_seed_open_candle():
    sink = Sink()
    agg = KlineAggregator("btcusdt", "1m", sink, sink)
    now = agg.now_ms()
    cur_bucket = now - now % 60_000
    hist = [make_closed(cur_bucket - 60_000 * (i + 1), 10, 11, 9, 10.5) for i in range(5)]
    cur = Candle(cur_bucket, 50, 51, 49, 50.2, interval="1m", closed=True)
    agg.seed(hist + [cur])
    assert agg.current is cur
    assert agg.current.closed is False          # seed 时进行中K线要复位 closed 标志
    assert len(agg.history) == 5


def test_sweep_force_close():
    sink = Sink()
    agg = KlineAggregator("btcusdt", "1m", sink, sink)
    t0 = 1_700_000_000_000
    agg.on_trade(100.0, 1.0, t0)
    # 时间直接跳到下一桶 + grace 之后，无新成交
    agg.sweep(t0 + 60_000 + 3000)
    assert len(sink.closes) == 1
    assert agg.current is None
    # 之后的成交开新桶
    agg.on_trade(101.0, 1.0, t0 + 120_000)
    assert agg.current.open_time == t0 + 120_000 - t0 % 60_000


def test_history_window_passed_to_engine():
    seen = []

    def cb(sym, iv, candle, closed, history):
        if closed:
            seen.append(len(history))

    agg = KlineAggregator("btcusdt", "1m", cb, cb)
    t0 = 1_700_000_000_000
    agg.on_trade(1.0, 1.0, t0)
    agg.on_trade(1.0, 1.0, t0 + 60_000)
    agg.on_trade(1.0, 1.0, t0 + 120_000)
    assert seen == [1, 2]     # 收线时 history 已含刚收的那根
