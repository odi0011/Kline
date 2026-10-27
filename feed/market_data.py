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
