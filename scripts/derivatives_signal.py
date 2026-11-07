#!/usr/bin/env python3
"""方向 3 可行性：资金费率 / 多空比 / 持仓量 对后续收益有没有预测力。

## 为什么单独做这个

9 年 1h 纯 K 线形态的结论已经很清楚：全形态系统性亏损、训练段选出的
形态在验证段不赚钱。用户问"要不要考虑资金费率、多空比这类衍生品信号"。
在把任何新数据源接进生产之前，先回答一个更便宜的问题：
**这类数据本身带不带方向信息？**

## 数据（币安官方公开仓，免 key）

- 资金费率：data/futures/um/monthly/fundingRate/<SYM>/...，每 8h 一条
- 多空比/持仓量：data/futures/um/daily/metrics/<SYM>/...，5m 一条，
  字段含 count_long_short_ratio / sum_toptrader_long_short_ratio /
  sum_taker_long_short_vol_ratio / sum_open_interest

## 方法（防前视、防过拟合）

1. 每个特征只取"事件时刻之前最后一条"记录，绝不看未来。
2. 事件按 4 小时采样一根 1h K 线，减少重叠样本的自相关。
3. 标签是未来 4h / 24h 的简单收益（未扣手续费，先测方向信息）。
4. 全样本按时间前后半切开：前半找方向，后半只看方向是否保持。
5. 同时给相关系数（IC）与五分位分组收益——只看"最好一组"极易过拟合。

## 用法

    venv/bin/python scripts/derivatives_signal.py --config /opt/kline-monitor/config.yaml
    ... --metrics-from 2025-01-01 --json /tmp/derivatives.json
"""
from __future__ import annotations

import argparse
import bisect
import io
import json
import os
import statistics
import sys
import zipfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import secrets_store  # noqa: E402
import yaml  # noqa: E402

from core.candle import Candle  # noqa: E402
from storage import Storage  # noqa: E402

BASE = "https://data.binance.vision/data"
UA = "Mozilla/5.0 (compatible; kline-derivatives-probe/1.0)"
FEATURES = (
    "funding_rate",
    "count_long_short_ratio",
    "sum_toptrader_long_short_ratio",
    "sum_taker_long_short_vol_ratio",
    "open_interest_chg",
)


def _get(url: str, timeout: int = 30) -> bytes | None:
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        return None


def _csv(raw: bytes) -> list[str]:
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
        return z.read(z.namelist()[0]).decode("utf-8", "replace").splitlines()
    except (zipfile.BadZipFile, IndexError, KeyError):
        return []


def month_list(a: tuple[int, int], b: tuple[int, int]) -> list[str]:
    y, m = a
    out = []
    while (y, m) <= b:
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def funding_series(sym: str, months: list[str]) -> dict[str, list[tuple[int, float]]]:
    """返回 {'funding_rate': [(ts_ms, value), ...]}，按时间升序。"""
    s = sym.upper()
    rows: list[tuple[int, float]] = []
    for ym in months:
        raw = _get(f"{BASE}/futures/um/monthly/fundingRate/{s}/"
                   f"{s}-fundingRate-{ym}.zip")
        if not raw:
            continue
        for line in _csv(raw):
            p = line.split(",")
            if len(p) < 3:
                continue
            try:
                rows.append((int(p[0]), float(p[2])))
            except ValueError:
                continue
    rows.sort()
    return {"funding_rate": rows}


def metrics_series(sym: str, start: str, end: str) -> dict[str, list[tuple[int, float]]]:
    """下载日频 metrics，拆成多个特征时间序列。"""
    s = sym.upper()
    d0 = datetime.strptime(start, "%Y-%m-%d").date()
    d1 = datetime.strptime(end, "%Y-%m-%d").date()
    out: dict[str, list[tuple[int, float]]] = {k: [] for k in FEATURES if k != "funding_rate"}
    prev_oi: float | None = None
    day = d0
    while day <= d1:
        raw = _get(f"{BASE}/futures/um/daily/metrics/{s}/"
                   f"{s}-metrics-{day.isoformat()}.zip")
        if raw:
            for line in _csv(raw)[1:]:
                p = line.split(",")
                if len(p) < 8:
                    continue
                try:
                    dt = datetime.strptime(p[0], "%Y-%m-%d %H:%M:%S").replace(
                        tzinfo=timezone.utc)
                    ts = int(dt.timestamp() * 1000)
                    oi = float(p[2])
                except (ValueError, IndexError):
                    continue
                vals = {
                    "count_long_short_ratio": p[6],
                    "sum_toptrader_long_short_ratio": p[5],
                    "sum_taker_long_short_vol_ratio": p[7],
                }
                for k, v in vals.items():
                    try:
                        out[k].append((ts, float(v)))
                    except ValueError:
                        pass
                if prev_oi and prev_oi > 0:
                    out["open_interest_chg"].append((ts, oi / prev_oi - 1))
                prev_oi = oi
        day += timedelta(days=1)
    for k in out:
        out[k].sort()
    return out


def load_bars(store: Storage, sym: str, interval: str = "1h") -> list[Candle]:
    rows = store.candle_history_all(sym, interval, 0, 2**62)
    bars = [Candle(open_time=int(r["t"]), open=float(r["o"]), high=float(r["h"]),
                   low=float(r["l"]), close=float(r["c"]), volume=float(r["v"]),
                   interval=interval, closed=True) for r in rows]
    bars.sort(key=lambda c: c.open_time)
    return bars


def corr(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 10:
        return 0.0
    mx, my = statistics.mean(xs), statistics.mean(ys)
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (vx * vy) ** 0.5


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="btcusdt")
    ap.add_argument("--config", default="/opt/kline-monitor/config.yaml")
    ap.add_argument("--funding-from", default="2020-01")
    ap.add_argument("--metrics-from", default="2025-01-01")
    ap.add_argument("--metrics-to", default="")
    ap.add_argument("--horizons", default="4,24", help="前瞻小时数")
    ap.add_argument("--sample-hours", type=int, default=4, help="事件采样间隔（小时）")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    try:
        cfg = secrets_store.decrypt_config(cfg, args.config)
    except Exception:
        pass
    store = Storage(cfg.get("mysql") or {})
    bars = load_bars(store, args.symbol, "1h")
    if len(bars) < 1000:
        print("历史 K 线不足")
        return 1
    print(f"{args.symbol} 1h: {len(bars)} 根", flush=True)

    today = datetime.now(timezone.utc)
    months = month_list(tuple(int(x) for x in args.funding_from.split("-")),
                        (today.year, today.month))
    print(f"下载资金费率 {len(months)} 个月...", flush=True)
    series = funding_series(args.symbol, months)
    print(f"  funding_rate {len(series['funding_rate'])} 条", flush=True)

    m_start = args.metrics_from
    m_end = args.metrics_to or today.strftime("%Y-%m-%d")
    print(f"下载多空比日频 {m_start} .. {m_end}...", flush=True)
    series.update(metrics_series(args.symbol, m_start, m_end))
    for k, v in series.items():
        print(f"  {k:38s} {len(v):>7d} 条", flush=True)

    times = [b.open_time for b in bars]
    horizons = [int(x) for x in args.horizons.split(",")]
    step = max(1, args.sample_hours)
    events: list[dict] = []
    for i in range(40, len(bars) - max(horizons) - 1, step):
        t = bars[i].open_time
        c = float(bars[i].close)
        if c <= 0:
            continue
        row = {"ts": t}
        for k, seq in series.items():
            ks = [x[0] for x in seq]
            j = bisect.bisect_right(ks, t) - 1
            row[k] = seq[j][1] if j >= 0 else None
        for h in horizons:
            row[f"fwd{h}"] = float(bars[i + h].close) / c - 1
        # 衍生品数据 2020 年后才有；2017-2019 的事件全是 None，
        # 留着会把训练/验证切分点推到没有特征的年份上。
        if any(row[k] is not None for k in FEATURES):
            events.append(row)
    if len(events) < 200:
        print(f"可用事件只有 {len(events)} 个，样本不足")
        return 2
    print(f"\n事件 {len(events)} 个，采样间隔 {step} 小时；"
          f"每个特征按自己的非空样本前后半切训练/验证")
    print(f"整体 {datetime.fromtimestamp(events[0]['ts']/1000, timezone.utc):%Y-%m-%d} .. "
          f"{datetime.fromtimestamp(events[-1]['ts']/1000, timezone.utc):%Y-%m-%d}")

    report: dict = {"events": len(events), "features": {}}
    for k in FEATURES:
        span = [e["ts"] for e in events if e[k] is not None]
        if len(span) < 200:
            print(f"\n=== {k}: 样本不足 ({len(span)})，跳过")
            continue
        print(f"\n=== {k}  {len(span)} 个样本  "
              f"{datetime.fromtimestamp(span[0]/1000, timezone.utc):%Y-%m-%d} .. "
              f"{datetime.fromtimestamp(span[-1]/1000, timezone.utc):%Y-%m-%d}")
        entry: dict = {"span": [span[0], span[-1]], "n": len(span)}
        for h in horizons:
            label = f"fwd{h}"
            pairs = [(e[k], e[label]) for e in events if e[k] is not None]
            if len(pairs) < 200:
                print(f"  {label}: 样本不足 ({len(pairs)})")
                continue
            # 按该特征自己的非空样本做时间前后半切分，避免某特征只在
            # 近几年有数据时，训练段全是空值导致 IC 恒为 0 的假结论。
            cut = len(pairs) // 2
            ptr, pva = pairs[:cut], pairs[cut:]
            ic_all = corr([a for a, _ in pairs], [b for _, b in pairs])
            ic_tr = corr([a for a, _ in ptr], [b for _, b in ptr])
            ic_va = corr([a for a, _ in pva], [b for _, b in pva])
            # 训练段定方向，验证段看是否保持（这是唯一有意义的判据）
            direction = 1 if ic_tr >= 0 else -1
            hold = ic_va * direction
            qs = sorted(a for a, _ in pairs)
            edges = [qs[int(len(qs) * q / 5)] for q in (1, 2, 3, 4)]
            groups = []
            for g in range(5):
                lo = -float("inf") if g == 0 else edges[g - 1]
                hi = float("inf") if g == 4 else edges[g]
                vals = [b for a, b in pairs if lo <= a < hi or (g == 4 and a >= lo)]
                groups.append({"n": len(vals),
                               "meanPct": round(statistics.mean(vals) * 100, 4)
                               if vals else 0})
            print(f"  {label}: 全样本 IC {ic_all:+.4f} | 训练 {ic_tr:+.4f} | "
                  f"验证 {ic_va:+.4f} → 方向保持 {hold:+.4f}")
            print(f"    五分位前瞻收益(%): " + "  ".join(
                f"Q{i+1}={g['meanPct']:+.3f}(n={g['n']})" for i, g in enumerate(groups)))
            entry[label] = {"icAll": round(ic_all, 5), "icTrain": round(ic_tr, 5),
                            "icValid": round(ic_va, 5),
                            "directionHeld": round(hold, 5), "quintiles": groups}
        report["features"][k] = entry

    good = [(k, v) for k, v in report["features"].items()
            if any(isinstance(x, dict) and x.get("directionHeld", 0) > 0.05
                   for x in v.values())]
    print("\n=== 结论")
    if good:
        print("  样本外 IC 方向保持 >0.05 的特征：")
        for k, v in good:
            print(f"    {k}: " + ", ".join(
                f"{h} {x['directionHeld']:+.4f}" for h, x in v.items()
                if isinstance(x, dict)))
        print("  → 值得进一步做成信号，但需再扣手续费、接形态门、跑影子对照。")
    else:
        print("  没有任何特征在验证段保持住训练段的方向（IC>0.05）。")
        print("  → 单独看，资金费率/多空比没有可直接用的方向 edge；")
        print("    引入它们前应先解决形态侧的问题，别指望换个数据源就翻身。")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"  JSON -> {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
