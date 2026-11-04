"""交易账户数据：签名请求拉取账户权益/未实现盈亏。

系统口径里这个账户**就是实盘账户**——真正下单、真正有权益与持仓的那一个。
BASE 目前指向币安合约测试网，只是部署时用的 key 属于测试环境；
这属于运维细节，不影响它在系统里的身份，也不要把它和「模拟仓」混为一谈
（模拟仓是 decision/paper.py 里的纸上交易，不产生任何委托）。
只读接口，无交易权限要求。
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


def cancel_algo_orders(api_key: str, api_secret: str, symbol: str) -> int:
    """撤掉某个币种挂着的算法条件单（止损/止盈），返回撤掉的数量。

    止损走的是 /fapi/v1/algoOrder（见 place_protective_order），
    所以 cancel_all_open_orders 撤不到它。平仓后不撤就会留下孤儿单：
    仓位没了、止损单还挂着，会一直累积（线上实测留下两张，
    其中一张名义 0.623 BTC）。
    """
    rows = signed_request(api_key, api_secret, "/fapi/v1/openAlgoOrders",
                          {"symbol": symbol.upper()})
    items = (rows.get("orders", rows) if isinstance(rows, dict) else rows) or []
    removed = 0
    for order in items:
        algo_id = order.get("algoId") if isinstance(order, dict) else None
        if not algo_id:
            continue
        try:
            signed_request(api_key, api_secret, "/fapi/v1/algoOrder",
                           {"algoId": algo_id}, method="DELETE")
            removed += 1
        except Exception:
            # 单张失败不中断：剩下的继续撤，最终数量如实返回
            continue
    return removed


def open_orders(api_key: str, api_secret: str, symbol: str) -> list:
    """Read-only protection-order audit."""
    return signed_request(api_key, api_secret, "/fapi/v1/openOrders",
                          {"symbol": symbol.upper()})


def algo_orders(api_key: str, api_secret: str, symbol: str = "") -> list:
    """当前挂着的算法条件单（止损/止盈）。

    止损走的是 /fapi/v1/algoOrder，**不在** /fapi/v1/openOrders 里。
    只查 openOrders 会得出"没有任何挂单保护"的错误结论——Agent 之前
    就是靠 positions 里的 sl 字段猜的，压根没查过交易所。
    symbol 为空时查全部合约。
    """
    params = {"symbol": symbol.upper()} if symbol else {}
    rows = signed_request(api_key, api_secret, "/fapi/v1/openAlgoOrders", params)
    return (rows.get("orders", rows) if isinstance(rows, dict) else rows) or []


def protection_snapshot(api_key: str, api_secret: str, positions: list) -> dict:
    """把"每个持仓有没有挂保护单"查清楚——这是用户最常问的一句话。

    输出两类：
      · symbols：每个合约当前的普通挂单 + 算法条件单（原始精简字段）
      · gaps：有仓但没找到 reduceOnly 条件单的合约（裸奔警告）

    只读，不下单、不撤单。
    """
    syms = sorted({str(p.get("symbol") or "").lower()
                   for p in positions or [] if p.get("symbol")})
    symbols, gaps, errors = {}, [], []
    for sym in syms:
        try:
            plain = open_orders(api_key, api_secret, sym) or []
        except Exception as exc:
            plain = []
            errors.append(f"{sym} 普通挂单: {str(exc)[:80]}")
        try:
            algo = algo_orders(api_key, api_secret, sym)
        except Exception as exc:
            algo = []
            errors.append(f"{sym} 条件单: {str(exc)[:80]}")
        keep = ("symbol", "side", "type", "origQty", "price", "stopPrice",
                "triggerPrice", "reduceOnly", "closePosition", "timeInForce", "status")
        algo_keep = ("symbol", "side", "type", "quantity", "triggerPrice",
                     "reduceOnly", "closePosition", "algoStatus", "algoId", "bookTime")
        rows = [{k: o.get(k) for k in keep if k in o} for o in plain if isinstance(o, dict)]
        rows += [{"algo": True, **{k: o.get(k) for k in algo_keep if k in o}}
                 for o in algo if isinstance(o, dict)]
        symbols[sym] = rows
        # 有仓却没有任何"只减仓/全平"条件单 = 保护缺失
        protected = any(
            (o.get("reduceOnly") in (True, "true")) or (o.get("closePosition") in (True, "true"))
            for o in algo if isinstance(o, dict))
        if not protected:
            gaps.append(sym)
    return {"symbols": symbols, "gaps": gaps, "errors": errors}

def cancel_all_open_orders(api_key: str, api_secret: str, symbol: str) -> dict:
    return signed_request(api_key, api_secret, "/fapi/v1/allOpenOrders",
                          {"symbol": symbol.upper()}, method="DELETE")


def cancel_order(api_key: str, api_secret: str, symbol: str, order_id: int) -> dict:
    return signed_request(api_key, api_secret, "/fapi/v1/order", {
        "symbol": symbol.upper(), "orderId": int(order_id),
    }, method="DELETE")


def query_order(api_key: str, api_secret: str, symbol: str, order_id: int) -> dict:
    return signed_request(api_key, api_secret, "/fapi/v1/order", {
        "symbol": symbol.upper(), "orderId": int(order_id),
    })


def place_protective_order(api_key: str, api_secret: str, symbol: str,
                           side: str, order_type: str, qty: float,
                           stop_price: float) -> dict:
    """挂交易所侧止损/止盈（进程挂了也有效）。

    **必须走 /fapi/v1/algoOrder**：老的 /fapi/v1/order + STOP_MARKET
    在现在的币安合约上会直接被拒（-4120 "Order type not supported for
    this endpoint, please use the Algo Order API endpoints instead"）。
    线上实测下单成功但止损单全失败，仓位裸奔。

    参数要点：
      algoType=CONDITIONAL  条件单
      triggerPrice          触发价（新接口叫这个，不叫 stopPrice）
      reduceOnly            只减仓，不会反向开新仓
      workingType=MARK_PRICE 用标记价触发，避免插针误触
    """
    if order_type not in ("STOP_MARKET", "TAKE_PROFIT_MARKET"):
        raise ValueError(f"unsupported protective order type: {order_type}")
    body = {
        "algoType": "CONDITIONAL",
        "symbol": symbol.upper(),
        "side": side.upper(),
        "type": order_type,
        "quantity": f"{qty:.12f}".rstrip("0").rstrip("."),
        "triggerPrice": f"{stop_price:.12f}".rstrip("0").rstrip("."),
        "workingType": "MARK_PRICE",
        "reduceOnly": "true",
    }
    try:
        return signed_request(api_key, api_secret, "/fapi/v1/algoOrder",
                              body, method="POST")
    except Exception as first:
        # 老账户/老环境可能还没有 algoOrder 路由，退回旧接口再试一次。
        # 旧接口在新环境会报 -4120，但留着这层对老部署更稳。
        if "-4120" not in str(first) and "404" not in str(first):
            raise
        return signed_request(api_key, api_secret, "/fapi/v1/order", {
            "symbol": body["symbol"], "side": body["side"], "type": order_type,
            "quantity": body["quantity"], "stopPrice": body["triggerPrice"],
            "workingType": "MARK_PRICE", "reduceOnly": "true",
            "newOrderRespType": "RESULT",
        }, method="POST")
