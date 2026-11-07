#!/usr/bin/env python3
"""跨周期研究：换周期 / 趋势过滤 / 手续费敏感度，全部带样本外验证。

## 背景

9 年 1h 全形态基线（scripts/backtest_history.py）：
    4806 笔、胜率 35.2%、盈亏比 0.83、期望 -0.642/笔、三段全负。
训练段选出的 4 个正期望形态在验证段不赚钱（scripts/optimize_strategy.py）。

用户提出的三个方向，前两个可以在同一套回测引擎上用数据直接判：
  1. 换周期：1h 上 ATR 相对波动太小，双边手续费吃掉边际；3m/15m 才是实盘周期。
  2. 加趋势过滤：赚钱的 4 个形态都是收敛/突破类，只有趋势启动才有效；
     现在无条件触发，大量单子开在震荡里被扫。

## 严谨性约束

- 每个周期取**同样根数**的最近历史（--bars），避免样本量差异伪装成周期效应。
- 训练段在前、验证段在后；过滤器阈值只在训练段比较，验证段是最终判据。
- 手续费敏感度单列（0 / 4 / 8 bps），把「信号没 edge」和「手续费吃掉 edge」分开。
- 报告显式写出 20 根持仓在各周期对应多少小时：这是跨周期比较的混淆变量，
  可用 --hold-hours 改成固定墙钟时间再跑一遍。

## 用法

    venv/bin/python scripts/interval_study.py --intervals 1h,15m,3m --bars 120000
    venv/bin/python scripts/interval_study.py --intervals 3m --bars 200000 \
        --hold-hours 4 --json /tmp/interval_study.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import secrets_store  # noqa: E402
import yaml  # noqa: E402

import patterns.builtin  # noqa: F401  导入即注册
import patterns.structure  # noqa: F401
from core.candle import Candle  # noqa: E402
from decision.backtest import run_backtest  # noqa: E402
from decision.config import DecisionConfig  # noqa: E402
from decision.features import atr_series  # noqa: E402
from feed.history import IV_MS  # noqa: E402
from patterns.base import all_patterns  # noqa: E402
from storage import Storage  # noqa: E402

# 1h 训练段里单独为正的 4 个形态，都是收敛/突破类
BREAKOUT_KEYS = ("rising_wedge", "double_bottom", "morning_star",
                 "descending_triangle")


def ts(ms: int) -> str:
    if not ms:
        return "-"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def load(store: Storage, symbol: str, interval: str, max_bars: int) -> list[Candle]:
    # 分页读：3m 九年 ≈158 万根，candle_history_range 单次上限 50 万
    rows = store.candle_history_all(symbol, interval, 0, 2**62)
    if max_bars > 0 and len(rows) > max_bars:
        rows = rows[-max_bars:]
    bars = [Candle(open_time=int(r["t"]), open=float(r["o"]), high=float(r["h"]),
                   low=float(r["l"]), close=float(r["c"]), volume=float(r["v"]),
                   trades=int(r["trades"] or 0), interval=interval, closed=True,
                   buy_vol=float(r.get("buyVol") or 0),
                   sell_vol=float(r.get("sellVol") or 0))
            for r in rows]
    bars.sort(key=lambda c: c.open_time)
    return bars


def atr_profile(bars: list[Candle], samples: int = 1500) -> dict:
    """采样算 ATR(14)/close 的分布，用于比较各周期的相对波动。"""
    step = max(1, len(bars) // samples)
    vals = []
    for i in range(30, len(bars), step):
        w = bars[i - 30:i + 1]
        a = atr_series(w, 14)
        c = float(bars[i].close)
        if a and a[-1] > 0 and c > 0:
            vals.append(a[-1] / c)
    vals.sort()
    if not vals:
        return {"median": 0.0, "p25": 0.0, "p75": 0.0, "n": 0}
    return {"median": round(vals[len(vals) // 2], 6),
            "p25": round(vals[len(vals) // 4], 6),
            "p75": round(vals[len(vals) * 3 // 4], 6),
            "n": len(vals)}


def summarize(res) -> dict:
    m = res.metrics
    return {
        "trades": int(m.get("trades", 0)),
        "winRate": round(float(m.get("winRate", 0)), 4),
        "expectancy": round(float(m.get("expectancy", 0)), 4),
        "pnl": round(float(m.get("pnl", 0)), 2),
        "profitFactor": m.get("profitFactor"),
        "maxDD": round(float(m.get("maxDrawdownPct", 0)), 2),
    }


def make_filter(kind: str, thr: float):
    """返回 run_backtest 的 signal_filter。

    dev = (close - SMA50) / ATR(14)，即"离均线多少个 ATR"。
    trend_*   要求偏离方向与形态一致且足够大（趋势启动）
    range_*   要求贴近均线（对照组：只在震荡里做）
    breakout  要求收盘突破前 20 根高低点
    """
    def filt(window, i, pat):
        n = len(window)
        if n < 55:
            return True                     # warmup 不足不拦，避免样本缺失
        closes = [float(b.close) for b in window]
        a = atr_series(window, 14)
        if not a or not a[-1] or a[-1] <= 0:
            return True
        dev = (closes[-1] - sum(closes[-50:]) / 50) / a[-1]
        sign = 1 if pat.direction == "bullish" else -1
        if kind == "trend":
            return dev * sign >= thr
        if kind == "range":
            return abs(dev) < thr
        if kind == "breakout":
            seg = window[-21:-1]
            if pat.direction == "bullish":
                return closes[-1] > max(float(b.high) for b in seg)
            return closes[-1] < min(float(b.low) for b in seg)
        return True

    return filt


def hold_bars(interval: str, hours: float) -> int:
    """把固定墙钟持仓时间换算成该周期的 K 线根数。"""
    if hours <= 0:
        return 20
    return max(2, int(hours * 3_600_000 / IV_MS[interval]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--intervals", default="1h,15m,3m")
    ap.add_argument("--symbol", default="btcusdt")
    ap.add_argument("--bars", type=int, default=120_000,
                    help="每个周期取最近多少根；0=全量")
    ap.add_argument("--train", type=float, default=0.7)
    ap.add_argument("--hold-hours", type=float, default=0,
                    help="固定持仓时长（小时）；0=引擎默认 20 根")
    ap.add_argument("--fee-grid", default="0,4,8")
    ap.add_argument("--config", default="/opt/kline-monitor/config.yaml")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    try:
        cfg = secrets_store.decrypt_config(cfg, args.config)
    except Exception:
        pass
    store = Storage(cfg.get("mysql") or {})
    pats = list(all_patterns().values())
    pats4 = [p for p in pats if p.key in BREAKOUT_KEYS]
    dcfg = DecisionConfig()
    fees = [float(x) for x in args.fee_grid.split(",")]
    variants = [
        ("none", 0.0, None),
        ("trend>=1", 1.0, make_filter("trend", 1.0)),
        ("trend>=2", 2.0, make_filter("trend", 2.0)),
        ("range<1", 1.0, make_filter("range", 1.0)),
        ("breakout20", 0.0, make_filter("breakout", 0.0)),
    ]
    report: dict = {"generatedAt": datetime.now(timezone.utc).isoformat(),
                    "symbol": args.symbol, "bars": args.bars,
                    "holdHours": args.hold_hours, "train": args.train,
                    "intervals": {}}

    for iv in [x.strip() for x in args.intervals.split(",") if x.strip()]:
        if iv not in IV_MS:
            print(f"跳过未知周期 {iv}")
            continue
        t0 = time.time()
        bars = load(store, args.symbol, iv, args.bars)
        if len(bars) < 2000:
            print(f"\n=== {iv}: 只有 {len(bars)} 根，先跑 import_history.py 导入")
            continue
        hb = hold_bars(iv, args.hold_hours)
        profile = atr_profile(bars)
        fee_ratio = (2 * 4 / 10_000 / profile["median"]) if profile["median"] else 0
        print(f"\n{'=' * 72}")
        print(f"=== {args.symbol} {iv}: {len(bars)} 根  "
              f"{ts(bars[0].open_time)} .. {ts(bars[-1].open_time)}")
        print(f"    ATR/价格 中位 {profile['median']*100:.4f}%  "
              f"p25 {profile['p25']*100:.4f}%  p75 {profile['p75']*100:.4f}%")
        print(f"    双边 4bps 手续费 ≈ ATR 的 {fee_ratio*100:.2f}%  "
              f"| 20 根持仓 = {20*IV_MS[iv]/3_600_000:.1f} 小时，"
              f"本次用 {hb} 根 = {hb*IV_MS[iv]/3_600_000:.1f} 小时")
        entry: dict = {"rows": len(bars), "from": ts(bars[0].open_time),
                       "to": ts(bars[-1].open_time), "atrProfile": profile,
                       "feeVsAtr": round(fee_ratio, 4),
                       "holdBars": hb, "fees": {}, "filters": {}}

        # ---- A) 全形态基线 + 手续费敏感度 ----
        print(f"\n  [A] 全形态基线 / 手续费敏感度（{len(pats)} 个形态）")
        for fee in fees:
            r = run_backtest(bars, pats, dcfg, symbol=args.symbol,
                             fee_bps=fee, max_hold_bars=hb)
            s = summarize(r)
            entry["fees"][str(fee)] = s
            print(f"    fee {fee:>4.1f}bps  {s['trades']:>6d} 笔  "
                  f"胜率 {s['winRate']*100:5.1f}%  期望 {s['expectancy']:+8.4f}  "
                  f"盈亏比 {(s['profitFactor'] or 0):.2f}", flush=True)

        # ---- B) 4 个收敛/突破形态 × 趋势过滤，训练段选、验证段判 ----
        cut = int(len(bars) * args.train)
        train, valid = bars[:cut], bars[cut:]
        print(f"\n  [B] 收敛/突破 {len(pats4)} 形态 × 过滤（训练 {len(train)} / "
              f"验证 {len(valid)} 根，验证段是最终判据）")
        print(f"    {'变体':<14}{'训练n':>7}{'训练E':>10}{'验证n':>7}{'验证E':>10}"
              f"{'验证胜率':>10}")
        for label, thr, fn in variants:
            rt = run_backtest(train, pats4, dcfg, symbol=args.symbol,
                              max_hold_bars=hb, signal_filter=fn)
            rv = run_backtest(valid, pats4, dcfg, symbol=args.symbol,
                              max_hold_bars=hb, signal_filter=fn)
            st, sv = summarize(rt), summarize(rv)
            entry["filters"][label] = {"threshold": thr, "train": st, "valid": sv,
                                       "sampleOutPositive": sv["expectancy"] > 0
                                       and sv["trades"] >= 30}
            print(f"    {label:<14}{st['trades']:>7}{st['expectancy']:>+10.4f}"
                  f"{sv['trades']:>7}{sv['expectancy']:>+10.4f}"
                  f"{sv['winRate']*100:>9.1f}%", flush=True)

        winners = [k for k, v in entry["filters"].items()
                   if v["sampleOutPositive"]]
        print(f"    样本外为正的变体: {', '.join(winners) if winners else '无'}")
        entry["elapsedSec"] = round(time.time() - t0, 1)
        report["intervals"][iv] = entry

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\nJSON -> {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
