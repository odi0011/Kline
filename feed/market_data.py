"""Public derivatives metrics used by the decision layer.

These endpoints are public and intentionally use mainnet data even when orders
are sent to the testnet: the monitor already consumes mainnet aggTrade data.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

PUBLIC_BASE = "https://fapi.binance.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
TIMEOUT = 12


def _get_json(path: str, params: dict, proxy: str | None = None):
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{PUBLIC_BASE}{path}?{query}",
        headers={"User-Agent": UA, "Accept": "application/json"},
    )
    try:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        ) if proxy else urllib.request.build_opener()
        with opener.open(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


def _get_json_any(url: str, proxy: str | None = None):
    """GET 任意 URL（非 fapi 主机上的公开接口用）。失败返回 None。"""
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        ) if proxy else urllib.request.build_opener()
        with opener.open(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


# 各合约的杠杆分档：fapi 的 leverageBracket 需要签名，这里用币安网页端
# 公开的同一份数据（含每档 maxOpenPosLeverage）。
BRACKETS_URL = "https://www.binance.com/bapi/futures/v1/friendly/future/common/brackets"


def max_leverage_map(proxy: str | None = None) -> dict[str, int]:
    """symbol -> 该合约允许的最高杠杆（真实分档上限，非猜测值）。

    BTCUSDT=150、TSLAUSDT=20 这类差异就来自这里：取第一档的
    maxOpenPosLeverage（最低名义规模对应的最高倍数）。
    """
    payload = _get_json_any(BRACKETS_URL, proxy)
    data = payload.get("data") if isinstance(payload, dict) else None
    out: dict[str, int] = {}
    for row in (data or {}).get("brackets") or []:
        try:
            symbol = str(row.get("symbol") or "").upper()
            brackets = row.get("riskBrackets") or []
            if not symbol or not brackets:
                continue
            # 第一档 = 最小名义规模，对应最高可用杠杆
            top = max(int(b.get("maxOpenPosLeverage") or 0) for b in brackets)
            if top > 0:
                out[symbol.lower()] = top
        except (TypeError, ValueError, AttributeError):
            continue
    return out


def open_interest_history(symbol: str, period: str = "5m", limit: int = 30,
                          proxy: str | None = None) -> dict | None:
    rows = _get_json("/futures/data/openInterestHist", {
        "symbol": symbol.upper(), "period": period, "limit": limit,
    }, proxy)
    if not isinstance(rows, list) or len(rows) < 2:
        return None
    try:
        values = [float(row.get("sumOpenInterest") or 0) for row in rows]
        stamps = [int(row.get("timestamp") or 0) for row in rows]
    except (TypeError, ValueError):
        return None
    if values[0] <= 0 or values[-1] <= 0:
        return None
    change = (values[-1] - values[0]) / values[0]
    return {
        "current": values[-1],
        "first": values[0],
        "changePct": change,
        "fromTs": stamps[0],
        "toTs": stamps[-1],
        "period": period,
    }


def funding_snapshot(symbol: str, proxy: str | None = None) -> dict | None:
    row = _get_json("/fapi/v1/premiumIndex", {"symbol": symbol.upper()}, proxy)
    if not isinstance(row, dict):
        return None
    try:
        return {
            "rate": float(row.get("lastFundingRate") or 0),
            "nextTime": int(row.get("nextFundingTime") or 0),
            "markPrice": float(row.get("markPrice") or 0),
            "ts": int(time.time() * 1000),
        }
    except (TypeError, ValueError):
        return None


def ticker_24h(symbol: str, proxy: str | None = None) -> dict | None:
    row = _get_json("/fapi/v1/ticker/24hr", {"symbol": symbol.upper()}, proxy)
    if not isinstance(row, dict):
        return None
    try:
        return {
            "quoteVolume": float(row.get("quoteVolume") or 0),
            "priceChangePercent": float(row.get("priceChangePercent") or 0) / 100,
        }
    except (TypeError, ValueError):
        return None


def ticker_full(symbol: str, proxy: str | None = None) -> dict | None:
    """24h 全字段行情：涨跌、开高低收、成交量、成交额、笔数、买卖盘口价。

    比 ticker_24h 给得多——Agent 被问"这个币 24 小时怎么样"时，
    只有成交额和涨跌幅是答不完整的。
    """
    row = _get_json("/fapi/v1/ticker/24hr", {"symbol": symbol.upper()}, proxy)
    if not isinstance(row, dict):
        return None

    def num(key):
        try:
            return float(row.get(key) or 0)
        except (TypeError, ValueError):
            return None

    return {
        "symbol": str(row.get("symbol") or symbol).lower(),
        "last": num("lastPrice"),
        "changePct": num("priceChangePercent"),
        "change": num("priceChange"),
        "open": num("openPrice"),
        "high": num("highPrice"),
        "low": num("lowPrice"),
        "volume": num("volume"),
        "quoteVolume": num("quoteVolume"),
        "trades": int(num("count") or 0),
        "weightedAvg": num("weightedAvgPrice"),
        "bestBid": num("bidPrice"),
        "bestAsk": num("askPrice"),
        "bidQty": num("bidQty"),
        "askQty": num("askQty"),
        "ts": int(time.time() * 1000),
    }


def recent_trades(symbol: str, limit: int = 100,
                  proxy: str | None = None) -> list[dict] | None:
    """最近的逐笔成交（交易所 /fapi/v1/aggTrades）。

    用途：Agent 要看"刚才这几分钟是谁在打"——每一笔都带方向
    （买方主动/卖方主动），可以直接汇成主动买卖比，比 1 分钟K线细得多。
    返回按时间升序。
    """
    limit = max(1, min(1000, int(limit or 100)))
    rows = _get_json("/fapi/v1/aggTrades", {"symbol": symbol.upper(), "limit": limit},
                     proxy)
    if not isinstance(rows, list):
        return None
    out = []
    for row in rows:
        try:
            price = float(row.get("p") or 0)
            qty = float(row.get("q") or 0)
            # m=True 表示买方是挂单方 → 这一笔是**主动卖出**
            maker = bool(row.get("m"))
            out.append({
                "ts": int(row.get("T") or 0),
                "price": price,
                "qty": qty,
                "notional": round(price * qty, 4),
                "side": "sell" if maker else "buy",
            })
        except (TypeError, ValueError):
            continue
    return out



def onboard_dates(symbols: list[str], proxy: str | None = None) -> dict[str, int]:
    data = _get_json("/fapi/v1/exchangeInfo", {}, proxy)
    wanted = {symbol.upper() for symbol in symbols}
    out = {}
    if not isinstance(data, dict):
        return out
    for row in data.get("symbols") or []:
        symbol = str(row.get("symbol") or "")
        if symbol not in wanted:
            continue
        try:
            # Futures exchangeInfo exposes onboardDate in milliseconds.
            out[symbol.lower()] = int(row.get("onboardDate") or 0)
        except (TypeError, ValueError):
            continue
    return out


def usdt_perpetuals(proxy: str | None = None) -> list[str]:
    """全部可交易的 USDT 本位永续合约（含 TradFi 标的，如 XAUUSDT/TSLAUSDT）。

    前端币种菜单用它列出整个市池，让用户能选到配置里没预先订阅的币。
    """
    data = _get_json("/fapi/v1/exchangeInfo", {}, proxy)
    if not isinstance(data, dict):
        return []
    out = []
    for row in data.get("symbols") or []:
        try:
            symbol = str(row.get("symbol") or "").upper()
            if not symbol.endswith("USDT"):
                continue
            if str(row.get("contractType") or "PERPETUAL") != "PERPETUAL":
                continue
            if str(row.get("status") or "TRADING") != "TRADING":
                continue
            out.append(symbol.lower())
        except (AttributeError, TypeError):
            continue
    return sorted(set(out))


def _first_row(path: str, params: dict, proxy: str | None) -> dict | None:
    rows = _get_json(path, params, proxy)
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return rows[0]
    return None


def _first_row_value(rows) -> dict | None:
    """已经是响应体时取第一行（并发版用，避免再发一次请求）。"""
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return rows[0]
    return None


# 小时级序列的缓存：数据本身一小时才更新一次，没必要每轮都重拉。
# 之前每 60 秒给每个币重新请求一次 24 点序列，五个币就是一次多余的并发风暴，
# 很容易触发交易所限流，表现就是整块数据"取数失败"。
_SERIES_TTL_MS = 10 * 60_000
_series_cache: dict[str, tuple[int, list]] = {}


def top_long_series(symbol: str, proxy: str | None = None) -> list:
    """大户多头占比的近 24 小时序列（小时粒度，缓存 10 分钟）。"""
    sym = symbol.upper()
    now = int(time.time() * 1000)
    cached = _series_cache.get(sym)
    if cached and now - cached[0] < _SERIES_TTL_MS:
        return cached[1]
    history = _get_json("/futures/data/topLongShortPositionRatio",
                        {"symbol": sym, "period": "1h", "limit": 24}, proxy)
    series: list = []
    if isinstance(history, list):
        for item in history:
            try:
                series.append({
                    "ts": int(item.get("timestamp") or 0),
                    "longPct": round(float(item.get("longAccount") or 0) * 100, 2),
                })
            except (TypeError, ValueError):
                continue
    if len(series) >= 2:
        _series_cache[sym] = (now, series)
        return series
    # 失败时退回上一次成功的序列，别让卡片突然空掉
    return cached[1] if cached else []


def _ratio(long_side, short_side):
    """多空账户比：交易所给的是两侧占比，这里换算成 多/空 比值。"""
    try:
        long_side = float(long_side)
        short_side = float(short_side)
    except (TypeError, ValueError):
        return None
    if short_side <= 0:
        return None
    return round(long_side / short_side, 3)


def derivatives_snapshot(symbol: str, period: str = "5m",
                         proxy: str | None = None) -> dict:
    """单个合约的真实衍生品指标（币安 /futures/data 公开接口）。

    这个板块替代了币安 App 的「量化策略」——那套策略数据是 App 专有的
    登录态接口，网页端拿不到。这里给的是同一市场里可公开验证的仓位与
    成交结构数据，全部逐项来自交易所，不含任何推算。
    """
    sym = symbol.upper()
    out: dict = {"symbol": sym.lower(), "ts": int(time.time() * 1000), "period": period}

    # 这些接口互相独立，必须并发取：串行时单币要 12 秒，
    # 线上稍慢就撞到 12 秒超时，整块数据表现为"取数失败/无数据"。
    jobs = {
        "account": ("/futures/data/globalLongShortAccountRatio",
                    {"symbol": sym, "period": period, "limit": 1}),
        "topPos": ("/futures/data/topLongShortPositionRatio",
                   {"symbol": sym, "period": period, "limit": 1}),
        "taker": ("/futures/data/takerlongshortRatio",
                  {"symbol": sym, "period": period, "limit": 1}),
        "oi": ("/fapi/v1/openInterest", {"symbol": sym}),
        "oiHist": ("/futures/data/openInterestHist",
                   {"symbol": sym, "period": period, "limit": 12}),
        "premium": ("/fapi/v1/premiumIndex", {"symbol": sym}),
        "ticker": ("/fapi/v1/ticker/24hr", {"symbol": sym}),
    }
    got: dict = {}
    with ThreadPoolExecutor(max_workers=len(jobs) + 1) as pool:
        futures = {name: pool.submit(_get_json, path, params, proxy)
                   for name, (path, params) in jobs.items()}
        futures["series"] = pool.submit(top_long_series, sym, proxy)
        for name, future in futures.items():
            try:
                got[name] = future.result()
            except Exception:
                got[name] = None

    row = _first_row_value(got.get("account"))
    if row:
        out["accountLongShort"] = _ratio(row.get("longAccount"), row.get("shortAccount"))
        out["accountLongPct"] = round(float(row.get("longAccount") or 0) * 100, 2)

    row = _first_row_value(got.get("topPos"))
    if row:
        out["topPositionLongShort"] = _ratio(row.get("longAccount"), row.get("shortAccount"))
        out["topPositionLongPct"] = round(float(row.get("longAccount") or 0) * 100, 2)

    # 大户多头占比的近 24 小时走势：列表卡片用来判断"在加仓还是在减仓"
    series = got.get("series") or []
    if len(series) >= 2:
        out["topLongSeries"] = series
        first, last = series[0]["longPct"], series[-1]["longPct"]
        out["topLongChange24h"] = round(last - first, 2)

    row = _first_row_value(got.get("taker"))
    if row:
        try:
            out["takerBuySellRatio"] = round(float(row.get("buySellRatio") or 0), 3)
            buy_vol = float(row.get("buyVol") or 0)
            sell_vol = float(row.get("sellVol") or 0)
            total = buy_vol + sell_vol
            if total > 0:
                out["takerBuyPct"] = round(buy_vol / total * 100, 2)
        except (TypeError, ValueError):
            pass

    row = got.get("oi")
    if isinstance(row, dict):
        try:
            out["openInterest"] = float(row.get("openInterest") or 0)
        except (TypeError, ValueError):
            pass

    oi_hist = got.get("oiHist")
    if isinstance(oi_hist, list) and len(oi_hist) >= 2:
        try:
            values = [float(r.get("sumOpenInterest") or 0) for r in oi_hist]
            if values[0] > 0:
                out["oiChangePct"] = round((values[-1] - values[0]) / values[0] * 100, 2)
        except (TypeError, ValueError):
            pass

    row = got.get("premium")
    if isinstance(row, dict):
        try:
            out["fundingRate"] = float(row.get("lastFundingRate") or 0)
            out["nextFundingMs"] = int(row.get("nextFundingTime") or 0)
        except (TypeError, ValueError):
            pass

    row = got.get("ticker")
    if isinstance(row, dict):
        try:
            out["price"] = float(row.get("lastPrice") or 0)
            out["changePct"] = round(float(row.get("priceChangePercent") or 0), 2)
            out["quoteVolume"] = float(row.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            pass
    return out


