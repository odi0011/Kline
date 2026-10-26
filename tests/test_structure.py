"""结构形态检测器自测：用合成K线构造标准形态，验证识别与几何点位。

不依赖网络/数据库，直接跑：python tests/test_structure.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.candle import Candle, INTERVAL_MS  # noqa: E402
import patterns.structure as S  # noqa: E402
from analysis.structure import analyze  # noqa: E402


def mk(prices, iv="1m"):
    """价格序列 → K线（每根实体由相邻价格差决定，影线上下各留少量）。"""
    iv_ms = INTERVAL_MS[iv]
    out = []
    for i, p in enumerate(prices):
        prev = prices[i - 1] if i else p
        o, c = prev, p
        hi = max(o, c) * 1.0008
        lo = min(o, c) * 0.9992
        out.append(Candle(i * iv_ms, o, hi, lo, c, volume=100, interval=iv))
    out[-1].closed = True
    return out


def V(seq):
    """把关键点序列插值成逐根价格。"""
    out = [seq[0]]
    for a, b in zip(seq, seq[1:]):
        steps = 6
        for k in range(1, steps + 1):
            out.append(a + (b - a) * k / steps)
    return out


def check(name, cs, expect_key):
    piv = S.pivots_of(cs)
    r = S.detect_best(cs, require_break=True)
    got = r["key"] if r else None
    ok = got == expect_key
    print(f"{'PASS' if ok else 'FAIL'}  {name:14s} expect={expect_key:22s} got={got}  pivots={len(piv)}")
    if r:
        print(f"        points={len(r['points'])} bars={r['bars']} status={r['status']} neck={r.get('neck')}")
    return ok


fails = 0

# 头肩底：下跌 → 左肩(低) ↑ 颈 → 头(更低) ↑ 颈 → 右肩(与左肩齐) → 突破颈线
hsb = V([120, 108, 112, 96, 116, 112, 118, 130])
fails += not check("头肩底", mk(hsb), "head_shoulders_bottom")

# 头肩顶：上涨 → 左肩(高) ↓ 颈 → 头(更高) ↓ 颈 → 右肩 → 跌破颈线
hst = V([100, 112, 106, 120, 104, 108, 102, 90])
fails += not check("头肩顶", mk(hst), "head_shoulders_top")

# 三重底：下跌 → 三次同一低位 → 突破
tb = V([125, 100, 112, 100, 112, 100, 118])
fails += not check("三重底", mk(tb), "triple_bottom")

# 三重顶：上涨 → 三次同一高位 → 跌破
tt = V([95, 120, 108, 120, 108, 120, 102])
fails += not check("三重顶", mk(tt), "triple_top")

# 双底
db = V([120, 100, 114, 100, 122])
fails += not check("双底", mk(db), "double_bottom")

# 双顶
dt = V([100, 120, 106, 120, 98])
fails += not check("双顶", mk(dt), "double_top")

# 结构分析产出
cs = mk(hsb)
a = analyze(cs)
print(f"\nanalysis keys: levels={len(a['levels'])} trendlines={len(a['trendlines'])} "
      f"zones={len(a['zones'])} pattern={a['pattern']['key'] if a['pattern'] else None} atr={a['atr']}")
if a["levels"]:
    print("  top level:", a["levels"][0])
if a["zones"]:
    print("  zone:", a["zones"][0])

print("\n" + ("全部通过" if fails == 0 else f"{fails} 个用例失败"))
sys.exit(1 if fails else 0)
