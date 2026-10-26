"""技术指标与市场状态识别（纯函数，无依赖，可单测）。

覆盖量化策略需要的全部指标：
  趋势类    SMA / EMA / ADX
  动量类    RSI / Z-score / ROC
  波动类    ATR / 布林带（含带宽与 %B）/ 实测波动率
  风险类    回撤（当前/最大）
  综合      market_state() 给出 regime（趋势/震荡）、波动档位、挤压状态

设计约定：
  - 所有函数接收 list[Candle]（或 float 列表），返回标量或小字典
  - 数据不足时返回 None 而不是抛异常（引擎里按"数据不足"处理）
  - 不使用未来数据：只依赖传入序列的最后 N 个值
"""
from __future__ import annotations

import math


# ---------------- 基础统计 ----------------

def sma(values: list[float], n: int) -> float | None:
    if not values or len(values) < n or n <= 0:
        return None
    return sum(values[-n:]) / n


def ema(values: list[float], n: int) -> float | None:
    """指数移动平均（用前 n 个的 SMA 作为种子）。"""
    if not values or len(values) < n or n <= 0:
        return None
    k = 2.0 / (n + 1)
    out = sum(values[:n]) / n
    for v in values[n:]:
        out = v * k + out * (1 - k)
    return out


def stddev(values: list[float], n: int) -> float | None:
    """样本标准差。"""
    if not values or len(values) < n or n < 2:
        return None
    w = values[-n:]
    m = sum(w) / n
    var = sum((x - m) ** 2 for x in w) / (n - 1)
    return math.sqrt(var)


def percentile_rank(values: list[float], value: float) -> float | None:
    """value 在 values 中的百分位（0~100）。用于"当前 ATR 处于历史的什么水位"。"""
    if not values:
        return None
    below = sum(1 for v in values if v <= value)
    return below / len(values) * 100


# ---------------- 波动类 ----------------

def true_ranges(cs) -> list[float]:
    """逐根真实波幅 TR = max(H-L, |H-prevC|, |L-prevC|)。"""
    out = []
    for i, c in enumerate(cs):
        if i == 0:
            out.append(c.high - c.low)
            continue
        pc = cs[i - 1].close
        out.append(max(c.high - c.low, abs(c.high - pc), abs(c.low - pc)))
    return out


def atr(cs, n: int = 14) -> float | None:
    """平均真实波幅（简单平均口径，与多数看盘软件一致）。"""
    trs = true_ranges(cs)
    if len(trs) < n:
        return None
    return sum(trs[-n:]) / n


def atr_series(cs, n: int = 14) -> list[float]:
    """滚动 ATR 序列（用于算当前 ATR 的历史分位）。"""
    trs = true_ranges(cs)
    if len(trs) < n:
        return []
    out = []
    run = sum(trs[:n])
    out.append(run / n)
    for i in range(n, len(trs)):
        run += trs[i] - trs[i - n]
        out.append(run / n)
    return out


def realized_vol(cs, n: int = 20, periods_per_year: float | None = None) -> float | None:
    """实测波动率：对数收益率的样本标准差。

    返回"每根K线的波动率"（不是年化）。乘 sqrt(periods_per_year) 可得年化。
    引擎里用它判断当前市场是不是处在异常高/低波动。
    """
    if len(cs) < n + 1:
        return None
    closes = [c.close for c in cs[-(n + 1):]]
    rets = []
    for i in range(1, len(closes)):
        if closes[i - 1] <= 0:
            return None
        rets.append(math.log(closes[i] / closes[i - 1]))
    if len(rets) < 2:
        return None
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    vol = math.sqrt(var)
    if periods_per_year:
        vol *= math.sqrt(periods_per_year)
    return vol


def bollinger(cs, n: int = 20, k: float = 2.0) -> dict | None:
    """布林带。

    mid    中轨（SMA）
    upper/lower 上下轨
    bandwidth 带宽 =(upper-lower)/mid，衡量波动相对水平（挤压行情会很低）
    pctB   价格在带内的相对位置：0=下轨 1=上轨，>1 或 <0 表示已突破带外
    """
    if len(cs) < n:
        return None
    closes = [c.close for c in cs[-n:]]
    mid = sum(closes) / n
    sd = stddev(closes, n)
    if mid <= 0 or sd is None:
        return None
    upper = mid + k * sd
    lower = mid - k * sd
    last = closes[-1]
    width = (upper - lower) / mid if mid else None
    span = upper - lower
    pct_b = (last - lower) / span if span > 1e-12 else 0.5
    return {
        "mid": mid, "upper": upper, "lower": lower,
        "bandwidth": width, "pctB": pct_b, "std": sd,
    }


def bandwidth_series(cs, n: int = 20, k: float = 2.0, lookback: int = 120) -> list[float]:
    """滚动带宽序列（取最近 lookback 根内的每根带宽），用于算带宽分位。"""
    out = []
    start = max(n, len(cs) - lookback)
    for i in range(start, len(cs) + 1):
        seg = cs[:i]
        b = bollinger(seg, n, k)
        if b and b["bandwidth"] is not None:
            out.append(b["bandwidth"])
    return out


# ---------------- 动量类 ----------------

def rsi(cs, n: int = 14) -> float | None:
    """RSI（Wilder 平滑口径）。

    >70 超买（不追多）、<30 超卖（不追空）；50 是多空分界。
    """
    if len(cs) < n + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(1, n + 1):
        d = cs[i].close - cs[i - 1].close
        if d >= 0:
            gains += d
        else:
            losses -= d
    avg_g = gains / n
    avg_l = losses / n
    # Wilder 递推
    for i in range(n + 1, len(cs)):
        d = cs[i].close - cs[i - 1].close
        g = d if d > 0 else 0.0
        l = -d if d < 0 else 0.0
        avg_g = (avg_g * (n - 1) + g) / n
        avg_l = (avg_l * (n - 1) + l) / n
    if avg_l <= 1e-12:
        return 100.0 if avg_g > 0 else 50.0
    rs = avg_g / avg_l
    return 100.0 - 100.0 / (1.0 + rs)


def zscore(cs, n: int = 20) -> float | None:
    """价格 Z-score：偏离 n 日均值多少个标准差。

    |z| > 2 表示价格已显著偏离均值（均值回归压力大，不宜顺势追单）。
    """
    if len(cs) < n:
        return None
    closes = [c.close for c in cs[-n:]]
    m = sum(closes) / n
    sd = stddev(closes, n)
    if not sd or sd <= 1e-12:
        return None
    return (closes[-1] - m) / sd


def roc(cs, n: int = 10) -> float | None:
    """变动率：(最新收盘 / n 根前收盘 - 1)。"""
    if len(cs) < n + 1:
        return None
    base = cs[-n - 1].close
    if base <= 0:
        return None
    return cs[-1].close / base - 1.0


# ---------------- 趋势强度 ----------------

def adx(cs, n: int = 14) -> dict | None:
    """ADX + 方向指标（+DI / -DI）。

    ADX > 25 趋势明确；< 20 基本是震荡。DI 对比给出方向。
    这里用简化版（Wilder 平滑的 DM/TR）。
    """
    if len(cs) < n * 2 + 1:
        return None
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(cs)):
        up = cs[i].high - cs[i - 1].high
        dn = cs[i - 1].low - cs[i].low
        plus_dm.append(up if (up > dn and up > 0) else 0.0)
        minus_dm.append(dn if (dn > up and dn > 0) else 0.0)
        pc = cs[i - 1].close
        trs.append(max(cs[i].high - cs[i].low, abs(cs[i].high - pc), abs(cs[i].low - pc)))

    def wilder(seq, n):
        if len(seq) < n:
            return []
        out = [sum(seq[:n])]
        for v in seq[n:]:
            out.append(out[-1] - out[-1] / n + v)
        return out

    tr_s = wilder(trs, n)
    p_s = wilder(plus_dm, n)
    m_s = wilder(minus_dm, n)
    if not tr_s or not p_s or not m_s:
        return None
    dxs = []
    for tr, p, m in zip(tr_s, p_s, m_s):
        if tr <= 1e-12:
            continue
        pdi = 100.0 * p / tr
        mdi = 100.0 * m / tr
        denom = pdi + mdi
        dxs.append(100.0 * abs(pdi - mdi) / denom if denom > 1e-12 else 0.0)
    if len(dxs) < n:
        return None
    # ADX = DX 的 n 期 Wilder 平均
    ax = sum(dxs[:n]) / n
    for dx in dxs[n:]:
        ax = (ax * (n - 1) + dx) / n
    last_pdi = 100.0 * p_s[-1] / tr_s[-1] if tr_s[-1] > 1e-12 else 0.0
    last_mdi = 100.0 * m_s[-1] / tr_s[-1] if tr_s[-1] > 1e-12 else 0.0
    return {"adx": ax, "plusDI": last_pdi, "minusDI": last_mdi}


# ---------------- 风险类 ----------------

def drawdown(values: list[float]) -> dict | None:
    """回撤序列统计。

    current 当前相对历史峰值的回撤（<=0 的百分比）
    max     历史最大回撤（<=0）
    peak    峰值
    """
    if not values or len(values) < 2:
        return None
    peak = -math.inf
    max_dd = 0.0
    for v in values:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (v - peak) / peak
            if dd < max_dd:
                max_dd = dd
    last_peak = max(values)
    cur = (values[-1] - last_peak) / last_peak if last_peak > 0 else 0.0
    return {"current": cur * 100, "max": max_dd * 100, "peak": last_peak}


# ---------------- 综合市场状态 ----------------

# 阈值集中在此，便于调参
ADX_TREND = 25.0        # ADX 高于此视为趋势市
ADX_RANGE = 20.0        # 低于此视为震荡市
ATR_HIGH_PCT = 85.0     # ATR 历史分位高于此 = 高波动
ATR_LOW_PCT = 15.0      # 低于此 = 低波动
BW_SQUEEZE_PCT = 20.0   # 带宽分位低于此 = 布林挤压
RSI_OVERBOUGHT = 70.0
RSI_OVERSOLD = 30.0
Z_EXTREME = 2.0


def market_state(cs, atr_n: int = 14, bb_n: int = 20, rsi_n: int = 14,
                 adx_n: int = 14, lookback: int = 120) -> dict:
    """一次性算出某个币种的全部指标与状态判定。

    返回结构直接给前端可视化用：
      price / atr / atrPct / realizedVol / boll / rsi / z / adx / roc
      regime   "trend" | "range" | "unknown"
      volState "high" | "normal" | "low"
      squeeze  bool（布林挤压，突破前兆）
    """
    out = {
        "price": cs[-1].close if cs else None,
        "bars": len(cs) if cs else 0,
    }
    if not cs or len(cs) < 30:
        out.update({"regime": "unknown", "volState": "unknown", "squeeze": False})
        return out

    a = atr(cs, atr_n)
    a_series = atr_series(cs, atr_n)
    atr_pct = percentile_rank(a_series, a) if (a is not None and a_series) else None

    bb = bollinger(cs, bb_n)
    bw_series = bandwidth_series(cs, bb_n, lookback=lookback)
    bw_pct = (percentile_rank(bw_series, bb["bandwidth"])
              if (bb and bb["bandwidth"] is not None and bw_series) else None)

    r = rsi(cs, rsi_n)
    z = zscore(cs, bb_n)
    ax = adx(cs, adx_n)
    rv = realized_vol(cs, 20)
    rc = roc(cs, 10)

    # regime：ADX 为主，布林挤压辅助
    regime = "unknown"
    if ax:
        if ax["adx"] >= ADX_TREND:
            regime = "trend"
        elif ax["adx"] <= ADX_RANGE:
            regime = "range"
        else:
            regime = "mixed"
    squeeze = bool(bw_pct is not None and bw_pct <= BW_SQUEEZE_PCT)

    vol_state = "normal"
    if atr_pct is not None:
        if atr_pct >= ATR_HIGH_PCT:
            vol_state = "high"
        elif atr_pct <= ATR_LOW_PCT:
            vol_state = "low"

    out.update({
        "atr": a,
        "atrPct": atr_pct,
        "realizedVol": rv,
        "boll": bb,
        "bwPct": bw_pct,
        "rsi": r,
        "z": z,
        "adx": ax["adx"] if ax else None,
        "plusDI": ax["plusDI"] if ax else None,
        "minusDI": ax["minusDI"] if ax else None,
        "roc": rc,
        "regime": regime,
        "volState": vol_state,
        "squeeze": squeeze,
    })
    return out


def signal_gate(state: dict, direction: str) -> tuple[bool, str]:
    """信号质量门：判断当前状态是否适合按该方向开仓。

    返回 (允许?, 拒绝原因)。原因是给用户看的中文说明。
    这是"形态之外"的第二层过滤——形态只说形状，这里看市场环境。
    """
    if not state:
        return True, ""
    # 震荡市：形态可靠性大幅下降
    if state.get("regime") == "range":
        return False, "当前处于震荡市（ADX 偏低），形态可靠性不足"
    # 极端波动：滑点与假突破风险高
    if state.get("volState") == "high":
        return False, "波动率处于高位，避免追单"
    # 布林挤压：方向未选择，等突破
    if state.get("squeeze"):
        return False, "布林带挤压（带宽极窄），等突破后再进场"
    z = state.get("z")
    # 价格已严重偏离均值：均值回归压力大，不顺势追
    if z is not None and abs(z) >= Z_EXTREME:
        if direction == "bullish" and z > 0:
            return False, f"价格已偏离均值 {z:.2f}σ（超买区），不追多"
        if direction == "bearish" and z < 0:
            return False, f"价格已偏离均值 {z:.2f}σ（超卖区），不追空"
    r = state.get("rsi")
    if r is not None:
        if direction == "bullish" and r >= RSI_OVERBOUGHT:
            return False, f"RSI {r:.0f} 已超买，不追多"
        if direction == "bearish" and r <= RSI_OVERSOLD:
            return False, f"RSI {r:.0f} 已超卖，不追空"
    # 趋势方向与信号方向冲突：逆势单胜率低
    if state.get("regime") == "trend":
        pdi, mdi = state.get("plusDI"), state.get("minusDI")
        if pdi is not None and mdi is not None:
            if direction == "bearish" and pdi > mdi + 8:
                return False, f"趋势向上（+DI {pdi:.0f} > -DI {mdi:.0f}），不逆势做空"
            if direction == "bullish" and mdi > pdi + 8:
                return False, f"趋势向下（-DI {mdi:.0f} > +DI {pdi:.0f}），不逆势做多"
    return True, ""


def adaptive_sl_tp(entry: float, atr_value: float | None, side: str,
                   sl_mult: float = 1.5, tp_mult: float = 2.5,
                   sl_min_pct: float = 0.004, tp_min_pct: float = 0.0018,
                   sl_max_pct: float = 0.02, tp_max_pct: float = 0.03) -> dict:
    """按 ATR 倍数给出止损/止盈价，并夹到 [min, max] 百分比区间。

    固定百分比的问题：低波动时止盈摸不到、高波动时止损被噪声扫掉。
    用 ATR 倍数能让风险距离跟随市场实际波动自动缩放。

    返回 {sl, tp, slPct, tpPct, dist}（dist = 止损距离占入场价比例）
    """
    if not entry or entry <= 0:
        return {"sl": None, "tp": None, "slPct": sl_min_pct, "tpPct": tp_min_pct, "dist": 0}
    if atr_value and atr_value > 0:
        sl_dist = atr_value * sl_mult
        tp_dist = atr_value * tp_mult
        sl_pct = min(sl_max_pct, max(sl_min_pct, sl_dist / entry))
        tp_pct = min(tp_max_pct, max(tp_min_pct, tp_dist / entry))
    else:
        # 没 ATR 时退回最小百分比
        sl_pct, tp_pct = sl_min_pct, tp_min_pct
    if side == "BUY":
        sl = entry * (1 - sl_pct)
        tp = entry * (1 + tp_pct)
    else:
        sl = entry * (1 + sl_pct)
        tp = entry * (1 - tp_pct)
    return {
        "sl": sl, "tp": tp,
        "slPct": sl_pct * 100, "tpPct": tp_pct * 100,
        "dist": sl_pct,
    }
