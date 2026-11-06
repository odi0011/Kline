#!/usr/bin/env python3
"""信号 lift 对照：放行 / 拦截 / 随机入场，用线上真实数据校准共鸣分阈值。

只读，不写库、不连交易所。三类对照：

* 真实信号：decision_traces 里带共鸣分的决策，按 (币种, 形态, 方向, 时间)
  关联 hits 的 10 分钟判定结果与 eval_price，算出「如果放行会怎样」。
* 放行 vs 拦截：拦截组若事后判定为 win，就是被误杀的信号。
* 随机入场基线：从 candle_archive 按真实信号的币种/周期分布随机抽样，
  用与 eval_outcomes 完全相同的 ±eval_threshold 先触及规则回放。

最终调用 decision.backtest.random_entry_lift 给出期望差，并扫描候选阈值。
用法：
    python scripts/signal_lift.py --days 7
    python scripts/signal_lift.py --config /opt/kline-monitor/config.yaml --json lift.json
"""
from __future__ import annotations

import argparse
import bisect
import json
import random
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
from decision.backtest import random_entry_lift  # noqa: E402
from strategy_report import db_config  # noqa: E402

SCORE_BINS = ((0, 45), (45, 55), (55, 65), (65, 75), (75, 101))
GAP_BINS = ((-1e9, 0), (0, 15), (15, 30), (30, 1e9))
MATCH_WINDOW_MS = 60_000


def load_params(path: Path) -> tuple[int, float]:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    s = cfg.get("storage") or {}
    return int(s.get("eval_minutes", 10)), float(s.get("eval_threshold", 0.0015))


def judge(price: float, direction: str, window: list[tuple], threshold: float):
    """与 storage.eval_outcomes 完全一致的先触及判定，返回 (结果, 窗口末收盘)。"""
    if len(window) < 3:
        return None, None
    up_lvl = price * (1 + threshold)
    dn_lvl = price * (1 - threshold)
    result = "flat"
    for high, low, close in window:
        touch_up = high >= up_lvl
        touch_dn = low <= dn_lvl
        if touch_up and touch_dn:
            result = "loss"
            break
        if direction == "bullish":
            if touch_up:
                result = "win"
                break
            if touch_dn:
                result = "loss"
                break
        else:
            if touch_dn:
                result = "win"
                break
            if touch_up:
                result = "loss"
                break
    return result, float(window[-1][2])


def bin_label(value: float, bins) -> str:
    for lo, hi in bins:
        if lo <= value < hi:
            if lo <= -1e8:
                return f"< {hi:g}"
            if hi >= 1e8:
                return f">= {lo:g}"
            return f"{lo:g}-{hi:g}"
    return "other"


def summarize(rows: list[dict], control_rets: list[float]) -> dict:
    judged = [r for r in rows if r["outcome"] in ("win", "loss")]
    wins = sum(1 for r in judged if r["outcome"] == "win")
    rets = [r["ret"] for r in rows]
    lift = random_entry_lift(rets, control_rets) if rets and control_rets else {
        "lift": 0.0, "realExpectancy": 0.0, "controlExpectancy": 0.0}
    return {
        "n": len(rows), "judged": len(judged), "wins": wins,
        "winrate": round(wins / len(judged), 4) if judged else 0.0,
        "avgRet": round(sum(rets) / len(rets), 4) if rets else 0.0,
        "lift": lift["lift"],
    }


def collect(conn, days: int, samples: int, seed: int,
            eval_minutes: int, threshold: float) -> dict:
    cur = conn.cursor()
    now = int(time.time() * 1000)
    since = now - days * 86_400_000

    cur.execute(
        "SELECT ts, symbol, payload FROM decision_traces "
        "WHERE kind='trace' AND ts>=%s", (since,))
    traces = []
    for ts, symbol, payload in cur.fetchall():
        try:
            p = json.loads(payload) if isinstance(payload, str) else (payload or {})
        except (ValueError, TypeError):
            continue
        direction = p.get("direction")
        if direction not in ("bullish", "bearish"):
            continue
        score_obj = p.get("score")
        if not isinstance(score_obj, dict) or score_obj.get("score") is None:
            continue
        features = {}
        for stage in p.get("stages") or []:
            if stage.get("id") == "features":
                features = stage.get("factors") or {}
        traces.append({
            "ts": int(ts), "symbol": symbol,
            "pattern": p.get("pattern") or "", "direction": direction,
            "status": p.get("status") or "",
            "score": float(score_obj["score"]),
            "gap": float(score_obj.get("gap") or 0),
            "regime": (score_obj.get("breakdown") or {}).get("regime")
                      or features.get("regime") or "unknown",
        })

    cur.execute(
        "SELECT ts, symbol, `interval`, pattern, direction, price, outcome, eval_price "
        "FROM hits WHERE outcome IN ('win','loss','flat') AND eval_price IS NOT NULL "
        "AND ts>=%s", (since,))
    by_key: dict[tuple, list[tuple]] = defaultdict(list)
    interval_counts: dict[tuple, int] = defaultdict(int)
    for ts, symbol, interval, pattern, direction, price, outcome, eval_price in cur.fetchall():
        by_key[(symbol, pattern, direction)].append(
            (int(ts), interval, float(price), outcome, float(eval_price)))
    for key in by_key:
        by_key[key].sort()
        for row in by_key[key]:
            interval_counts[(key[0], row[1])] += 1

    rows = []
    matched = 0
    for t in traces:
        candidates = by_key.get((t["symbol"], t["pattern"], t["direction"]), [])
        if not candidates:
            continue
        times = [c[0] for c in candidates]
        idx = bisect.bisect_left(times, t["ts"])
        best = None
        for j in (idx - 1, idx, idx + 1):
            if 0 <= j < len(candidates):
                delta = abs(candidates[j][0] - t["ts"])
                if delta <= MATCH_WINDOW_MS and (best is None or delta < best[0]):
                    best = (delta, candidates[j])
        if best is None:
            continue
        _ts, interval, price, outcome, eval_price = best[1]
        sign = 1 if t["direction"] == "bullish" else -1
        rows.append({
            **t, "interval": interval, "outcome": outcome,
            "ret": (eval_price - price) / price * sign * 100,
        })
        matched += 1

    rng = random.Random(seed)
    n_samples = min(samples, max(500, len(rows) * 2))
    keys = list(interval_counts)
    weights = [interval_counts[k] for k in keys]
    control_rets: list[float] = []
    control_outcomes: list[str] = []
    if keys and n_samples:
        picks = rng.choices(keys, weights=weights, k=n_samples)
        cache: dict[tuple, list[tuple]] = {}
        for symbol, interval in picks:
            if (symbol, interval) not in cache:
                cur.execute(
                    "SELECT open_time, open, high, low, close FROM candle_archive "
                    "WHERE symbol=%s AND `interval`=%s AND open_time>=%s "
                    "ORDER BY open_time",
                    (symbol, interval, since))
                cache[(symbol, interval)] = [
                    (int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]))
                    for r in cur.fetchall()]
            bars = cache[(symbol, interval)]
            iv_ms = INTERVAL_MS.get(interval)
            if not iv_ms or len(bars) < 15:
                continue
            needed = max(3, int(eval_minutes * 60_000 / iv_ms))
            i = rng.randint(3, len(bars) - needed - 2)
            price = bars[i][4]
            window = [(b[2], b[3], b[4]) for b in bars[i + 1:i + 1 + needed]]
            direction = "bullish" if rng.random() < 0.5 else "bearish"
            result, last_close = judge(price, direction, window, threshold)
            if result is None:
                continue
            sign = 1 if direction == "bullish" else -1
            control_outcomes.append(result)
            control_rets.append((last_close - price) / price * sign * 100)

    def group(keyfn, bins=None):
        groups: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            groups[keyfn(r, bins)].append(r)
        return {k: summarize(v, control_rets) for k, v in sorted(groups.items())}

    by_score = group(lambda r, b: bin_label(r["score"], b), SCORE_BINS)
    by_gap = group(lambda r, b: bin_label(r["gap"], b), GAP_BINS)
    by_status = group(lambda r, b: r["status"] or "unknown")
    by_regime = group(lambda r, b: r["regime"] or "unknown")

    scan = []
    for t in range(40, 81, 5):
        above = [r for r in rows if r["score"] >= t]
        if len(above) < max(30, len(rows) * 0.08):
            continue
        s = summarize(above, control_rets)
        scan.append({"threshold": t, "n": s["n"], "winrate": s["winrate"],
                     "lift": s["lift"]})
    scan.sort(key=lambda x: -x["lift"])

    control_judged = [o for o in control_outcomes if o in ("win", "loss")]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days": days, "threshold_pct": threshold * 100,
        "eval_minutes": eval_minutes,
        "traces_with_score": len(traces), "matched": matched,
        "all": summarize(rows, control_rets),
        "control": {
            "n": len(control_rets),
            "judged": len(control_judged),
            "wins": sum(1 for o in control_judged if o == "win"),
            "winrate": round(sum(1 for o in control_judged if o == "win")
                             / len(control_judged), 4) if control_judged else 0.0,
            "avgRet": round(sum(control_rets) / len(control_rets), 4)
            if control_rets else 0.0,
        },
        "byScore": by_score, "byGap": by_gap,
        "byStatus": by_status, "byRegime": by_regime,
        "scan": scan[:6],
        "current": next((s for s in scan if s["threshold"] == 55), None),
    }


def render(d: dict) -> None:
    c, a = d["control"], d["all"]
    print(f"\n=== 信号 lift 对照（近 {d['days']} 天，{d['generated_at'][:19]} UTC）===")
    print(f"口径：{d['eval_minutes']} 分钟窗口 / ±{d['threshold_pct']:.2f}% 先触及；"
          f"ret = 窗口末收盘的方向收益（百分点）\n")
    print(f"[0] 样本：带评分决策 {d['traces_with_score']} 条，"
          f"成功关联命中判定 {d['matched']} 条；随机基线 {c['n']} 条")
    print(f"    随机入场基线     胜率 {c['winrate']:.1%}  平均 ret {c['avgRet']:+.4f}")
    print(f"    全部形态信号     胜率 {a['winrate']:.1%}  平均 ret {a['avgRet']:+.4f}  "
          f"lift {a['lift']:+.4f}")

    print("\n[1] 按共鸣分分箱（检验 55 分线）")
    print(f"    {'分数区间':<12}{'样本':>7}{'已判':>7}{'胜率':>9}{'avgRet':>10}{'lift':>10}")
    for k, v in d["byScore"].items():
        print(f"    {k:<12}{v['n']:>7}{v['judged']:>7}{v['winrate']:>8.1%}"
              f"{v['avgRet']:>10.4f}{v['lift']:>+10.4f}")

    low = [v for k, v in d["byScore"].items() if k in ("0-45", "45-55")]
    high = [v for k, v in d["byScore"].items() if k in ("55-65", "65-75", "75-101")]
    ln = sum(v["judged"] for v in low)
    hn = sum(v["judged"] for v in high)
    lw = sum(v["wins"] for v in low)
    hw = sum(v["wins"] for v in high)
    if ln and hn:
        lwr, hwr = lw / ln, hw / hn
        delta = hwr - lwr
        print(f"\n    <55 胜率 {lwr:.1%} (n={ln})   >=55 胜率 {hwr:.1%} (n={hn})"
              f"   差 {delta:+.1%}")
        if delta > 0.03 and hn >= 100:
            print("    -> 55 分线有区分力，阈值可保留")
        elif abs(delta) <= 0.03:
            print("    -> 未发现区分力：当前 55 分阈值缺乏实证支持")
        else:
            print("    -> 高分组反而更差：评分在这批样本上呈反向。"
                  f"样本仅 {d['matched']} 条 / {d['days']} 天，"
                  "勿据此直接改生产阈值；请拉长窗口复核或先走影子对照")

    print("\n[2] 按分离度 gap 分箱（检验 15 分线）")
    for k, v in d["byGap"].items():
        print(f"    {k:<12}{v['n']:>7} 笔  胜率 {v['winrate']:>7.1%}  "
              f"lift {v['lift']:+.4f}")

    print("\n[3] 放行 vs 拦截（拦截组的胜率 = 误杀率）")
    for k, v in d["byStatus"].items():
        print(f"    {k:<14}{v['n']:>7} 笔  胜率 {v['winrate']:>7.1%}  "
              f"lift {v['lift']:+.4f}")

    print("\n[4] 按 regime 分层")
    for k, v in d["byRegime"].items():
        print(f"    {k:<14}{v['n']:>7} 笔  胜率 {v['winrate']:>7.1%}  "
              f"lift {v['lift']:+.4f}")

    print("\n[5] 阈值扫描（按 lift 排序，只列样本足够的候选）")
    for s in d["scan"]:
        mark = "  <- 当前" if s["threshold"] == 55 else ""
        print(f"    >= {s['threshold']:<3}  n={s['n']:<6} 胜率 {s['winrate']:.1%}"
              f"  lift {s['lift']:+.4f}{mark}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description="只读信号 lift 对照分析")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--samples", type=int, default=4000, help="随机基线抽样数上限")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    path = Path(args.config)
    if not path.exists():
        sys.exit(f"找不到配置文件：{path}")
    eval_minutes, threshold = load_params(path)
    conn = pymysql.connect(**db_config(path))
    try:
        data = collect(conn, max(1, args.days), args.samples, args.seed,
                       eval_minutes, threshold)
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
