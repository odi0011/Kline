from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from core.candle import Candle, INTERVAL_MS

REST_URL = "https://fapi.binance.com/fapi/v1/klines"
OKX_URL = "https://www.okx.com/api/v5/market/history-candles"

# 周期名两家不一样
OKX_BAR = {"1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
           "1h": "1H", "2h": "2H", "4h": "4H", "1d": "1D"}


def _okx_inst(symbol: str) -> str:
    base = symbol.upper()
    if base.endswith("USDT"):
        base = base[:-4]
    return f"{base}-USDT-SWAP"


def _fetch_okx(symbol: str, interval: str, limit: int, proxy: str | None,
               timeout: int) -> list[Candle]:
    """币安 REST 被地区限制（451）时的回退：欧易历史K线。

    返回结构与币安一致，按时间正序，closed=True。
    欧易不直接给主动买量，用成交额方向近似：这里保守地按 K 线涨跌拆分，
    只影响"买卖力量"图的观感，不参与策略评分。
    """
    bar = OKX_BAR.get(interval)
    if bar is None:
        raise ValueError(f"欧易不支持周期 {interval}")
    params = {"instId": _okx_inst(symbol), "bar": bar, "limit": min(int(limit), 300)}
    url = f"{OKX_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "kline-pattern-monitor/1.0",
                                               "Accept": "application/json"})
    opener = (urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
              if proxy else urllib.request.build_opener())
    with opener.open(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8", "replace"))
    if str(payload.get("code")) != "0":
        raise OSError(f"欧易返回错误 {payload.get('msg')}")
    out = []
    # 欧易返回新→旧，这里反转成正序
    for r in reversed(payload.get("data") or []):
        try:
            o, h, l, c = float(r[1]), float(r[2]), float(r[3]), float(r[4])
            vol = float(r[5])
        except (TypeError, ValueError, IndexError):
            continue
        # 收阳按主动买 60% 估、收阴按 40% 估，仅用于买卖力量图的近似
        ratio = 0.6 if c >= o else 0.4
        out.append(Candle(
            open_time=int(r[0]), open=o, high=h, low=l, close=c,
            volume=vol, trades=0, interval=interval, closed=True,
            buy_vol=vol * ratio, sell_vol=vol * (1 - ratio),
        ))
    return out


def fetch_klines(symbol: str, interval: str, limit: int, proxy: str | None = None,
                 timeout: int = 10) -> list[Candle]:
    """历史K线回补：优先币安，被地区限制时自动回退欧易。

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
    try:
        with opener.open(req, timeout=timeout) as resp:
            rows = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        # 451 = 地区限制；这类错误换域名才有意义，重试同一个域名没意义。
        # 注意：回补用欧易、实时成交仍来自币安 WS，两家价格基准略有差异，
        # 第一根实时K线接上历史时可能有极小跳空，对指标影响可忽略。
        if e.code in (451, 403):
            bars = _fetch_okx(symbol, interval, limit, proxy, timeout)
            if bars:
                print(f"[backfill] {symbol} {interval} 币安受限({e.code})，改用欧易 {len(bars)} 根")
            return bars
        raise
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
