"""技术指标单测：用可手算的合成数据验证每个指标的数值正确性。

跑法：python tests/test_indicators.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.candle import Candle  # noqa: E402
import analysis.indicators as I  # noqa: E402

FAILS = []


def check(name, got, want, tol=1e-6):
    ok = False
    if got is None or want is None:
        ok = got is want
    else:
        ok = abs(got - want) <= tol
    print(f"{'PASS' if ok else 'FAIL':4} {name:<34} got={got!r:>22}  want={want!r}")
    if not ok:
        FAILS.append(name)


def mk(closes, highs=None, lows=None):
    """由收盘价序列构造K线（默认 high/low 就是收盘价）。"""
    out = []
    for i, c in enumerate(closes):
        h = highs[i] if highs else c
        l = lows[i] if lows else c
        out.append(Candle(i * 60_000, c, h, l, c, interval="1m", closed=True))
    return out


# ---------------- 基础统计 ----------------
check("sma([1..10], 5)", I.sma(list(range(1, 11)), 5), 8.0)
check("sma 数据不足", I.sma([1, 2], 5), None)
check("stddev([2,4,4,4,5,5,7,9], 8)", I.stddev([2, 4, 4, 4, 5, 5, 7, 9], 8), 2.138089935299395, 1e-9)
check("ema 常数序列收敛", I.ema([5.0] * 20, 5), 5.0)
check("percentile_rank 中位", I.percentile_rank([1, 2, 3, 4, 5], 3), 60.0)

# ---------------- ATR ----------------
# 全部K线 H-L = 2，无跳空 → ATR 应为 2
cs = mk([10, 10, 10, 10, 10], highs=[11] * 5, lows=[9] * 5)
check("atr 恒定振幅=2", I.atr(cs, 5), 2.0)
# 混合：H-L=2 且相邻收盘无跳空
check("atr 数据不足", I.atr(cs, 10), None)

# ---------------- 布林带 ----------------
# 收盘 [10]*20 → 标准差 0 → sd=0 会导致 mid>0 但 sd=None? 这里 sd=0，span=0，pctB=0.5
bb = I.bollinger(mk([10.0] * 20), 20, 2)
check("boll 常数序列 mid", bb["mid"], 10.0)
check("boll 常数序列 bandwidth", bb["bandwidth"], 0.0)
check("boll 常数序列 pctB", bb["pctB"], 0.5)
# 已知数据：1..20，均值10.5，样本std≈5.9161
bb2 = I.bollinger(mk([float(i) for i in range(1, 21)]), 20, 2)
check("boll 1..20 mid", bb2["mid"], 10.5)
check("boll 1..20 std", bb2["std"], 5.916079783099616, 1e-9)
check("boll 1..20 upper", bb2["upper"], 10.5 + 2 * 5.916079783099616, 1e-9)
# pctB：1..20 递增序列里 band 被趋势撑宽，最后价仍在上轨内 → 0 < pctB < 1
assert 0 < bb2["pctB"] < 1, f"递增序列 pctB 应在 0~1 内，实际 {bb2['pctB']}"
print(f"PASS boll pctB 合理区间              got={bb2['pctB']:.3f}")
# 真正突破：20 根 10 之后突然 30 → pctB > 1
bb3 = I.bollinger(mk([10.0] * 19 + [30.0]), 20, 2)
assert bb3["pctB"] > 1, f"跳涨应突破上轨，实际 {bb3['pctB']}"
print(f"PASS boll 跳涨突破上轨 pctB>1        got={bb3['pctB']:.3f}")

# ---------------- RSI ----------------
# 连续上涨 → RSI 100
up = mk([float(i) for i in range(1, 30)])
check("rsi 单边上涨=100", I.rsi(up, 14), 100.0)
# 连续下跌 → RSI 0
dn = mk([float(100 - i) for i in range(30)])
check("rsi 单边下跌=0", I.rsi(dn, 14), 0.0)
# 交替等幅涨跌 → RSI 约 50
osc = mk([100 + (1 if i % 2 == 0 else 0) for i in range(40)])
r_osc = I.rsi(osc, 14)
print(f"PASS rsi 震荡≈50                     got={r_osc:.2f}")
assert 40 <= r_osc <= 60, f"震荡序列 RSI 应接近 50，实际 {r_osc}"

# ---------------- Z-score ----------------
z = I.zscore(mk([10.0] * 19 + [20.0]), 20)
# 均值 10.5，std≈2.198，最后一价 20 → z≈4.32
print(f"PASS zscore 跳涨 >2                  got={z:.2f}")
assert z > 2, f"单点跳涨 z 应 > 2，实际 {z}"
check("zscore 常数序列", I.zscore(mk([10.0] * 20), 20), None)

# ---------------- ADX ----------------
# 单边上涨趋势 → ADX 应显著为正且 +DI > -DI
trend = mk([float(100 + i * 2) for i in range(60)])
ax = I.adx(trend, 14)
print(f"PASS adx 单边趋势                    adx={ax['adx']:.1f} +DI={ax['plusDI']:.1f} -DI={ax['minusDI']:.1f}")
assert ax["adx"] > 20 and ax["plusDI"] > ax["minusDI"], "单边上涨应 ADX 高且 +DI 主导"

# 横盘 → ADX 低
flat = mk([100 + (i % 2) * 0.01 for i in range(60)])
ax2 = I.adx(flat, 14)
print(f"PASS adx 横盘偏低                    adx={ax2['adx']:.1f}")
assert ax2["adx"] < 40, f"横盘 ADX 应偏低，实际 {ax2['adx']}"

# ---------------- 实测波动率 ----------------
check("realized_vol 常数序列=0", I.realized_vol(mk([10.0] * 30), 20), 0.0)
rv = I.realized_vol(mk([100 * (1.01 ** i) for i in range(30)]), 20)
# 每根恒定 +1% → 对数收益恒定 → 标准差 0
check("realized_vol 恒定涨幅=0", rv, 0.0, 1e-9)

# ---------------- 回撤 ----------------
dd = I.drawdown([100, 110, 105, 120, 90, 100])
check("drawdown 当前", dd["current"], (100 - 120) / 120 * 100, 1e-9)
check("drawdown 最大", dd["max"], (90 - 120) / 120 * 100, 1e-9)
check("drawdown 峰值", dd["peak"], 120.0)

# ---------------- 自适应止损止盈 ----------------
# ATR=100，入场 10000，1.5 倍止损 → 1.5%，2.5 倍止盈 → 2.5%
r = I.adaptive_sl_tp(10000, 100, "BUY", sl_mult=1.5, tp_mult=2.5,
                     sl_min_pct=0.001, tp_min_pct=0.001, sl_max_pct=0.05, tp_max_pct=0.05)
check("adaptive BUY slPct", r["slPct"], 1.5, 1e-9)
check("adaptive BUY tpPct", r["tpPct"], 2.5, 1e-9)
check("adaptive BUY sl 价", r["sl"], 10000 * (1 - 0.015), 1e-6)
check("adaptive BUY tp 价", r["tp"], 10000 * (1 + 0.025), 1e-6)
# SELL 镜像
r2 = I.adaptive_sl_tp(10000, 100, "SELL", sl_mult=1.5, tp_mult=2.5,
                      sl_min_pct=0.001, tp_min_pct=0.001, sl_max_pct=0.05, tp_max_pct=0.05)
check("adaptive SELL sl 价", r2["sl"], 10000 * (1 + 0.015), 1e-6)
check("adaptive SELL tp 价", r2["tp"], 10000 * (1 - 0.025), 1e-6)
# 夹紧到下限（ATR 极小时）
r3 = I.adaptive_sl_tp(10000, 0.1, "BUY", sl_min_pct=0.004, tp_min_pct=0.0018)
check("adaptive 触发止损下限", r3["slPct"], 0.4, 1e-9)
# 夹紧到上限（ATR 极大时）
r4 = I.adaptive_sl_tp(10000, 5000, "BUY", sl_max_pct=0.02, tp_max_pct=0.03)
check("adaptive 触发止损上限", r4["slPct"], 2.0, 1e-9)

# ---------------- market_state / signal_gate ----------------
st = I.market_state(trend)
print(f"PASS market_state 趋势               regime={st['regime']} vol={st['volState']}")
assert st["regime"] in ("trend", "mixed")

st_flat = I.market_state(flat)
print(f"PASS market_state 横盘               regime={st_flat['regime']}")

# 震荡市应拒绝开仓
allow, why = I.signal_gate({"regime": "range"}, "bullish")
print(f"PASS gate 震荡市拒绝                  allow={allow} why={why[:24]}")
assert not allow

# RSI 超买拒绝做多
allow2, why2 = I.signal_gate({"regime": "trend", "rsi": 78, "z": 1.0,
                              "plusDI": 30, "minusDI": 10}, "bullish")
print(f"PASS gate RSI超买拒绝做多             allow={allow2} why={why2[:24]}")
assert not allow2

# 趋势向上时拒绝逆势做空
allow3, why3 = I.signal_gate({"regime": "trend", "rsi": 55, "z": 0.5,
                              "plusDI": 35, "minusDI": 12}, "bearish")
print(f"PASS gate 逆势做空拒绝                allow={allow3} why={why3[:24]}")
assert not allow3

# 正常趋势顺势 → 放行
allow4, why4 = I.signal_gate({"regime": "trend", "rsi": 55, "z": 0.8,
                              "plusDI": 35, "minusDI": 12,
                              "volState": "normal", "squeeze": False}, "bullish")
print(f"PASS gate 顺势放行                    allow={allow4}")
assert allow4

print()
if FAILS:
    print(f"{len(FAILS)} 个断言失败: {FAILS}")
    sys.exit(1)
print("全部通过")
