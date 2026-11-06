#!/usr/bin/env python3
"""离线回放：用线上 candle_archive + hits 扫描止损/止盈与评分阈值。

只读，不写库、不连交易所。所有参数由这段真实行情回放选出，不拍脑袋。

规则（与线上执行尽量一致）：
* 信号在 bar T 确认，从 T+1 根开始逐根回放，最长持仓 60 分钟。
* 同一根 K 线内上下都触及 → 保守判止损优先。
* 跳空越过止损 → 按开盘价成交，不是按止损价。
* 双边手续费 + 滑点按 cost_pct（默认 0.10%）扣。
* 超时按最后一根收盘价平仓。

用法：
    python scripts/param_sweep.py --days 7
    python scripts/param_sweep.py --config /opt/kline-monitor/config.yaml --days 30 --json sweep.json
"""
from __future__ import annotations

import argparse
import bisect
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import yaml
    import pymysql
except ImportError as exc:
    sys.exit(f"缺少依赖 {exc.name}：pip install pyyaml pymysql")

from core.candle import INTERVAL_MS  # noqa: E402
from strategy_report import db_config  # noqa: E402

SL_GRID = (0.002, 0.003, 0.004, 0.005, 0.006, 0.008)
RR_GRID = (0.36, 1.0, 1.5, 2.0, 2.5, 3.0)
COST_PCT = 0.10          # 百分点：双边 taker 手续费 + 滑点
HOLD_MIN = 60
MATCH_MS = 60_000


def replay(bars: list[tuple], idx: int, entry: float, direction: str,
           sl_pct: float, rr: float, max_bars: int, cost: float) -> float | None:
    """返回这一笔的净收益率（百分点）；窗口不足返回 None。"""
    window = bars[idx + 1:idx + 1 + max_bars]
    if len(window) < 3:
        return None
    sign = 1 if direction == "bullish" else -1
    sl = entry * (1 - sign * sl_pct)
    tp = entry * (1 + sign * sl_pct * rr)
    for _t, op, hi, lo, close in window:
        gap_sl = (op <= sl) if direction == "bullish" else (op >= sl)
        hit_sl = (lo <= sl) if direction == "bullish" else (hi >= sl)
        hit_tp = (hi >= tp) if direction == "bullish" else (lo <= tp)
        if gap_sl:
            return (op - entry) / entry * sign * 100 - cost
        if hit_sl and hit_tp:
            return -sl_pct * 100 - cost
        if hit_sl:
            return -sl_pct * 100 - cost
        if hit_tp:
            return sl_pct * rr * 100 - cost
    return (window[-1][4] - entry) / entry * sign * 100 - cost


def summarize(rets: list[float]) -> dict:
    if not rets:
        return {"n": 0, "winrate": 0.0, "expectancy": 0.0, "total": 0.0,
                "maxDD": 0.0, "profitFactor": 0.0}
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r < 0]
    equity = peak = 0.0
    max_dd = 0.0
    for r in rets:
        equity += r
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    return {
        "n": len(rets),
        "winrate": round(len(wins) / len(rets), 4),
        "expectancy": round(sum(rets) / len(rets), 4),
        "total": round(sum(rets), 2),
        "maxDD": round(max_dd, 2),
        "profitFactor": round(gross_win / gross_loss, 3) if gross_loss else None,
    }


def collect(conn, days: int, cost: float) -> dict:
    cur = conn.cursor()
    now = int(time.time() * 1000)
    since = now - days * 86_400_000
    cur.execute(
        "SELECT ts, symbol, `interval`, pattern, direction, price, bar_open_time "
        "FROM hits WHERE mode='confirmed' AND direction IN ('bullish','bearish') "
        "AND ts>=%s", (since,))
    hits = [(int(r[0]), r[1], r[2], r[3], r[4], float(r[5]), int(r[6]))
            for r in cur.fetchall()]

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

    # 把 decision_traces 的评分关联到命中，用于阈值对照
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
                (int(ts), float(so["score"]), float(so.get("gap") or 0),
                 (so.get("breakdown") or {}).get("regime") or "unknown"))
        except (ValueError, TypeError, AttributeError):
            continue
    for key in scored:
        scored[key].sort()

    events = []
    for ts, sym, iv, pattern, direction, price, bar_open in hits:
        bars, times = bars_for(sym, iv)
        iv_ms = INTERVAL_MS.get(iv)
        if not iv_ms or not bars:
            continue
        idx = bisect.bisect_left(times, bar_open)
        if idx >= len(bars) - 4:
            continue
        max_bars = max(3, int(HOLD_MIN * 60_000 / iv_ms))
        candidates = scored.get((sym, pattern, direction), [])
        score = gap = None
        regime = "unknown"
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
                score, gap, regime = best[1][1], best[1][2], best[1][3]
        events.append((bars, idx, price, direction, max_bars,
                       score, gap, regime))

    grid = []
    for sl in SL_GRID:
        for rr in RR_GRID:
            rets = []
            for bars, idx, entry, direction, max_bars, *_ in events:
                r = replay(bars, idx, entry, direction, sl, rr, max_bars, cost)
                if r is not None:
                    rets.append(r)
            grid.append({"slPct": round(sl * 100, 2), "rr": rr,
                         **summarize(rets)})
    grid.sort(key=lambda x: -x["expectancy"])

    current = next((g for g in grid
                    if g["slPct"] == 0.5 and g["rr"] == 0.36), None)
    best = grid[0] if grid else None

    # 在最优止损/盈亏比下，对照不同评分门槛
    thresholds = []
    if best:
        sl, rr = best["slPct"] / 100, best["rr"]
        def rets_for(predicate):
            out = []
            for bars, idx, entry, direction, max_bars, score, gap, regime in events:
                if not predicate(score, gap, regime):
                    continue
                r = replay(bars, idx, entry, direction, sl, rr, max_bars, cost)
                if r is not None:
                    out.append(r)
            return out
        cases = {
            "全部信号（不过滤）": lambda s, g, r: True,
            "当前门槛 score>=55 & gap>=15": lambda s, g, r: s is not None and s >= 55 and g >= 15,
            "score<55": lambda s, g, r: s is not None and s < 55,
            "score>=40": lambda s, g, r: s is not None and s >= 40,
            "regime!=trend": lambda s, g, r: r != "trend",
            "只做空": lambda s, g, r: True,
        }
        for name, pred in cases.items():
            if name == "只做空":
                out = [r for bars, idx, entry, direction, mb, *_ in events
                       if direction == "bearish"
                       for r in [replay(bars, idx, entry, direction, sl, rr, mb, cost)]
                       if r is not None]
            else:
                out = rets_for(pred)
            thresholds.append({"case": name, **summarize(out)})

    dir_grid = []
    for sl in SL_GRID:
        for rr in RR_GRID:
            row = {"slPct": round(sl * 100, 2), "rr": rr}
            for label, pred in (("all", lambda d: True),
                                ("short", lambda d: d == "bearish"),
                                ("long", lambda d: d == "bullish")):
                rets = []
                for bars, idx, entry, direction, max_bars, *_ in events:
                    if not pred(direction):
                        continue
                    r = replay(bars, idx, entry, direction, sl, rr, max_bars, cost)
                    if r is not None:
                        rets.append(r)
                row[label] = summarize(rets)["expectancy"]
            dir_grid.append(row)
    dir_grid.sort(key=lambda x: -x["short"])

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days": days, "costPct": cost, "holdMin": HOLD_MIN,
        "hits": len(hits), "replayed": len(events),
        "withScore": sum(1 for e in events if e[5] is not None),
        "current": current, "best": best,
        "grid": grid[:12], "thresholds": thresholds,
        "dirGrid": dir_grid[:10],
    }


def render(d: dict) -> None:
    print(f"\n=== 参数回放扫描（近 {d['days']} 天，{d['generated_at'][:19]} UTC）===")
    print(f"输入 {d['hits']} 条收线确认信号，可回放 {d['replayed']} 条，"
          f"其中 {d['withScore']} 条带共鸣分")
    print(f"口径：最长持仓 {d['holdMin']} 分钟，同根双触判止损，"
          f"跳空按开盘价，双边成本 {d['costPct']}%\n")
    c, b = d["current"], d["best"]
    if c:
        print(f"[当前线上] 止损 {c['slPct']}% / rr {c['rr']}："
              f"{c['n']} 笔，胜率 {c['winrate']:.1%}，"
              f"期望 {c['expectancy']:+.4f}%/笔，合计 {c['total']:+.2f}%，"
              f"最大回撤 {c['maxDD']:.2f}%")
    if b:
        print(f"[数据最优] 止损 {b['slPct']}% / rr {b['rr']}："
              f"{b['n']} 笔，胜率 {b['winrate']:.1%}，"
              f"期望 {b['expectancy']:+.4f}%/笔，合计 {b['total']:+.2f}%，"
              f"最大回撤 {b['maxDD']:.2f}%\n")
    print("[1] 网格前 12（按每笔期望排序）")
    print(f"    {'止损%':<8}{'rr':<7}{'笔数':<8}{'胜率':<9}{'期望%':<10}{'合计%':<11}{'PF':<7}")
    for g in d["grid"]:
        print(f"    {g['slPct']:<8}{g['rr']:<7}{g['n']:<8}{g['winrate']:>7.1%}"
              f"  {g['expectancy']:>+9.4f}{g['total']:>+11.2f}"
              f"  {str(g['profitFactor']):<7}")
    if d["thresholds"]:
        print("\n[2] 在最优参数下对照评分门槛")
        for t in d["thresholds"]:
            print(f"    {t['case']:<32}{t['n']:>6} 笔  胜率 {t['winrate']:>7.1%}"
                  f"  期望 {t['expectancy']:>+9.4f}  合计 {t['total']:>+9.2f}")
    if d.get("dirGrid"):
        print("\n[3] 方向 × 参数（按只做空期望排序，单位 %/笔）")
        print(f"    {'止损%':<8}{'rr':<7}{'全部':>10}{'只做空':>11}{'只做多':>11}")
        for g in d["dirGrid"]:
            print(f"    {g['slPct']:<8}{g['rr']:<7}{g['all']:>+10.4f}"
                  f"{g['short']:>+11.4f}{g['long']:>+11.4f}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description="只读参数回放扫描")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--cost", type=float, default=COST_PCT)
    ap.add_argument("--json", default="")
    args = ap.parse_args()
    path = Path(args.config)
    if not path.exists():
        sys.exit(f"找不到配置文件：{path}")
    conn = pymysql.connect(**db_config(path))
    try:
        data = collect(conn, max(1, args.days), args.cost)
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
