"""每币种「现在该干嘛」的一句话建议，给 K 线图右上角的胶囊用。

只做展示层的聚合：方向来自 live_evaluations 的评分，止损止盈复用
adaptive_sl_tp 的同一套 ATR 参数，杠杆取交易所真实分档上限，持仓时长
按引擎的 ATR 止损距离与 MAX_HOLD_MIN 推算。不产生任何下单决策。
"""
from __future__ import annotations

# 与 quant.py 的下单参数保持同一口径（改那边时要一起改）
ATR_SL_MULT = 1.5
ATR_TP_MULT = 2.5
SL_MIN_PCT = 0.004
SL_MAX_PCT = 0.020
TP_MIN_PCT = 0.0018
TP_MAX_PCT = 0.030
MAX_HOLD_MIN = 60
# 止损被打掉时，允许亏掉保证金的这个比例（用于反推建议杠杆）
MARGIN_RISK_AT_STOP = 0.12
# 展示建议要求的最低盈亏比：（止盈距离 / 止损距离）
MIN_RR = 1.6


def _num(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and abs(out) != float("inf") else None


def _round_price(value: float) -> float:
    """价格保留有效位数：BTC 6 位太长，小币 2 位又丢精度。"""
    a = abs(value)
    if a >= 1000:
        return round(value, 1)
    if a >= 1:
        return round(value, 3)
    if a >= 0.01:
        return round(value, 5)
    return round(value, 8)


def _stop_plan(price: float, atr: float | None, bullish: bool) -> dict:
    """止损止盈价：ATR 倍数，但保证止盈距离不小于止损的 MIN_RR 倍。

    引擎的 adaptive_sl_tp 上下限是各自独立的（止损 0.4%、止盈 0.18%），
    低波动时两者都被夹到下限，算出来 rr=0.45 —— 那样的仓位不该开。
    展示建议时按 MIN_RR 反推止盈距离，让"该不该做"由评分决定，
    而不是被一个盈亏比先天不成立的区间否决。
    """
    if not price or price <= 0:
        return {}
    if atr and atr > 0:
        sl_pct = min(SL_MAX_PCT, max(SL_MIN_PCT, atr * ATR_SL_MULT / price))
        tp_pct = min(TP_MAX_PCT, max(TP_MIN_PCT, atr * ATR_TP_MULT / price))
    else:
        sl_pct, tp_pct = SL_MIN_PCT, TP_MIN_PCT
    tp_pct = max(tp_pct, sl_pct * MIN_RR)
    if bullish:
        sl = price * (1 - sl_pct)
        tp = price * (1 + tp_pct)
    else:
        sl = price * (1 + sl_pct)
        tp = price * (1 - tp_pct)
    return {
        "sl": _round_price(sl),
        "tp": _round_price(tp),
        "slPct": round(sl_pct * 100, 2),
        "tpPct": round(tp_pct * 100, 2),
        "rr": round(tp_pct / sl_pct, 2) if sl_pct else None,
    }


def _suggest_leverage(max_lev: int | None, score: float, sl_pct: float,
                      atr_pct: float | None) -> int | None:
    """建议杠杆：让「止损被打掉时亏掉保证金的多少」保持固定档位。

    单看评分给倍数没有意义——同样 20x，止损 0.4% 只亏 8% 保证金，
    止损 2% 就亏 40%。所以先定亏损比例，再由止损距离反推杠杆：
    止损越宽杠杆越低，单笔风险敞口始终一致。评分与波动再各打一档。
    最后夹到交易所该合约的真实上限（BTCUSDT 150x / TSLAUSDT 20x）。
    """
    if not max_lev or max_lev <= 0 or sl_pct <= 0:
        return None
    if score >= 80:
        score_factor = 1.0
    elif score >= 70:
        score_factor = 0.85
    elif score >= 60:
        score_factor = 0.7
    elif score >= 52:
        score_factor = 0.55
    else:
        return None
    vol_factor = 1.0
    if atr_pct is not None:
        if atr_pct >= 85:
            vol_factor = 0.55
        elif atr_pct >= 70:
            vol_factor = 0.75
    # 12% 保证金亏损上限 ÷ 止损距离，例如 0.5% → 24x
    lev = (MARGIN_RISK_AT_STOP / (sl_pct / 100.0)) * score_factor * vol_factor
    lev = max(1, min(max_lev, int(round(lev))))
    # 落到常用整档，避免出现 23x 这种没有意义的数
    for step in (10, 5, 2):
        if lev >= step * 2:
            lev = int(round(lev / step)) * step
            break
    return max(1, min(max_lev, lev))


def _entry_hint(price: float, bullish: bool, atr: float | None,
                plan: dict) -> dict:
    """入场时机：现在就能进，还是等回踩/反弹。

    为什么需要它：只给方向不给时机，用户看到「做多」就市价追进去，
    而价格往往正好在短期高点——止损一点点就够不着，持仓时间也白搭。
    这里用 ATR 的一半作为"理想回撤幅度"：当前价距离理想入场区
    超过阈值就说"等回踩"，否则说"可以进"。

    返回 {timing, entryZone:[low, high], note}：
      timing = "now"（可以进）| "wait"（等更好价位）
    """
    if not price or price <= 0:
        return {}
    # 理想入场区：多头在现价下方 half-ATR，空头在上方
    offset = (atr * 0.5) if (atr and atr > 0) else price * 0.002
    if bullish:
        zone = [price - offset, price - offset * 0.2]
    else:
        zone = [price + offset * 0.2, price + offset]
    lo, hi = sorted(zone)
    # 距离理想区中点还有多远（百分比）
    mid = (lo + hi) / 2
    drift = abs(price - mid) / price * 100
    timing = "wait" if drift > 0.15 else "now"
    if timing == "now":
        note = "现价可入"
    else:
        note = "等回落到区间" if bullish else "等反弹到区间"
    return {
        "timing": timing,
        "entryZone": [_round_price(lo), _round_price(hi)],
        "entryNote": note,
        "entryDriftPct": round(drift, 2),
    }


def _hold_minutes(score: float, bullish: bool, regime: str | None,
                  atr_pct: float | None) -> int:
    """建议持有时长：引擎最长 60 分钟强平，趋势/强信号给更长的目标。"""
    hold = 20.0
    if score >= 80:
        hold = 45
    elif score >= 70:
        hold = 35
    elif score >= 60:
        hold = 28
    if regime == "trend":
        hold *= 1.25
    elif regime == "range":
        hold *= 0.7
    if atr_pct is not None and atr_pct >= 80:
        hold *= 0.8
    return int(max(8, min(MAX_HOLD_MIN, round(hold))))


def build_advice(symbol: str, state: dict, evaluation: dict,
                 max_leverage: int | None) -> dict:
    """返回一个币的实时交易方向建议；数据不足时给出明确的观望原因。

    只回答一个问题：现在该做多、做空，还是不动、以及为什么。
    持仓状态不属于这里（账户卡和仓位历史已经在展示）。
    """
    advice = {"symbol": symbol, "maxLeverage": max_leverage}
    price = _num(state.get("price"))
    atr = _num(state.get("atr"))
    atr_pct = _num(state.get("atrPct"))
    if not price or not evaluation:
        return {**advice, "action": "wait", "reason": "等待指标"}

    score = _num(evaluation.get("score"))
    opposite = _num(evaluation.get("oppositeScore"))
    gap = _num(evaluation.get("gap"))
    direction = evaluation.get("direction")
    grade = evaluation.get("grade")
    vetoes = evaluation.get("vetoes") or []

    # 中性/看空方向、或评分不够：明确说不动，别让用户自己猜
    if direction not in ("bullish", "bearish"):
        return {**advice, "action": "wait", "reason": "方向不明"}
    if grade in ("none", None):
        return {**advice, "action": "wait", "reason": "信号弱", "score": score}
    if vetoes:
        return {**advice, "action": "wait", "reason": str(vetoes[0])[:40], "score": score}
    if score is None or gap is None or score < 52 or gap < 6:
        return {**advice, "action": "wait", "reason": "优势不足", "score": score}

    bullish = direction == "bullish"
    plan = _stop_plan(price, atr, bullish)
    if not plan:
        return {**advice, "action": "wait", "reason": "等待价格"}
    lev = _suggest_leverage(max_leverage, score, plan["slPct"], atr_pct)
    return {
        **advice,
        "action": "long" if bullish else "short",
        "side": "long" if bullish else "short",
        "price": _round_price(price),
        "score": round(score, 1),
        "gap": round(gap, 1),
        "grade": grade,
        "confidence": _num(evaluation.get("confidence")),
        "leverage": lev,
        "sl": plan["sl"], "tp": plan["tp"],
        "slPct": plan["slPct"], "tpPct": plan["tpPct"],
        "rr": plan["rr"],
        "holdMin": _hold_minutes(score, bullish, state.get("regime"), atr_pct),
        # 入场时机：现在能不能进、理想区间在哪
        **_entry_hint(price, bullish, atr, plan),
        # 方向锁定状态：上一轮是什么方向、这轮有没有被"绑住"
        "directionLatched": bool(evaluation.get("latched")),
    }
