#!/usr/bin/env python3
"""把历史 hits 补进 signal_features（信号实时特征永久表）。

幂等：按 (symbol, interval, mode, bar_open_time, pattern, direction) 去重，
重复运行不会插入第二份。归档 K 线不足 30 根时仍写入"信号事实"，
特征字段留空 —— 这样回放至少不再需要前置K线窗口。

用法：
    python scripts/backfill_features.py --config /opt/kline-monitor/config.yaml
"""
from __future__ import annotations

import argparse
import bisect
import json
import sys
import time
from collections import defaultdict
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
from decision.quality import noise_ratio  # noqa: E402
from strategy_report import db_config  # noqa: E402

MATCH_MS = 60_000
COLS = ("ts,symbol,`interval`,pattern,direction,mode,price,bar_open_time,"
        "regime,adx,atr,atr_pct,vol_state,rsi,zscore,squeeze,noise_ratio,"
        "score,gap,grade,confidence,quality_allowed,quality_size_factor,"
        "quality_reason")
PLACEHOLDERS = ",".join(["%s"] * 24)


def main() -> None:
    ap = argparse.ArgumentParser(description="回填历史信号特征（幂等）")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--days", type=int, default=0, help="只回填最近 N 天，0=全部")
    args = ap.parse_args()
    path = Path(args.config)
    if not path.exists():
        sys.exit(f"找不到配置文件：{path}")
    conn = pymysql.connect(**db_config(path))
    cur = conn.cursor()

    try:
        cur.execute("SELECT symbol,`interval`,mode,bar_open_time,pattern,direction "
                    "FROM signal_features")
        existing = {(r[0], r[1], r[2], int(r[3] or 0), r[4], r[5])
                    for r in cur.fetchall()}
    except Exception as ex:
        sys.exit(f"signal_features 表不可用（先部署一次让服务建表）: {ex}")

    since = int(time.time() * 1000) - args.days * 86_400_000 if args.days else 0
    cur.execute(
        "SELECT ts,symbol,`interval`,pattern,direction,mode,price,bar_open_time "
        "FROM hits WHERE mode='confirmed' AND direction IN ('bullish','bearish') "
        "AND ts>=%s ORDER BY ts", (since,))
    hits = cur.fetchall()

    cur.execute("SELECT ts,symbol,payload FROM decision_traces WHERE kind='trace'")
    scored: dict[tuple, list[tuple]] = defaultdict(list)
    for ts, sym, payload in cur.fetchall():
        try:
            p = json.loads(payload) if isinstance(payload, str) else (payload or {})
            so = p.get("score")
            if not isinstance(so, dict) or so.get("score") is None:
                continue
            scored[(sym, p.get("pattern") or "", p.get("direction"))].append(
                (int(ts), float(so["score"]), float(so.get("gap") or 0),
                 p.get("grade"), so.get("confidence")))
        except (ValueError, TypeError, AttributeError):
            continue
    for key in scored:
        scored[key].sort()

    cache: dict[tuple, list[tuple]] = {}
    cache_times: dict[tuple, list[int]] = {}

    def bars_for(sym, iv):
        key = (sym, iv)
        if key not in cache:
            cur.execute(
                "SELECT open_time,open,high,low,close FROM candle_archive "
                "WHERE symbol=%s AND `interval`=%s ORDER BY open_time", (sym, iv))
            cache[key] = [(int(r[0]), float(r[1]), float(r[2]),
                           float(r[3]), float(r[4])) for r in cur.fetchall()]
            cache_times[key] = [b[0] for b in cache[key]]
        return cache[key], cache_times[key]

    inserted = skipped = 0
    for ts, sym, iv, pattern, direction, mode, price, bar_open in hits:
        key = (sym, iv, mode, int(bar_open or 0), pattern, direction)
        if key in existing:
            skipped += 1
            continue
        regime = adx = atr = atr_pct = vol_state = rsi = z = squeeze = nr = None
        bars, times = bars_for(sym, iv)
        idx = bisect.bisect_left(times, int(bar_open or 0))
        if 31 <= idx < len(bars):
            window = bars[idx - 30:idx + 1]
            objs = [SimpleNamespace(open_time=b[0], open=b[1], high=b[2],
                                    low=b[3], close=b[4], volume=0.0)
                    for b in window]
            try:
                st = IND.market_state(objs)
                regime, adx, atr = st.get("regime"), st.get("adx"), st.get("atr")
                atr_pct, vol_state = st.get("atrPct"), st.get("volState")
                rsi, z, squeeze = st.get("rsi"), st.get("z"), st.get("squeeze")
                nr = noise_ratio(objs)
            except Exception:
                pass

        score = gap = grade = confidence = None
        candidates = scored.get((sym, pattern, direction), [])
        if candidates:
            ctimes = [c[0] for c in candidates]
            j = bisect.bisect_left(ctimes, int(ts))
            best = None
            for k in (j - 1, j, j + 1):
                if 0 <= k < len(candidates):
                    delta = abs(candidates[k][0] - int(ts))
                    if delta <= MATCH_MS and (best is None or delta < best[0]):
                        best = (delta, candidates[k])
            if best:
                score, gap, grade, confidence = (best[1][1], best[1][2],
                                                  best[1][3], best[1][4])
        cur.execute(
            f"INSERT INTO signal_features({COLS}) VALUES({PLACEHOLDERS})",
            (int(ts), sym, iv, pattern, direction, mode,
             float(price) if price else None, int(bar_open or 0),
             regime, adx, atr, atr_pct, vol_state, rsi, z,
             None if squeeze is None else int(bool(squeeze)), nr,
             score, gap, grade, confidence, None, None, None))
        existing.add(key)
        inserted += 1
    conn.commit()
    print(f"回填完成：命中 {len(hits)} 条，新写入 {inserted} 条，"
          f"已存在跳过 {skipped} 条")
    cur.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM signal_features")
    print("signal_features 当前：", cur.fetchone())
    conn.close()


if __name__ == "__main__":
    main()
