"""手动开仓：BTCUSDT 1/3仓做多，杠杆从 150 向下试探交易所允许的最高档，
止损单挂在强平价上方 0.2%（STOP_MARKET 全平），并注册引擎放行（不干预止盈止损）。"""
import json
import math
import os

import yaml

from accounts import signed_request

CFG = yaml.safe_load(open("config.yaml", encoding="utf-8"))
acct = CFG["accounts"][0]              # ODDD测试盘1
KEY, SECRET = acct["api_key"], acct["api_secret"]
ACCT_NAME = acct["name"]
SYMBOL = "BTCUSDT"
LEV_WANT = 150
FRACTION = 3                            # 1/3 仓
SL_ABOVE_LIQ = 1.002                    # 止损 = 强平价 × 1.002（强平线上方一点点）

# 1. 可用余额
bal = signed_request(KEY, SECRET, "/fapi/v2/balance")
usdt = next(b for b in bal if b["asset"] == "USDT")
avail = float(usdt["availableBalance"])
margin = round(avail / FRACTION, 2)

# 2. 交易所规则（步长/价格精度/杠杆分档）
info = signed_request(KEY, SECRET, "/fapi/v1/exchangeInfo")
sym = next(s for s in info["symbols"] if s["symbol"] == SYMBOL)
step = tick = 0.001
for f in sym["filters"]:
    if f["filterType"] == "LOT_SIZE":
        step = float(f["stepSize"])
    if f["filterType"] == "PRICE_FILTER":
        tick = float(f["tickSize"])
brackets = signed_request(KEY, SECRET, "/fapi/v1/leverageBracket", {"symbol": SYMBOL})[0]["brackets"]


def feasible(L):
    """杠杆 L 可行：存在分档 initialLeverage ≥ L 且 保证金×L 不超过该档名义上限。"""
    for b in brackets:
        if float(b.get("initialLeverage", 0)) >= L:
            cap = float(b.get("notionalCap", 0) or 0)
            if cap == 0 or margin * L <= cap:
                return True, b
    return False, None


lev, bracket = None, None
for L in range(LEV_WANT, 0, -1):
    ok, b = feasible(L)
    if ok:
        lev, bracket = L, b
        break

# 3. 设置杠杆
try:
    signed_request(KEY, SECRET, "/fapi/v1/leverage",
                   {"symbol": SYMBOL, "leverage": lev}, method="POST")
    lev_note = f"{lev}x 已设置"
except Exception as e:
    lev_note = f"杠杆设置失败: {str(e)[:80]}"

# 4. 数量（名义价值 = 保证金 × 杠杆，超出分档上限则收缩）
price = float(signed_request(KEY, SECRET, "/fapi/v2/ticker/price", {"symbol": SYMBOL})["price"])
qty = math.floor((margin * lev / price) / step) * step
cap = None
for b in brackets:
    if float(b.get("initialLeverage", 0)) >= lev:
        cap = float(b.get("notionalCap", 0) or 0)
        break
if cap and qty * price > cap:
    qty = math.floor(cap / price / step) * step
qty = round(qty, 10)

# 5. 市价开多
order = signed_request(KEY, SECRET, "/fapi/v1/order",
                       {"symbol": SYMBOL, "side": "BUY", "type": "MARKET", "quantity": qty,
                        "newOrderRespType": "RESULT"}, method="POST")

# 6. 强平价与止损单（触发即全平该仓）
pr = signed_request(KEY, SECRET, "/fapi/v2/positionRisk", {"symbol": SYMBOL})
pos = next(p for p in pr if p.get("symbol") == SYMBOL)
liq = float(pos.get("liquidationPrice") or 0)
entry = float(pos.get("entryPrice") or price)
sl = round(liq * SL_ABOVE_LIQ / tick) * tick

sl_order = signed_request(KEY, SECRET, "/fapi/v1/order",
                          {"symbol": SYMBOL, "side": "SELL", "type": "STOP_MARKET",
                           "stopPrice": sl, "closePosition": "true",
                           "workingType": "MARK_PRICE", "newOrderRespType": "RESULT"},
                          method="POST")

# 7. 注册引擎放行（量化引擎不干预此仓的止盈止损）
mp = "quant-manual.json"
cur = []
if os.path.exists(mp):
    cur = json.load(open(mp)) or []
entry_key = f"{ACCT_NAME}|btcusdt"
if entry_key not in cur:
    cur.append(entry_key)
json.dump(cur, open(mp, "w"))

print(json.dumps({
    "account": ACCT_NAME,
    "requestedLeverage": LEV_WANT,
    "effectiveLeverage": lev,
    "leverageNote": f"交易所分档上限：该保证金规模下最高 {lev}x（150x 超出 BTCUSDT 分档限制）",
    "available": avail,
    "margin": margin,
    "qty": qty,
    "notional": round(qty * price, 2),
    "entry": entry,
    "liquidationPrice": liq,
    "stopLoss": sl,
    "slDistanceFromEntryPct": round((entry - sl) / entry * 100, 3),
    "orderId": order.get("orderId"),
    "slOrderId": sl_order.get("orderId"),
}, ensure_ascii=False, indent=1))
