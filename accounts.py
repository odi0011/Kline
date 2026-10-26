"""币安合约测试盘账户数据：签名请求拉取账户权益/未实现盈亏。
仅使用测试盘 key（testnet.binancefuture.com），只读接口，无交易权限要求。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://testnet.binancefuture.com"


def signed_request(api_key: str, api_secret: str, path: str, params: dict | None = None,
                   timeout: int = 8, method: str = "GET"):
    params = dict(params or {})
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 10000
    qs = urllib.parse.urlencode(params)
    sig = hmac.new(api_secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    url = f"{BASE}{path}?{qs}&signature={sig}"
    # 下单/撤单类必须 POST；查询类用 GET（币安对 POST 参数走 body 校验）
    data = None
    if method.upper() == "POST":
        data = b""
    req = urllib.request.Request(url, data=data, headers={"X-MBX-APIKEY": api_key},
                                 method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        # 币安把错误码放在响应体里（如 -4411 未签 TradFi 协议）。
        # 不读 body 就只能拿到 "HTTP Error 400"，上层无法区分错误类型。
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            body = ""
        raise RuntimeError(f"HTTP {e.code}: {body or e.reason}") from e


def fetch_positions(api_key: str, api_secret: str) -> list:
    """拉取有仓位的合约（positionAmt != 0）。

    给出卡片需要的完整字段：数量、入场价、标记价、未实现盈亏与收益率、
    杠杆、强平价、保证金、名义价值。收益率按保证金口径算（PnL / 占用保证金），
    这样不同杠杆的仓位可以直接横向比较。
    """
    rows = signed_request(api_key, api_secret, "/fapi/v2/positionRisk")
    out = []
    for p in rows:
        amt = float(p.get("positionAmt", 0))
        if abs(amt) < 1e-9:
            continue
        entry = float(p.get("entryPrice", 0) or 0)
        mark = float(p.get("markPrice", 0) or 0)
        upnl = float(p.get("unRealizedProfit", 0) or 0)
        leverage = int(float(p.get("leverage", 0) or 0))
        # 逐仓用 isolatedMargin，全仓的 margin 字段为 0/None
        isolated = bool(p.get("isolated", False))
        margin = float(p.get("margin", 0) or 0)
        if margin <= 0:
            margin = float(p.get("isolatedMargin", 0) or 0)
        notional = abs(float(p.get("notional", 0) or 0))
        if notional <= 0 and mark > 0:
            notional = abs(amt) * mark
        # 全仓仓位两处都拿不到保证金，按 名义价值/杠杆 估算
        if margin <= 0 and leverage > 0:
            margin = notional / leverage
        # 收益率：优先用保证金口径；保证金为 0（全仓已用尽）时退回名义价值口径
        base = margin if margin > 0 else notional
        upnl_pct = (upnl / base * 100) if base > 0 else 0.0
        out.append({
            "symbol": p.get("symbol", "").lower(),
            "amt": amt,
            "entry": round(entry, 8),
            "mark": round(mark, 8),
            "unrealized": round(upnl, 2),
            "unrealized_pct": round(upnl_pct, 2),
            "leverage": leverage,
            "liquidation": round(float(p.get("liquidationPrice", 0) or 0), 8),
            "margin": round(margin, 2),
            "notional": round(notional, 2),
            "isolated": isolated,
        })
    # 保证金占用大的排前面（主力仓位优先可见）
    out.sort(key=lambda x: -x["margin"])
    return out


def fetch_account(api_key: str, api_secret: str) -> dict:
    """拉取 USDT 本位账户权益：总权益 = 钱包余额 + 未实现盈亏，附当前持仓列表。

    注意：不能用 balance 接口的 crossUnPnl 当浮盈——它只统计全仓持仓，
    逐仓(逐仓保证金模式)仓位的浮盈完全不在里面，会显示成 0。
    统一改为按 positionRisk 里每个持仓的 unRealizedProfit 求和，
    全仓/逐仓都能覆盖。
    """
    balances = signed_request(api_key, api_secret, "/fapi/v2/balance")
    wallet = avail = 0.0
    for b in balances:
        if b.get("asset") != "USDT":
            continue
        wallet += float(b.get("balance", 0))
        avail += float(b.get("availableBalance", 0))
    positions = fetch_positions(api_key, api_secret)
    upnl = sum(p["unrealized"] for p in positions)
    return {
        "balance": round(wallet + upnl, 2),
        "unrealized": round(upnl, 2),
        "available": round(avail, 2),
        "positions": positions,
        "ts": int(time.time() * 1000),
    }
