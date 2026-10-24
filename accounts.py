"""币安合约测试盘账户数据：签名请求拉取账户权益/未实现盈亏。
仅使用测试盘 key（testnet.binancefuture.com），只读接口，无交易权限要求。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
import urllib.request

BASE = "https://testnet.binancefuture.com"


def signed_request(api_key: str, api_secret: str, path: str, params: dict | None = None,
                   timeout: int = 8):
    params = dict(params or {})
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 10000
    qs = urllib.parse.urlencode(params)
    sig = hmac.new(api_secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    url = f"{BASE}{path}?{qs}&signature={sig}"
    req = urllib.request.Request(url, headers={"X-MBX-APIKEY": api_key})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def fetch_positions(api_key: str, api_secret: str) -> list:
    """拉取有仓位的合约（positionAmt != 0）：symbol / 数量 / 未实现盈亏。"""
    rows = signed_request(api_key, api_secret, "/fapi/v2/positionRisk")
    out = []
    for p in rows:
        amt = float(p.get("positionAmt", 0))
        if abs(amt) < 1e-9:
            continue
        out.append({
            "symbol": p.get("symbol", "").lower(),
            "amt": amt,
            "unrealized": round(float(p.get("unRealizedProfit", 0)), 2),
        })
    return out


def fetch_account(api_key: str, api_secret: str) -> dict:
    """拉取 USDT 本位账户权益：总权益 = 钱包余额 + 未实现盈亏，附当前持仓列表。"""
    balances = signed_request(api_key, api_secret, "/fapi/v2/balance")
    total = upnl = avail = 0.0
    for b in balances:
        if b.get("asset") != "USDT":
            continue
        total += float(b.get("balance", 0))
        upnl += float(b.get("crossUnPnl", 0))
        avail += float(b.get("availableBalance", 0))
    positions = fetch_positions(api_key, api_secret)
    return {
        "balance": round(total + upnl, 2),
        "unrealized": round(upnl, 2),
        "available": round(avail, 2),
        "positions": positions,
        "ts": int(time.time() * 1000),
    }
