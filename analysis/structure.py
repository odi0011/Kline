"""K线结构分析：支撑压力位、趋势线、FVG（公允价值缺口）、订单块（OB），
以及当前正在构筑/刚完成的结构形态几何。

输出直接给前端画布叠加层用，全部坐标是 [[open_time_ms, price], ...]。
"""
from __future__ import annotations

from patterns.structure import DETECTORS, detect_best, pivots_of

LOOKBACK = 160          # 分析窗口（根）
MIN_TOUCHES = 2         # 支撑压力最少触碰次数
ZONE_BARS = 60          # FVG / 订单块只看最近这么多根


def _atr(cs, n: int = 14) -> float:
    if len(cs) < 2:
        return 0.0
    trs = []
    for i in range(max(1, len(cs) - n), len(cs)):
        c, p = cs[i], cs[i - 1]
        trs.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
    return sum(trs) / len(trs) if trs else 0.0


def _levels(cs, atr: float) -> list[dict]:
    """摆动点价格聚类 → 支撑/压力位。两个以上摆动点落在同一窄带即为有效位。"""
    piv = pivots_of(cs)
    if not piv:
        return []
    last = cs[-1].close
    tol = max(last * 0.0012, atr * 0.3)
    pts = [(i, cs[i].high if k == "H" else cs[i].low, k) for i, k in piv]
    pts = [p for p in pts if len(cs) - p[0] <= LOOKBACK]
    clusters: list[dict] = []
    for i, price, kind in pts:
        hit = None
        for c in clusters:
            if abs(c["price"] - price) <= tol:
                hit = c
                break
        if hit:
            hit["prices"].append(price)
            hit["touches"] += 1
            hit["last_i"] = max(hit["last_i"], i)
            hit["kinds"].add(kind)
        else:
            clusters.append({"price": price, "prices": [price], "touches": 1,
                             "last_i": i, "kinds": {kind}})
    out = []
    for c in clusters:
        if c["touches"] < MIN_TOUCHES:
            continue
        price = sum(c["prices"]) / len(c["prices"])
        is_res = price > last
        # 同簇里只有同一侧的摆动点才算数（高点簇=压力，低点簇=支撑）
        if is_res and "H" not in c["kinds"]:
            continue
        if (not is_res) and "L" not in c["kinds"]:
            continue
        out.append({
            "price": round(price, 8),
            "kind": "resistance" if is_res else "support",
            "touches": c["touches"],
            "last_time": cs[c["last_i"]].open_time,
            "strength": round(min(1.0, c["touches"] / 4) * 0.7 + min(1.0, (c["last_i"] / max(1, len(cs)))) * 0.3, 3),
        })
    out.sort(key=lambda x: abs(x["price"] - last))
    if not out:
        # 兜底：价格走势太顺滑（没有重复触碰）时，取最近的显著摆动点，
        # 否则画面上一条支撑压力线都没有，形态缺少参照。
        near = sorted(pts, key=lambda p: abs(p[1] - last))[:2]
        for i, price, kind in near:
            out.append({
                "price": round(price, 8),
                "kind": "resistance" if price > last else "support",
                "touches": 1,
                "last_time": cs[i].open_time,
                "strength": 0.3,
            })
        out.sort(key=lambda x: abs(x["price"] - last))
    return out[:5]


def _fit_line(points: list[tuple[int, float]]):
    n = len(points)
    if n < 2:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    k = sum((x - mx) * (y - my) for x, y in points) / den
    return k, my - k * mx


def _trendlines(cs, atr: float) -> list[dict]:
    """最近两个同向摆动点连线；要求中间K线未明显穿越（有效性校验）。"""
    piv = pivots_of(cs)
    highs = [(i, cs[i].high) for i, k in piv if k == "H"][-2:]
    lows = [(i, cs[i].low) for i, k in piv if k == "L"][-2:]
    out = []
    for pts, kind in ((lows, "support"), (highs, "resistance")):
        if len(pts) < 2:
            continue
        (i1, p1), (i2, p2) = pts
        if i2 - i1 < 3 or len(cs) - i1 > LOOKBACK:
            continue
        fit = _fit_line(pts)
        if not fit:
            continue
        k, b = fit
        if abs(k) * (len(cs) - 1) < atr * 0.15:      # 近乎水平 → 交给支撑压力位，不画斜线
            continue
        # 有效性：两摆动点之间不能有实体明显穿越
        broken = False
        for j in range(i1 + 1, i2):
            line = k * j + b
            if kind == "support" and cs[j].close < line - atr * 0.25:
                broken = True
                break
            if kind == "resistance" and cs[j].close > line + atr * 0.25:
                broken = True
                break
        if broken:
            continue
        out.append({
            "p1": [cs[i1].open_time, round(p1, 8)],
            "p2": [cs[i2].open_time, round(p2, 8)],
            "kind": kind,
            "touches": 2,
        })
    return out


def _zones(cs, atr: float) -> list[dict]:
    """FVG 与订单块（只看最近 ZONE_BARS 根）。"""
    out: list[dict] = []
    n = len(cs)
    start = max(2, n - ZONE_BARS)
    last_close = cs[-1].close

    # --- FVG：三根K线形成的缺口，未被回补 ---
    fvgs = []
    for i in range(start, n):
        a, c = cs[i - 2], cs[i]
        # 看涨 FVG：第 i 根的最低价高于 i-2 的最高价
        if c.low > a.high and c.close > c.open:
            top, bottom = c.low, a.high
            filled = any(cs[j].low <= bottom for j in range(i + 1, n))
            if not filled:
                fvgs.append({"kind": "fvg", "dir": "bull", "top": round(top, 8),
                             "bottom": round(bottom, 8), "from": a.open_time,
                             "to": cs[-1].open_time, "label": "FVG"})
        # 看跌 FVG
        if c.high < a.low and c.close < c.open:
            top, bottom = a.low, c.high
            filled = any(cs[j].high >= top for j in range(i + 1, n))
            if not filled:
                fvgs.append({"kind": "fvg", "dir": "bear", "top": round(top, 8),
                             "bottom": round(bottom, 8), "from": a.open_time,
                             "to": cs[-1].open_time, "label": "FVG"})
    out.extend(fvgs[-2:])

    # --- 订单块：强冲动前最后一根反向K线的实体 ---
    obs = []
    i = start
    while i < n - 1:
        c = cs[i]
        # 未来 1~3 根累计涨幅
        j = min(n - 1, i + 3)
        move = cs[j].close - c.close
        if atr > 0 and abs(move) > 1.6 * atr:
            if move > 0 and c.close < c.open:        # 看涨 OB：下跌K线后向上冲动
                obs.append({"kind": "ob", "dir": "bull",
                            "top": round(c.high, 8), "bottom": round(c.low, 8),
                            "from": c.open_time, "to": cs[-1].open_time, "label": "OB"})
            elif move < 0 and c.close > c.open:      # 看跌 OB：上涨K线后向下冲动
                obs.append({"kind": "ob", "dir": "bear",
                            "top": round(c.high, 8), "bottom": round(c.low, 8),
                            "from": c.open_time, "to": cs[-1].open_time, "label": "OB"})
            i += 3
        else:
            i += 1
    # 只保留价格仍与现价接近的最近订单块
    obs = [o for o in obs if abs((o["top"] + o["bottom"]) / 2 - last_close) < atr * 8]
    out.extend(obs[-2:])
    return out


def analyze(cs) -> dict:
    """对一段K线做结构分析，返回前端可直接画的结构字典。"""
    empty = {"levels": [], "trendlines": [], "zones": [], "pattern": None}
    if len(cs) < 25:
        return empty
    cs = cs[-LOOKBACK:]
    atr = _atr(cs)
    if atr <= 0:
        return empty
    pattern = None
    try:
        pattern = detect_best(cs, require_break=False)
        if pattern and pattern.get("bars", 0) < 5:
            pattern = None
    except Exception:
        pattern = None
    return {
        "levels": _levels(cs, atr),
        "trendlines": _trendlines(cs, atr),
        "zones": _zones(cs, atr),
        "pattern": pattern,
        "atr": round(atr, 8),
        "price": round(cs[-1].close, 8),
    }
