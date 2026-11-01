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

    row = _first_row("/futures/data/globalLongShortAccountRatio",
                     {"symbol": sym, "period": period, "limit": 1}, proxy)
    if row:
        out["accountLongShort"] = _ratio(row.get("longAccount"), row.get("shortAccount"))
        out["accountLongPct"] = round(float(row.get("longAccount") or 0) * 100, 2)

    row = _first_row("/futures/data/topLongShortPositionRatio",
                     {"symbol": sym, "period": period, "limit": 1}, proxy)
    if row:
        out["topPositionLongShort"] = _ratio(row.get("longAccount"), row.get("shortAccount"))
        out["topPositionLongPct"] = round(float(row.get("longAccount") or 0) * 100, 2)

    row = _first_row("/futures/data/topLongShortAccountRatio",
                     {"symbol": sym, "period": period, "limit": 1}, proxy)
    if row:
        out["topAccountLongShort"] = _ratio(row.get("longAccount"), row.get("shortAccount"))

    row = _first_row("/futures/data/takerlongshortRatio",
                     {"symbol": sym, "period": period, "limit": 1}, proxy)
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

    row = _get_json("/fapi/v1/openInterest", {"symbol": sym}, proxy)
    if isinstance(row, dict):
        try:
            out["openInterest"] = float(row.get("openInterest") or 0)
        except (TypeError, ValueError):
            pass

    oi_hist = _get_json("/futures/data/openInterestHist",
                        {"symbol": sym, "period": period, "limit": 12}, proxy)
    if isinstance(oi_hist, list) and len(oi_hist) >= 2:
        try:
            values = [float(r.get("sumOpenInterest") or 0) for r in oi_hist]
            if values[0] > 0:
                out["oiChangePct"] = round((values[-1] - values[0]) / values[0] * 100, 2)
        except (TypeError, ValueError):
            pass

    row = _get_json("/fapi/v1/premiumIndex", {"symbol": sym}, proxy)
    if isinstance(row, dict):
        try:
            out["fundingRate"] = float(row.get("lastFundingRate") or 0)
            out["nextFundingMs"] = int(row.get("nextFundingTime") or 0)
        except (TypeError, ValueError):
            pass

    row = _get_json("/fapi/v1/ticker/24hr", {"symbol": sym}, proxy)
    if isinstance(row, dict):
        try:
            out["price"] = float(row.get("lastPrice") or 0)
            out["changePct"] = round(float(row.get("priceChangePercent") or 0), 2)
            out["quoteVolume"] = float(row.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            pass
    return out


