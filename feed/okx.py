"""欧易（OKX）衍生品指标：币安 REST 被地区限制（HTTP 451）时的回退数据源。

币安 WS 域名可用、REST 域名被 451，所以线上只有行情能跑、指标全空。
欧易在同一台机器上全部可访问，这里取等价字段，保持与币安版本同样的
输出结构，前端不需要区分来自哪家。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = "https://www.okx.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
TIMEOUT = 10


def _get(path: str, params: dict | None = None):
    query = urllib.parse.urlencode(params or {})
    url = f"{BASE}{path}" + (f"?{query}" if query else "")
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    if not isinstance(payload, dict) or str(payload.get("code")) != "0":
        return None
    return payload.get("data")


def _swap(symbol: str) -> str:
    """btcusdt -> BTC-USDT-SWAP；xauusdt -> XAU-USDT-SWAP。"""
    base = symbol.upper()
    if base.endswith("USDT"):
        base = base[:-4]
    return f"{base}-USDT-SWAP"


def _num(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def derivatives_snapshot(symbol: str, period: str = "5m") -> dict:
    """与 market_data.derivatives_snapshot 同结构的欧易版本。

    所有接口互相独立，必须并发取——串行要 12 秒以上，等于每个币都超时。
    """
    sym = symbol.upper()
    inst = _swap(sym)
    ccy = inst.split("-")[0]
    out: dict = {"symbol": sym.lower(), "ts": int(time.time() * 1000),
                 "period": period, "venue": "okx"}

    jobs = {
        "tick": ("/api/v5/market/ticker", {"instId": inst}),
        "ratio": ("/api/v5/rubik/stat/contracts/long-short-account-ratio",
                  {"ccy": ccy, "period": period}),
        "taker": ("/api/v5/rubik/stat/taker-volume", {"ccy": ccy, "instType": "CONTRACTS"}),
        "oi": ("/api/v5/public/open-interest", {"instId": inst}),
        "oiHist": ("/api/v5/rubik/stat/contracts/open-interest-volume",
                   {"ccy": ccy, "period": period}),
        "funding": ("/api/v5/public/funding-rate", {"instId": inst}),
        "hist": ("/api/v5/rubik/stat/contracts/long-short-account-ratio",
                 {"ccy": ccy, "period": "1H"}),
    }
    got: dict = {}
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {name: pool.submit(_get, path, params)
                   for name, (path, params) in jobs.items()}
        for name, future in futures.items():
            try:
                got[name] = future.result()
            except Exception:
                got[name] = None

    tick = got.get("tick")
    if tick:
        row = tick[0]
        last = _num(row.get("last"))
        open24 = _num(row.get("open24h"))
        out["price"] = last
        if last is not None and open24:
            out["changePct"] = round((last - open24) / open24 * 100, 2)
        vol_ccy = _num(row.get("volCcy24h"))
        if vol_ccy is not None:
            # 欧易 volCcy24h 是币本位量，乘价格近似 USDT 名义额，供排序用
            out["quoteVolume"] = vol_ccy * (last or 0)

    # 多空账户比（等价币安的 globalLongShortAccountRatio）
    ratio_rows = got.get("ratio")
    if isinstance(ratio_rows, list) and ratio_rows:
        try:
            ratio = float(ratio_rows[0][1])
            out["accountLongShort"] = round(ratio, 3)
            if ratio > 0:
                out["accountLongPct"] = round(ratio / (1 + ratio) * 100, 2)
        except (TypeError, ValueError, IndexError):
            pass

    # 主动买卖量（等价 takerlongshortRatio）
    taker_rows = got.get("taker")
    if isinstance(taker_rows, list) and taker_rows:
        try:
            buy_vol, sell_vol = float(taker_rows[0][1]), float(taker_rows[0][2])
            total = buy_vol + sell_vol
            if sell_vol > 0:
                out["takerBuySellRatio"] = round(buy_vol / sell_vol, 3)
            if total > 0:
                out["takerBuyPct"] = round(buy_vol / total * 100, 2)
        except (TypeError, ValueError, IndexError):
            pass

    # 持仓量
    oi = got.get("oi")
    if oi:
        try:
            out["openInterest"] = float(oi[0].get("oiCcy") or 0)
        except (TypeError, ValueError, IndexError):
            pass

    # 持仓量近 1 小时变化（OI 与成交额的历史序列）
    oi_hist = got.get("oiHist")
    if isinstance(oi_hist, list) and len(oi_hist) >= 2:
        try:
            values = [float(row[1]) for row in oi_hist[:12]]
            if values and values[-1] > 0:
                out["oiChangePct"] = round((values[0] - values[-1]) / values[-1] * 100, 2)
        except (TypeError, ValueError, IndexError):
            pass

    # 资金费率
    funding = got.get("funding")
    if funding:
        try:
            out["fundingRate"] = float(funding[0].get("fundingRate") or 0)
            out["nextFundingMs"] = int(funding[0].get("fundingTime") or 0)
        except (TypeError, ValueError, IndexError):
            pass

    # 多空比近 24 点序列（大户持仓卡片的折线用这个顶替）
    hist = got.get("hist")
    if isinstance(hist, list) and len(hist) >= 2:
        series = []
        for row in reversed(hist[-24:]):
            try:
                ratio = float(row[1])
                series.append({"ts": int(row[0]), "longPct": round(ratio / (1 + ratio) * 100, 2)})
            except (TypeError, ValueError, IndexError):
                continue
        if len(series) >= 2:
            out["topLongSeries"] = series
            out["topLongChange24h"] = round(series[-1]["longPct"] - series[0]["longPct"], 2)
            # 欧易没有"大户持仓"这一维度，用整体多空占比作为同义展示
            out["topPositionLongPct"] = series[-1]["longPct"]
            ratio = _num(out.get("accountLongShort"))
            if ratio:
                out["topPositionLongShort"] = ratio

    return out
