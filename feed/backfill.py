from __future__ import annotations

import json
import urllib.request

from core.candle import Candle

REST_URL = "https://fapi.binance.com/fapi/v1/klines"


def fetch_klines(symbol: str, interval: str, limit: int, proxy: str | None = None,
                 timeout: int = 10) -> list[Candle]:
    """启动时回补历史K线（公共接口，免 key，仅启动拉一次）。

    失败时抛异常，由调用方决定是否降级为空历史。
    """
    url = f"{REST_URL}?symbol={symbol.upper()}&interval={interval}&limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": "kline-pattern-monitor/1.0"})
    if proxy:
        handler = urllib.request.ProxyHandler({
            "http": proxy, "https": proxy,
        })
        opener = urllib.request.build_opener(handler)
    else:
        opener = urllib.request.build_opener()
    with opener.open(req, timeout=timeout) as resp:
        rows = json.loads(resp.read().decode())
    out = []
    for r in rows:
        vol = float(r[5])
        # klines[9] = taker_buy_base_asset_volume（主动买成交量），
        # 主动卖 = 总量 - 主动买。都是交易所返回的真实值，不用估算。
        taker_buy = float(r[9]) if len(r) > 9 and r[9] is not None else 0.0
        out.append(Candle(
            open_time=int(r[0]),
            open=float(r[1]), high=float(r[2]), low=float(r[3]), close=float(r[4]),
            volume=vol, trades=int(r[8]), interval=interval, closed=True,
            buy_vol=taker_buy, sell_vol=max(0.0, vol - taker_buy),
        ))
    return out
