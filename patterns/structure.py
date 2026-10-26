"""结构形态：基于摆动点（swing pivot）识别经典反转/持续形态。

与单根/双根/三根K线形态不同，这类形态跨越十几到几十根K线，命中时需要把
几何点位（肩/头/颈线/趋势线）一起给出，前端才能在K线上画出轮廓。

检测器统一返回 dict（供 analysis 模块复用于画图），Pattern 子类只做布尔包装：
  {key, name_zh, direction, status, points:[[t_ms, price],...],
   neckline:[[t_ms, price],[t_ms, price]] | None, bars:int, detail:dict}
"""
from __future__ import annotations

from .base import BEARISH, BULLISH, NEUTRAL, Pattern, register, trend_of

# 摆动点判定左右各需要的K线数（越大越迟钝、越可靠）
PIVOT_LEFT = 2
PIVOT_RIGHT = 2


def find_pivots(cs, left: int = PIVOT_LEFT, right: int = PIVOT_RIGHT) -> list[tuple[int, str]]:
    """分形摆动点：i 在 [i-left, i+right] 内是最极端的高/低。

    返回 [(index, "H"|"L")]，按时间升序；同一根既高又低（内包线）时保留振幅更大的一侧。
    """
    out: list[tuple[int, str]] = []
    n = len(cs)
    for i in range(left, n - right):
        h, l = cs[i].high, cs[i].low
        win = range(i - left, i + right + 1)
        is_h = all(h >= cs[j].high for j in win) and any(h > cs[j].high for j in win if j != i)
        is_l = all(l <= cs[j].low for j in win) and any(l < cs[j].low for j in win if j != i)
        if is_h and is_l:
            # 内外包叠：取相对邻域更显著的方向
            up = h - max(cs[j].high for j in win if j != i)
            dn = min(cs[j].low for j in win if j != i) - l
            out.append((i, "H" if up >= dn else "L"))
        elif is_h:
            out.append((i, "H"))
        elif is_l:
            out.append((i, "L"))
    return out


def alternations(piv: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """（保留给外部调用）同类相邻压缩需价格，实际用 pivots_of。"""
    return piv


def pivots_of(cs, left: int = PIVOT_LEFT, right: int = PIVOT_RIGHT) -> list[tuple[int, str]]:
    """摆动点 + 同类相邻压缩（需要价格因而在此实现）。"""
    raw = find_pivots(cs, left, right)
    out: list[tuple[int, str]] = []
    for i, kind in raw:
        if out and out[-1][1] == kind:
            pi = out[-1][0]
            if kind == "H":
                if cs[i].high >= cs[pi].high:
                    out[-1] = (i, kind)
            else:
                if cs[i].low <= cs[pi].low:
                    out[-1] = (i, kind)
        else:
            out.append((i, kind))
    return out


def _seq(piv: list[tuple[int, str]], types: str, max_trail: int = 5):
    """在摆动点序列尾部找 types 形状（允许末尾多出 max_trail 个摆动点）。"""
    k = len(types)
    if len(piv) < k:
        return None
    for trail in range(0, max_trail + 1):
        seg = piv[:len(piv) - trail][-k:]
        if len(seg) == k and "".join(t for _, t in seg) == types:
            return seg
    return None


def _pts(cs, idxs) -> list[list[float]]:
    return [[cs[i].open_time, round(cs[i].high if kind == "H" else cs[i].low, 8)]
            for i, kind in idxs]


def _neckline(cs, a: int, b: int) -> list[list[float]]:
    return [[cs[a].open_time, round(cs[a].high, 8)], [cs[b].open_time, round(cs[b].high, 8)]]


def _line_price(t1: int, p1: float, t2: int, p2: float, t: int) -> float:
    if t2 == t1:
        return p2
    return p1 + (p2 - p1) * (t - t1) / (t2 - t1)


BREAK_WINDOW = 8     # 突破必须发生在最近这几根内（限制重复报警；旧形态只画不再提示）


def _recent_break(cs, level: float, up: bool, window: int = BREAK_WINDOW) -> bool:
    """收盘已站在 level 之外，且穿越发生在最近 window 根内。"""
    cur = cs[-1].close
    if up and cur <= level:
        return False
    if not up and cur >= level:
        return False
    for j in range(max(0, len(cs) - 1 - window), len(cs)):
        c = cs[j].close
        if (up and c <= level) or (not up and c >= level):
            return True
    return False


def _prior_trend_ok(cs, start_i: int, want: str) -> bool:
    """形态之前要有反向趋势；历史太短（序列开头）时不做要求。"""
    if start_i < 6:
        return True
    return trend_of(cs[:start_i - 1]) == want


# ---------------- 反转形态 ----------------

def detect_head_shoulders(cs, bottom: bool, require_break: bool = True):
    """头肩底（bottom=True）/ 头肩顶。"""
    piv = pivots_of(cs)
    seq = _seq(piv, "LHLHL" if bottom else "HLHLH")
    if not seq:
        return None
    i_l, i_h1, i_head, i_h2, i_r = (i for i, _ in seq)
    if len(cs) - i_l > 80:
        return None
    if bottom:
        lvals = [cs[i_l].low, cs[i_head].low, cs[i_r].low]
        neck_vals = [cs[i_h1].high, cs[i_h2].high]
        head_ext = min(lvals)
        shoulders_equal = abs(lvals[0] - lvals[2])
        top = max(neck_vals)
        height = top - head_ext
        if height <= 0:
            return None
        # 头必须明显低于双肩；双肩高度接近；两颈点接近
        if not (lvals[1] < lvals[0] - 0.12 * height and lvals[1] < lvals[2] - 0.12 * height):
            return None
        if shoulders_equal > 0.45 * height or abs(neck_vals[0] - neck_vals[1]) > 0.45 * height:
            return None
        neck = max(neck_vals)
        broke = _recent_break(cs, neck, True)
        prior = "down"
    else:
        hvals = [cs[i_l].high, cs[i_head].high, cs[i_r].high]
        neck_vals = [cs[i_h1].low, cs[i_h2].low]
        head_ext = max(hvals)
        shoulders_equal = abs(hvals[0] - hvals[2])
        bottom_p = min(neck_vals)
        height = head_ext - bottom_p
        if height <= 0:
            return None
        if not (hvals[1] > hvals[0] + 0.12 * height and hvals[1] > hvals[2] + 0.12 * height):
            return None
        if shoulders_equal > 0.45 * height or abs(neck_vals[0] - neck_vals[1]) > 0.45 * height:
            return None
        neck = min(neck_vals)
        broke = _recent_break(cs, neck, False)
        prior = "up"
    if not _prior_trend_ok(cs, i_l, prior):
        return None
    if require_break and not broke:
        return None
    return {
        "key": "head_shoulders_bottom" if bottom else "head_shoulders_top",
        "name_zh": "头肩底" if bottom else "头肩顶",
        "direction": BULLISH if bottom else BEARISH,
        "status": "confirmed" if broke else "forming",
        "priority": 70,
        "points": _pts(cs, seq),
        "neckline": _neckline(cs, i_h1, i_h2),
        "neck": round(neck, 8),
        "bars": len(cs) - i_l,
        "target": round(neck + (neck - head_ext) if bottom else neck - (head_ext - neck), 8),
        "detail": {"head": head_ext, "shoulder_left": lvals[0] if bottom else hvals[0],
                   "shoulder_right": lvals[2] if bottom else hvals[2]},
    }


def detect_triple(cs, bottom: bool, require_break: bool = True):
    """三重底 / 三重顶。三个同向极值接近，颈线被突破即成立。"""
    piv = pivots_of(cs)
    seq = _seq(piv, "LHLHL" if bottom else "HLHLH")
    if not seq:
        return None
    i_a, i_h1, i_b, i_h2, i_c = (i for i, _ in seq)
    if len(cs) - i_a > 80:
        return None
    if bottom:
        vals = [cs[i_a].low, cs[i_b].low, cs[i_c].low]
        neck_vals = [cs[i_h1].high, cs[i_h2].high]
        height = max(neck_vals) - min(vals)
        if height <= 0:
            return None
        if max(vals) - min(vals) > 0.18 * height:      # 三底必须接近
            return None
        if abs(neck_vals[0] - neck_vals[1]) > 0.3 * height:
            return None
        neck = max(neck_vals)
        broke = _recent_break(cs, neck, True)
        prior = "down"
    else:
        vals = [cs[i_a].high, cs[i_b].high, cs[i_c].high]
        neck_vals = [cs[i_h1].low, cs[i_h2].low]
        height = max(vals) - min(neck_vals)
        if height <= 0:
            return None
        if max(vals) - min(vals) > 0.18 * height:
            return None
        if abs(neck_vals[0] - neck_vals[1]) > 0.3 * height:
            return None
        neck = min(neck_vals)
        broke = _recent_break(cs, neck, False)
        prior = "up"
    if require_break and not broke:
        return None
    if not _prior_trend_ok(cs, i_a, prior):
        return None
    return {
        "key": "triple_bottom" if bottom else "triple_top",
        "name_zh": "三重底" if bottom else "三重顶",
        "direction": BULLISH if bottom else BEARISH,
        "status": "confirmed" if broke else "forming",
        "priority": 66,
        "points": _pts(cs, seq),
        "neckline": _neckline(cs, i_h1, i_h2),
        "neck": round(neck, 8),
        "bars": len(cs) - i_a,
        "target": round(neck + (neck - min(vals)) if bottom else neck - (max(vals) - neck), 8),
        "detail": {"lows": [round(v, 8) for v in vals]},
    }


def detect_double(cs, bottom: bool, require_break: bool = True):
    """双底 / 双顶。"""
    piv = pivots_of(cs)
    seq = _seq(piv, "LHL" if bottom else "HLH")
    if not seq:
        return None
    i_a, i_mid, i_b = (i for i, _ in seq)
    if len(cs) - i_a > 60:
        return None
    if bottom:
        v1, v2 = cs[i_a].low, cs[i_b].low
        neck = cs[i_mid].high
        height = neck - min(v1, v2)
        if height <= 0 or abs(v1 - v2) > 0.25 * height:
            return None
        broke = _recent_break(cs, neck, True)
        prior = "down"
    else:
        v1, v2 = cs[i_a].high, cs[i_b].high
        neck = cs[i_mid].low
        height = max(v1, v2) - neck
        if height <= 0 or abs(v1 - v2) > 0.25 * height:
            return None
        broke = _recent_break(cs, neck, False)
        prior = "up"
    if require_break and not broke:
        return None
    if not _prior_trend_ok(cs, i_a, prior):
        return None
    neck_pts = [[cs[i_mid].open_time, round(neck, 8)],
                [cs[-1].open_time, round(neck, 8)]]
    return {
        "key": "double_bottom" if bottom else "double_top",
        "name_zh": "双底" if bottom else "双顶",
        "direction": BULLISH if bottom else BEARISH,
        "status": "confirmed" if broke else "forming",
        "priority": 62,
        "points": _pts(cs, seq),
        "neckline": neck_pts,
        "neck": round(neck, 8),
        "bars": len(cs) - i_a,
        "target": round(neck + height if bottom else neck - height, 8),
        "detail": {"v1": round(v1, 8), "v2": round(v2, 8)},
    }


# ---------------- 持续/中继形态 ----------------

def _fit(points: list[tuple[int, float]]):
    """最小二乘直线：返回 (slope_per_bar, intercept) 供趋势线/三角判定。"""
    n = len(points)
    if n < 2:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    mx = sum(xs) / n
    my = sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    k = sum((x - mx) * (y - my) for x, y in points) / den
    return k, my - k * mx


def detect_triangle(cs, require_break: bool = True):
    """上升/下降/对称三角形：高点连线与低点连线收敛。"""
    piv = pivots_of(cs)
    highs = [(i, cs[i].high) for i, k in piv if k == "H"][-3:]
    lows = [(i, cs[i].low) for i, k in piv if k == "L"][-3:]
    if len(highs) < 2 or len(lows) < 2:
        return None
    if len(cs) - min(highs[0][0], lows[0][0]) > 90:
        return None
    fh = _fit(highs)
    fl = _fit(lows)
    if not fh or not fl:
        return None
    kh, bh = fh
    kl, bl = fl
    price = cs[-1].close
    if price <= 0:
        return None
    # 归一化斜率：每根K线相对价格的变化率
    sh = kh / price
    sl = kl / price
    cur = len(cs) - 1
    hi_last = kh * cur + bh
    lo_last = kl * cur + bl
    if hi_last <= lo_last:
        return None
    # 收敛：两线间距随时间为负斜率
    if sh >= sl:
        return None
    kind = "symmetrical_triangle"
    name = "对称三角形"
    direction = NEUTRAL
    if abs(sh) < 0.00008 and sl > 0.00008:
        kind, name, direction = "ascending_triangle", "上升三角形", BULLISH
    elif abs(sl) < 0.00008 and sh < -0.00008:
        kind, name, direction = "descending_triangle", "下降三角形", BEARISH
    # 突破判定
    up_break = cs[-1].close > hi_last
    dn_break = cs[-1].close < lo_last
    if require_break and not (up_break or dn_break):
        return None
    status = "confirmed" if (up_break or dn_break) else "forming"
    if up_break:
        direction = BULLISH
    elif dn_break:
        direction = BEARISH
    t0 = min(highs[0][0], lows[0][0])
    t_end = len(cs) - 1
    return {
        "key": kind, "name_zh": name, "direction": direction, "status": status,
        "priority": 56,
        "points": _pts(cs, [(i, "H") for i, _ in highs] + [(i, "L") for i, _ in lows]),
        "neckline": [[cs[t0].open_time, round(kh * t0 + bh, 8)],
                     [cs[t_end].open_time, round(kh * t_end + bh, 8)]],
        "trendline": [[cs[t0].open_time, round(kl * t0 + bl, 8)],
                      [cs[t_end].open_time, round(kl * t_end + bl, 8)]],
        "bars": t_end - t0,
        "neck": round(hi_last, 8),
        "target": round(hi_last + (hi_last - lo_last) if up_break else lo_last - (hi_last - lo_last), 8),
        "detail": {"slope_high": round(sh, 8), "slope_low": round(sl, 8)},
    }


def detect_wedge(cs, require_break: bool = True):
    """楔形：高、低点同向倾斜且收敛。上升楔形偏空，下降楔形偏多。"""
    piv = pivots_of(cs)
    highs = [(i, cs[i].high) for i, k in piv if k == "H"][-3:]
    lows = [(i, cs[i].low) for i, k in piv if k == "L"][-3:]
    if len(highs) < 2 or len(lows) < 2:
        return None
    if len(cs) - min(highs[0][0], lows[0][0]) > 90:
        return None
    fh, fl = _fit(highs), _fit(lows)
    if not fh or not fl:
        return None
    kh, bh = fh
    kl, bl = fl
    price = cs[-1].close or 1
    sh, sl = kh / price, kl / price
    if not (sh > 0.00005 and sl > 0.00005) and not (sh < -0.00005 and sl < -0.00005):
        return None
    cur = len(cs) - 1
    hi_last = kh * cur + bh
    lo_last = kl * cur + bl
    if hi_last <= lo_last:
        return None
    # 收敛（间距缩小）
    if not (sh < sl if sh > 0 else abs(sh) > abs(sl)):
        return None
    rising = sh > 0
    kind = "rising_wedge" if rising else "falling_wedge"
    name = "上升楔形" if rising else "下降楔形"
    # 上升楔形向下破位看跌；下降楔形向上破位看涨
    broke = cs[-1].close < lo_last if rising else cs[-1].close > hi_last
    if require_break and not broke:
        return None
    t0 = min(highs[0][0], lows[0][0])
    t_end = len(cs) - 1
    return {
        "key": kind, "name_zh": name,
        "direction": BEARISH if rising else BULLISH,
        "status": "confirmed" if broke else "forming",
        "priority": 58,
        "points": _pts(cs, [(i, "H") for i, _ in highs] + [(i, "L") for i, _ in lows]),
        "neckline": [[cs[t0].open_time, round(kh * t0 + bh, 8)],
                     [cs[t_end].open_time, round(kh * t_end + bh, 8)]],
        "trendline": [[cs[t0].open_time, round(kl * t0 + bl, 8)],
                      [cs[t_end].open_time, round(kl * t_end + bl, 8)]],
        "bars": t_end - t0,
        "neck": round(lo_last if rising else hi_last, 8),
        "target": round((lo_last - (hi_last - lo_last)) if rising else (hi_last + (hi_last - lo_last)), 8),
        "detail": {"slope_high": round(sh, 8), "slope_low": round(sl, 8)},
    }


DETECTORS = [
    ("head_shoulders_bottom", lambda cs, rb: detect_head_shoulders(cs, True, rb)),
    ("head_shoulders_top", lambda cs, rb: detect_head_shoulders(cs, False, rb)),
    ("triple_bottom", lambda cs, rb: detect_triple(cs, True, rb)),
    ("triple_top", lambda cs, rb: detect_triple(cs, False, rb)),
    ("double_bottom", lambda cs, rb: detect_double(cs, True, rb)),
    ("double_top", lambda cs, rb: detect_double(cs, False, rb)),
    ("ascending_triangle", lambda cs, rb: detect_triangle(cs, rb)),
    ("rising_wedge", lambda cs, rb: detect_wedge(cs, rb)),
]


def detect_best(cs, require_break: bool = True):
    """跑全部结构检测器，返回几何最完整且最近的一个（画图用）。"""
    best = None
    for _, fn in DETECTORS:
        try:
            r = fn(cs, require_break)
        except Exception:
            r = None
        if not r:
            continue
        # 优先：确认态 > 构筑中；其次：形态优先级（反转形态更具体）；再看跨度
        score = (1 if r["status"] == "confirmed" else 0, r.get("priority", 0), r.get("bars", 0))
        if best is None or score > best[0]:
            best = (score, r)
    return best[1] if best else None


# ---------------- Pattern 包装（注册进形态引擎，走通知/统计） ----------------

def _mk(key, name_zh, direction, fn, expect: set[str], priority=60):
    """把检测器包装成已注册的 Pattern。

    注意：Pattern 实例是进程级单例（跨币种共享），所以不能把检测结果缓存在
    self 上；geometry() 干脆重算一次（每次收线一次，成本可忽略）。
    expect：检测器可能返回多个 key（如三角检测器），只有落在这里的才认。
    """

    class _Structural(Pattern):
        pass

    _Structural.key = key
    _Structural.name_zh = name_zh
    _Structural.direction = direction
    _Structural.priority = priority
    _Structural.min_bars = 12
    _Structural.live = False        # 结构形态只在收线确认后判定

    def check(self, cs):
        r = fn(cs, True)
        if not (r and r.get("status") == "confirmed" and r["key"] in expect):
            return False
        # 只在"刚突破的那一根"报警：突破发生在更早的K线说明已经报过，
        # 否则同一形态会在突破后的窗口内每根K线重复触发。
        neck = r.get("neck")
        if neck is None or len(cs) < 2:
            return True
        prev = cs[-2].close
        up = r.get("direction") == BULLISH
        return (prev <= neck) if up else (prev >= neck)

    def geometry(self, cs):
        r = fn(cs, False)
        if r and r["key"] in expect:
            return r
        return None

    _Structural.check = check
    _Structural.geometry = geometry
    return register(type(f"S_{key}", (_Structural,), {}))


HeadShouldersBottom = _mk("head_shoulders_bottom", "头肩底", BULLISH,
                          lambda cs, rb: detect_head_shoulders(cs, True, rb),
                          {"head_shoulders_bottom"}, priority=70)
HeadShouldersTop = _mk("head_shoulders_top", "头肩顶", BEARISH,
                       lambda cs, rb: detect_head_shoulders(cs, False, rb),
                       {"head_shoulders_top"}, priority=70)
TripleBottom = _mk("triple_bottom", "三重底", BULLISH,
                   lambda cs, rb: detect_triple(cs, True, rb),
                   {"triple_bottom"}, priority=66)
TripleTop = _mk("triple_top", "三重顶", BEARISH,
                lambda cs, rb: detect_triple(cs, False, rb),
                {"triple_top"}, priority=66)
DoubleBottom = _mk("double_bottom", "双底", BULLISH,
                   lambda cs, rb: detect_double(cs, True, rb),
                   {"double_bottom"}, priority=62)
DoubleTop = _mk("double_top", "双顶", BEARISH,
                lambda cs, rb: detect_double(cs, False, rb),
                {"double_top"}, priority=62)
AscendingTriangle = _mk("ascending_triangle", "上升三角形", BULLISH,
                        lambda cs, rb: detect_triangle(cs, rb),
                        {"ascending_triangle"}, priority=58)
DescendingTriangle = _mk("descending_triangle", "下降三角形", BEARISH,
                         lambda cs, rb: detect_triangle(cs, rb),
                         {"descending_triangle"}, priority=58)
SymmetricalTriangle = _mk("symmetrical_triangle", "对称三角形", NEUTRAL,
                          lambda cs, rb: detect_triangle(cs, rb),
                          {"symmetrical_triangle"}, priority=56)
RisingWedge = _mk("rising_wedge", "上升楔形", BEARISH,
                  lambda cs, rb: detect_wedge(cs, rb),
                  {"rising_wedge"}, priority=58)
FallingWedge = _mk("falling_wedge", "下降楔形", BULLISH,
                   lambda cs, rb: detect_wedge(cs, rb),
                   {"falling_wedge"}, priority=58)
