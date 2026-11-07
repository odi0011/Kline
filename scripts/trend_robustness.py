#!/usr/bin/env python3
"""对 interval_study 发现的趋势过滤做稳健性复核，防止把偶然当 edge。

1h 九年初次结果：收敛/突破 4 形态 + trend>=1 过滤，训练段期望 +0.35/笔
(n=235)，验证段 +0.81/笔 (n=116)。但验证样本只有 116 笔，且阈值是从
少数几个候选里挑的 —— 必须先回答四个问题再谈上线：

  1. 阈值邻域：1.0 是真的平台区，还是孤立的尖峰？
  2. 切分稳健性：换训练/验证比例，正向结论还在吗？
  3. 三折时间切片：是不是只靠某一段行情？
  4. 手续费与收益集中度：扣费后还剩多少？是不是靠一两笔大赢？

任何一项不过关，结论都是"观察到了，但不足以改生产"。

口径说明：本脚本用回测引擎默认的 ATR 止损（不夹紧）。1h 上 ATR/价格
中位 0.79%，生产的 0.4% 下限影响有限；若要严格对齐生产规则，用
interval_study.py --sl-min-pct 0.004 --min-rr 3.0 复核短周期。

用法：
    venv/bin/python scripts/trend_robustness.py --interval 1h
    ... --interval 15m --json /tmp/trend_15m.json
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import secrets_store  # noqa: E402
import yaml  # noqa: E402

from decision.backtest import run_backtest  # noqa: E402
from decision.config import DecisionConfig  # noqa: E402
from patterns.base import all_patterns  # noqa: E402
from storage import Storage  # noqa: E402

# 复用 interval_study 的加载与过滤实现，避免两份定义漂移
_spec = importlib.util.spec_from_file_location(
    "interval_study", Path(__file__).with_name("interval_study.py"))
IS = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(IS)
load, make_filter, summarize = IS.load, IS.make_filter, IS.summarize
BREAKOUT_KEYS = IS.BREAKOUT_KEYS


def exp(res) -> float:
    return summarize(res)["expectancy"]


def trimmed(res, drop: int) -> float:
    """去掉最赚钱的 drop 笔后，剩下交易的期望。"""
    pnls = sorted((t.pnl for t in res.trades), reverse=True)
    rest = pnls[drop:]
    return round(sum(rest) / len(rest), 4) if rest else 0.0


def bootstrap(res, iterations: int = 1000, seed: int = 7) -> tuple[float, float]:
    """有放回重采样，给验证段期望的 95% 区间。"""
    pnls = [t.pnl for t in res.trades]
    if not pnls:
        return (0.0, 0.0)
    rng = random.Random(seed)
    means = []
    for _ in range(iterations):
        means.append(sum(pnls[rng.randrange(len(pnls))] for _ in pnls) / len(pnls))
    means.sort()
    return (round(means[int(len(means) * 0.025)], 4),
            round(means[int(len(means) * 0.975)], 4))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--symbol", default="btcusdt")
    ap.add_argument("--bars", type=int, default=0, help="0=全量")
    ap.add_argument("--config", default="/opt/kline-monitor/config.yaml")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    try:
        cfg = secrets_store.decrypt_config(cfg, args.config)
    except Exception:
        pass
    store = Storage(cfg.get("mysql") or {})
    bars = load(store, args.symbol, args.interval, args.bars)
    if len(bars) < 5000:
        print("数据不足")
        return 1
    pats4 = [p for p in all_patterns().values() if p.key in BREAKOUT_KEYS]
    dcfg = DecisionConfig()
    print(f"{args.symbol} {args.interval}: {len(bars)} 根 "
          f"{IS.ts(bars[0].open_time)} .. {IS.ts(bars[-1].open_time)}")
    out: dict = {"interval": args.interval, "rows": len(bars),
                 "thresholdScan": [], "splits": [], "folds": [], "fees": [],
                 "concentration": {}}

    cut = int(len(bars) * 0.7)
    train, valid = bars[:cut], bars[cut:]

    # ---- 1) 阈值邻域：尖峰 = 过拟合，平台区 = 真效应 ----
    print("\n=== 1) 阈值邻域（训练段选、验证段判）")
    print(f"    {'阈值':<10}{'训练n':>7}{'训练E':>10}{'验证n':>7}{'验证E':>10}")
    for thr in (0.5, 0.75, 1.0, 1.25, 1.5):
        f = make_filter("trend", thr)
        rt = run_backtest(train, pats4, dcfg, signal_filter=f)
        rv = run_backtest(valid, pats4, dcfg, signal_filter=f)
        st, sv = summarize(rt), summarize(rv)
        out["thresholdScan"].append({"thr": thr, "train": st, "valid": sv})
        print(f"    {thr:<10.2f}{st['trades']:>7}{st['expectancy']:>+10.4f}"
              f"{sv['trades']:>7}{sv['expectancy']:>+10.4f}", flush=True)
    base = run_backtest(valid, pats4, dcfg)
    print(f"    {'无过滤':<10}{'':>7}{'':>10}{summarize(base)['trades']:>7}"
          f"{exp(base):>+10.4f}")

    # ---- 2) 切分稳健性：换训练比例，验证段是否仍为正 ----
    print("\n=== 2) 切分稳健性（阈值固定 1.0，只看验证段）")
    f = make_filter("trend", 1.0)
    for ratio in (0.5, 0.6, 0.7, 0.8):
        c = int(len(bars) * ratio)
        rv = run_backtest(bars[c:], pats4, dcfg, signal_filter=f)
        sv = summarize(rv)
        out["splits"].append({"trainRatio": ratio, "valid": sv})
        print(f"    训练 {ratio:.0%} → 验证 {sv['trades']:>5} 笔  "
              f"期望 {sv['expectancy']:+.4f}  胜率 {sv['winRate']*100:.1f}%",
              flush=True)

    # ---- 3) 三折时间切片 ----
    print("\n=== 3) 三折时间切片（阈值固定 1.0）")
    n = len(bars)
    for i, (a, b) in enumerate(((0, n // 3), (n // 3, 2 * n // 3),
                                (2 * n // 3, n))):
        rv = run_backtest(bars[a:b], pats4, dcfg, signal_filter=f)
        sv = summarize(rv)
        out["folds"].append({"fold": i + 1, "valid": sv})
        print(f"    第 {i+1} 折 {sv['trades']:>5} 笔  期望 {sv['expectancy']:+.4f}  "
              f"胜率 {sv['winRate']*100:.1f}%", flush=True)

    # ---- 4) 手续费 + 集中度 + bootstrap ----
    print("\n=== 4) 主验证段：手续费 / 收益集中度 / bootstrap")
    for fee in (0.0, 4.0, 8.0):
        rv = run_backtest(valid, pats4, dcfg, fee_bps=fee, signal_filter=f)
        sv = summarize(rv)
        out["fees"].append({"feeBps": fee, "valid": sv})
        print(f"    fee {fee:>4.1f}bps  {sv['trades']:>5} 笔  期望 {sv['expectancy']:+.4f}",
              flush=True)
    main_res = run_backtest(valid, pats4, dcfg, signal_filter=f)
    lo, hi = bootstrap(main_res)
    conc = {"n": len(main_res.trades), "expectancy": exp(main_res),
            "ci95": [lo, hi],
            "dropTop1": trimmed(main_res, 1), "dropTop3": trimmed(main_res, 3),
            "dropTop5": trimmed(main_res, 5)}
    out["concentration"] = conc
    print(f"    验证段 {conc['n']} 笔  期望 {conc['expectancy']:+.4f}  "
          f"bootstrap 95% CI [{lo:+.4f}, {hi:+.4f}]")
    print(f"    去掉最赚的 1/3/5 笔后期望: {conc['dropTop1']:+.4f} / "
          f"{conc['dropTop3']:+.4f} / {conc['dropTop5']:+.4f}")

    # ---- 判据 ----
    scan = {x["thr"]: x for x in out["thresholdScan"]}
    neighbours_positive = sum(
        1 for t in (0.75, 1.0, 1.25) if scan[t]["valid"]["expectancy"] > 0)
    splits_positive = sum(1 for x in out["splits"] if x["valid"]["expectancy"] > 0)
    folds_positive = sum(1 for x in out["folds"] if x["valid"]["expectancy"] > 0)
    fee_ok = all(x["valid"]["expectancy"] > 0 for x in out["fees"])
    conc_ok = conc["dropTop5"] > 0 and lo > 0
    passed = (neighbours_positive >= 2 and splits_positive >= 3
              and folds_positive >= 2 and fee_ok and conc_ok)
    print("\n=== 判据")
    print(f"    阈值邻域正: {neighbours_positive}/3 | 切分正: {splits_positive}/4 | "
          f"三折正: {folds_positive}/3 | 扣费全正: {fee_ok} | "
          f"集中度+CI 通过: {conc_ok}")
    print("    ✅ 稳健，值得做影子对照" if passed else
          "    ❌ 不够稳健，不要把趋势过滤直接写进生产参数")
    out["passed"] = passed
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=2)
        print(f"    JSON -> {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
