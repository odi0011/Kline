#!/usr/bin/env python3
"""多维信号质量回放：震荡 / 波动 / 噪声 / 假信号，谁真的有区分力。

只读，不写库、不连交易所。用 candle_archive + hits + decision_traces，
按与当前实盘一致的动态止损（clamp 0.4%~2.0%）和 rr 3.0 逐笔回放，
再按 regime / ATR 分位 / 影线噪声 / 时段 / 评分 / 形态 / 方向分组，
并用 7 天与 30 天双窗口筛掉只在单窗口好看的组合。

用法：
    python scripts/signal_quality.py --days 30
    python scripts/signal_quality.py --config /opt/kline-monitor/config.yaml --json quality.json
"""
from __future__ import annotations

import argparse
import bisect
import itertools
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import yaml
    import pymysql
except ImportError as exc:
    sys.exit(f"缺少依赖 {exc.name}：pip install pyyaml pymysql")

from analysis import indicators as IND  # noqa: E402
from core.candle import INTERVAL_MS  # noqa: E402
from param_sweep import db_config, replay, summarize  # noqa: E402

COST_PCT = 0.10
HOLD_MIN = 60
MIN_RR = 3.0
SL_MIN, SL_MAX = 0.004, 0.020
MATCH_MS = 60_000


def _objs(rows):
    return [SimpleNamespace(open_time=int(r[0]), open=float(r[1]),
                            high=float(r[2]), low=float(r[3]),
                            close=float(r[4]), volume=0.0) for r in rows]


def _features(rows):
    cs = _objs(rows)
    try:
        st = IND.market_state(cs)
    except Exception:
        st = {}
    rng = sum(max(c.high - c.low, 1e-12) for c in cs)
    body = sum(abs(c.close - c.open) for c in cs)
    return {
        "regime": st.get("regime") or "unknown",
        "atr": st.get("atr"),
        "atrPct": st.get("atrPct"),
        "volState": st.get("volState") or "unknown",
        "squeeze": bool(st.get("squeeze")),
        "noise": 1 - body / rng if rng else 0.5,
    }


def _quantile_bands(values: list[float]) -> tuple[float, float]:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return 0.0, 1.0
    return (vals[int(len(vals) * 0.33)], vals[int(len(vals) * 0.66)])


def _band(v, lo, hi):
    if v is None:
        return "unknown"
    if v <= lo:
        return "low"
    if v <= hi:
        return "mid"
    return "high"


def collect(conn, days: int, cost: float) -> dict:
    cur = conn.cursor()
    now = int(time.time() * 1000)
    since = now - days * 86_400_000
    since7 = now - 7 * 86_400_000

    cur.execute(
        "SELECT ts, symbol, `interval`, pattern, direction, price, bar_open_time, mode "
        "FROM hits WHERE mode='confirmed' AND direction IN ('bullish','bearish') "
        "AND ts>=%s", (since,))
    hits = [(int(r[0]), r[1], r[2], r[3], r[4], float(r[5]), int(r[6]), r[7])
            for r in cur.fetchall()]

    # 优先用 signal_features：特征在信号发生那一刻就已算好并永久保留。
    # 有了它就不必再凑前置 30 根K线，早期样本也能回放。
    snapshots: dict[tuple, dict] = {}
    try:
        cur.execute(
            "SELECT symbol,`interval`,mode,bar_open_time,pattern,direction,"
            "regime,adx,atr,atr_pct,vol_state,squeeze,noise_ratio,score,gap,grade "
            "FROM signal_features WHERE ts>=%s", (since,))
        for r in cur.fetchall():
            snapshots[(r[0], r[1], r[2], int(r[3] or 0), r[4], r[5])] = {
                "regime": r[6] or "unknown", "adx": r[7], "atr": r[8],
                "atrPct": r[9], "volState": r[10] or "unknown",
                "squeeze": bool(r[11]), "noise": r[12],
                "score": r[13], "gap": r[14], "grade": r[15],
            }
    except Exception:
        snapshots = {}

    cache: dict[tuple, list[tuple]] = {}
    cache_times: dict[tuple, list[int]] = {}

    def bars_for(sym, iv):
        key = (sym, iv)
        if key not in cache:
            cur.execute(
                "SELECT open_time, open, high, low, close FROM candle_archive "
                "WHERE symbol=%s AND `interval`=%s AND open_time>=%s ORDER BY open_time",
                (sym, iv, since - 86_400_000))
            cache[key] = [(int(r[0]), float(r[1]), float(r[2]),
                           float(r[3]), float(r[4])) for r in cur.fetchall()]
            cache_times[key] = [b[0] for b in cache[key]]
        return cache[key], cache_times[key]

    cur.execute("SELECT ts, symbol, payload FROM decision_traces "
                "WHERE kind='trace' AND ts>=%s", (since,))
    scored: dict[tuple, list[tuple]] = defaultdict(list)
    for ts, sym, payload in cur.fetchall():
        try:
            p = json.loads(payload) if isinstance(payload, str) else (payload or {})
            so = p.get("score")
            if not isinstance(so, dict) or so.get("score") is None:
                continue
            scored[(sym, p.get("pattern") or "", p.get("direction"))].append(
                (int(ts), float(so["score"]), float(so.get("gap") or 0)))
        except (ValueError, TypeError, AttributeError):
            continue
    for key in scored:
        scored[key].sort()

    events = []
    from_archive = 0
    for ts, sym, iv, pattern, direction, price, bar_open, mode in hits:
        bars, times = bars_for(sym, iv)
        iv_ms = INTERVAL_MS.get(iv)
        if not iv_ms or len(bars) < 5:
            continue
        idx = bisect.bisect_left(times, bar_open)
        if idx >= len(bars) - 4:
            continue
        snap = snapshots.get((sym, iv, mode, bar_open, pattern, direction))
        if snap is not None:
            feat = snap
            from_archive += 1
        else:
            if idx < 31:
                continue
            feat = _features(bars[idx - 30:idx + 1])
        atr = feat.get("atr") or 0.0
        sl_pct = SL_MIN
        if atr > 0:
            sl_pct = min(SL_MAX, max(SL_MIN, atr * 1.5 / price))
        max_bars = max(3, int(HOLD_MIN * 60_000 / iv_ms))
        ret = replay(bars, idx, price, direction, sl_pct, MIN_RR,
                     max_bars, cost)
        if ret is None:
            continue
        score = gap = None
        candidates = scored.get((sym, pattern, direction), [])
        if candidates:
            ctimes = [c[0] for c in candidates]
            j = bisect.bisect_left(ctimes, ts)
            best = None
            for k in (j - 1, j, j + 1):
                if 0 <= k < len(candidates):
                    delta = abs(candidates[k][0] - ts)
                    if delta <= MATCH_MS and (best is None or delta < best[0]):
                        best = (delta, candidates[k])
            if best:
                score, gap = best[1][1], best[1][2]
        if snap is not None:
            if score is None and snap.get("score") is not None:
                score = float(snap["score"])
            if gap is None and snap.get("gap") is not None:
                gap = float(snap["gap"])
        events.append({
            **feat,
            "ts": ts, "in7": ts >= since7, "ret": ret,
            "direction": direction, "pattern": pattern,
            "hour": datetime.fromtimestamp(ts / 1000, timezone.utc).hour,
            "score": score, "gap": gap,
        })

    atr_lo, atr_hi = _quantile_bands([e["atrPct"] for e in events])
    noise_lo, noise_hi = _quantile_bands([e["noise"] for e in events])
    # 归档实际只覆盖约 10 天，早期样本常因 K 线不足被跳过。用命中时间的
    # 中位数把可回放样本切成前后两半做样本外验证，而不是假装跑了 30 天。
    all_ts = sorted(e["ts"] for e in events)
    split_ts = all_ts[len(all_ts) // 2] if all_ts else now
    span_days = round((all_ts[-1] - all_ts[0]) / 86_400_000, 1) if all_ts else 0.0
    for e in events:
        e["atrBand"] = _band(e["atrPct"], atr_lo, atr_hi)
        e["noiseBand"] = _band(e["noise"], noise_lo, noise_hi)
        e["late"] = e["ts"] >= split_ts
        e["hourBand"] = ("00-06" if e["hour"] < 6 else
                         "06-12" if e["hour"] < 12 else
                         "12-18" if e["hour"] < 18 else "18-24")

    def stats(rows):
        s = summarize([r["ret"] for r in rows])
        early = summarize([r["ret"] for r in rows if not r["late"]])
        late = summarize([r["ret"] for r in rows if r["late"]])
        return {"n": s["n"], "nEarly": early["n"], "nLate": late["n"],
                "winrate": s["winrate"], "expectancy": s["expectancy"],
                "eEarly": early["expectancy"], "eLate": late["expectancy"],
                "total": s["total"]}

    def group(keyfn):
        buckets = defaultdict(list)
        for e in events:
            buckets[keyfn(e)].append(e)
        return {str(k): stats(v) for k, v in sorted(buckets.items())}

    dims = {
        "regime": group(lambda e: e["regime"]),
        "atrBand": group(lambda e: e["atrBand"]),
        "noiseBand": group(lambda e: e["noiseBand"]),
        "volState": group(lambda e: e["volState"]),
        "hourBand": group(lambda e: e["hourBand"]),
        "squeeze": group(lambda e: "squeeze" if e["squeeze"] else "normal"),
        "direction": group(lambda e: "short" if e["direction"] == "bearish" else "long"),
        "pattern": group(lambda e: e["pattern"]),
        "score": group(lambda e: "no_score" if e["score"] is None else
                       "<55" if e["score"] < 55 else ">=55"),
        "gap": group(lambda e: "no_gap" if e["gap"] is None else
                     "<0" if e["gap"] < 0 else "0-15" if e["gap"] < 15 else
                     "15-30" if e["gap"] < 30 else ">=30"),
    }

    def label(e, dim, val):
        if val == "all":
            return True
        if dim == "direction":
            return (e["direction"] == "bearish") if val == "short" else (
                e["direction"] == "bullish")
        if dim == "regime":
            return {"range": e["regime"] == "range",
                    "mixed": e["regime"] == "mixed",
                    "trend": e["regime"] == "trend",
                    "non_trend": e["regime"] != "trend"}[val]
        if dim == "noise":
            return e["noiseBand"] == val
        if dim == "atr":
            return e["atrBand"] == val
        if dim == "gap":
            return (e["gap"] is not None and
                    ("<15" == val and e["gap"] < 15 or
                     ">=15" == val and e["gap"] >= 15))
        if dim == "score":
            return (e["score"] is not None and
                    ("<55" == val and e["score"] < 55 or
                     ">=55" == val and e["score"] >= 55))
        return True

    space = {
        "direction": ("all", "short", "long"),
        "regime": ("all", "range", "mixed", "non_trend", "trend"),
        "noise": ("all", "low", "mid", "high"),
        "atr": ("all", "low", "mid", "high"),
        "gap": ("all", "<15", ">=15"),
        "score": ("all", "<55", ">=55"),
    }
    combos = []
    for values in itertools.product(*space.values()):
        cond = dict(zip(space, values))
        if sum(v != "all" for v in values) < 2:
            continue
        picked = [e for e in events
                  if all(label(e, k, v) for k, v in cond.items())]
        if len(picked) < 120:
            continue
        early = [e["ret"] for e in picked if not e["late"]]
        late = [e["ret"] for e in picked if e["late"]]
        if len(early) < 25 or len(late) < 25:
            continue
        ee = summarize(early)["expectancy"]
        el = summarize(late)["expectancy"]
        if ee <= 0 or el <= 0:
            continue
        combos.append({"cond": cond, "n": len(picked),
                       "nEarly": len(early), "nLate": len(late),
                       "eEarly": ee, "eLate": el,
                       "score": min(ee, el),
                       "winrate": summarize([e["ret"] for e in picked])["winrate"]})
    combos.sort(key=lambda x: -x["score"])

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days": days, "costPct": cost,
        "hits": len(hits), "replayed": len(events),
        "fromArchive": from_archive,
        "spanDays": span_days,
        "splitAt": datetime.fromtimestamp(split_ts / 1000, timezone.utc).isoformat(),
        "atrBands": [atr_lo, atr_hi], "noiseBands": [noise_lo, noise_hi],
        "baseline": stats(events),
        "dims": dims, "combos": combos[:12],
    }


def render(d: dict) -> None:
    print(f"\n=== 信号质量多维回放（{d['days']} 天，{d['generated_at'][:19]} UTC）===")
    print(f"命中 {d['hits']} 条，可回放 {d['replayed']} 条"
          f"（其中 {d.get('fromArchive', 0)} 条用永久特征快照，无需前置K线）；"
          f"动态止损 0.4~2.0% / rr 3.0 / 扣双边 {d['costPct']}%")
    b = d["baseline"]
    print(f"实际可回放跨度 {d['spanDays']} 天，前后半切分点 {d['splitAt'][:16]} UTC")
    print(f"基准：{b['n']} 笔，胜率 {b['winrate']:.1%}，"
          f"前半期望 {b['eEarly']:+.4f}%，后半 {b['eLate']:+.4f}%\n")
    for name, groups in d["dims"].items():
        if name == "pattern" and len(groups) > 8:
            groups = dict(sorted(groups.items(), key=lambda kv: -kv[1]["n"])[:8])
        print(f"[{name}]")
        print(f"    {'分组':<16}{'笔数':>7}{'前半':>7}{'胜率':>8}{'前半E':>10}{'后半E':>10}")
        for k, v in groups.items():
            print(f"    {k:<16}{v['n']:>7}{v['nEarly']:>7}{v['winrate']:>7.1%}"
                  f"{v['eEarly']:>+10.4f}{v['eLate']:>+10.4f}")
        print()
    print("[前后半都为正的组合]（样本外验证，按较差那段排序）")
    if not d["combos"]:
        print("    无 —— 没有任何组合在前后两段里同时为正")
    for c in d["combos"]:
        cond = " ".join(f"{k}={v}" for k, v in c["cond"].items() if v != "all")
        print(f"    {cond:<62} n={c['n']:<5}"
              f" 前半={c['eEarly']:+.4f} 后半={c['eLate']:+.4f}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description="只读信号质量多维回放")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--cost", type=float, default=COST_PCT)
    ap.add_argument("--json", default="")
    args = ap.parse_args()
    path = Path(args.config)
    if not path.exists():
        sys.exit(f"找不到配置文件：{path}")
    conn = pymysql.connect(**db_config(path))
    try:
        data = collect(conn, max(7, args.days), args.cost)
    finally:
        conn.close()
    render(data)
    if args.json:
        Path(args.json).write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        print(f"已写出 {args.json}")


if __name__ == "__main__":
    main()
