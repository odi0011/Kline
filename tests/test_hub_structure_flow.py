"""结构分析的下发时机：只在收线重算后推送，不跟着每笔 tick 重发。

线上实测（10 秒窗口）：
  structure  200 次 / 271KB   ← 每笔 tick 都推整份形态+趋势线
  candle     200 次 /  24KB
结构数据约 1.4KB/份，跟着 tick 推等于给每个客户端灌 27KB/s，
叠加前端 32 个 canvas 各自重画，是页面卡顿的元凶。
"""
import asyncio

from server.hub import BroadcastHub


class _Agg:
    history = []
    current = None


def _hub():
    hub = BroadcastHub({}, ["btcusdt"], ["1m"])
    sent = []

    async def broadcast(msg):
        sent.append(msg)

    hub._broadcast = broadcast
    return hub, sent


class _Candle:
    """最小可用 candle（hub._bar 只读这几个字段）。"""
    open_time = 1_700_000_000_000
    open = high = low = close = 100.0
    volume = 1.0
    buy_vol = sell_vol = 0.0


def _candle():
    return _Candle()


def _bar(price=100.0, ts=1_700_000_000_000):
    return {"t": ts, "o": price, "h": price, "l": price, "c": price, "v": 1}


def test_structure_only_pushed_on_close():
    """逐笔 tick 只推 candle；结构只在收线时推一次。"""
    hub, sent = _hub()
    hub.analysis_fn = lambda sym, iv, hist: {"pattern": {"key": "hammer"}, "levels": []}
    hub.latest[("btcusdt", "1m")] = _bar()

    # 10 笔 tick，全部未收线
    for i in range(10):
        hub.on_update("btcusdt", "1m", _candle(), False, [])
        hub.dirty.add(("btcusdt", "1m"))
    asyncio.run(_flush(hub))

    kinds = [m["type"] for m in sent]
    assert kinds.count("candle") == 1, f"只该推一次（去重后）: {kinds}"
    assert kinds.count("structure") == 0, "未收线不该推结构"


def test_structure_pushed_once_when_bar_closes():
    hub, sent = _hub()
    hub.analysis_fn = lambda sym, iv, hist: {"pattern": {"key": "hammer"}, "levels": []}
    hub.latest[("btcusdt", "1m")] = _bar()

    hub.on_update("btcusdt", "1m", _candle(), False, [])
    hub.on_update("btcusdt", "1m", _candle(), False, [])
    hub.on_update("btcusdt", "1m", _candle(), True, [])   # 收线
    asyncio.run(_flush(hub))

    kinds = [m["type"] for m in sent]
    assert kinds.count("structure") == 1, f"收线只该推一次结构: {kinds}"


def test_structure_not_resent_without_recalc():
    """没有新的收线就不该再推结构（哪怕又来了很多 tick）。"""
    hub, sent = _hub()
    hub.analysis_fn = lambda sym, iv, hist: {"pattern": {"key": "hammer"}}
    hub.latest[("btcusdt", "1m")] = _bar()

    hub.on_update("btcusdt", "1m", _candle(), True, [])
    asyncio.run(_flush(hub))
    sent.clear()

    for _ in range(20):
        hub.on_update("btcusdt", "1m", _candle(), False, [])
    asyncio.run(_flush(hub))

    assert [m["type"] for m in sent].count("structure") == 0


async def _flush(hub):
    """跑一轮 hub 的发送循环体（只处理 dirty/结构两段）。"""
    if hub.dirty:
        dirty, hub.dirty = hub.dirty, set()
        for key in dirty:
            bar = hub.latest.get(key)
            if bar:
                await hub._broadcast({"type": "candle", "symbol": key[0],
                                      "interval": key[1], "bar": bar})
    if hub._structure_dirty:
        s_dirty, hub._structure_dirty = hub._structure_dirty, set()
        for key in s_dirty:
            st = hub.structures.get(key)
            if st:
                await hub._broadcast({"type": "structure", "symbol": key[0],
                                      "interval": key[1], "structure": st})
