#!/usr/bin/env python3
"""策略自优化：找出真正能赚钱的形态组合与止损止盈参数。

## 为什么必须做这个

9 年回测（4806 笔）结论：**全形态放一起是系统性亏损**——
胜率 35.2%、盈亏比 0.83、期望 -0.64/笔、最大回撤 -31%。
而且三段（早期/中期/近期）全是负的，说明不是某段行情不好，是配置本身错。

数学上很清楚：胜率 35% 时，盈亏比必须 > 1/0.35 - 1 = **1.86** 才不亏，
而当前配置（SL=1.5×ATR、TP=4.5×ATR）实际只有 1.54 左右
（因为止损经常被跳空/移动止损提前打掉，而止盈很少完整吃到）。

## 这个脚本做什么

1. 按形态筛：只保留在**训练段**里单独盈利的形态
2. 网格搜 SL/TP 倍数，在**训练段**里挑期望最高的组合
3. 用**验证段**（训练段之后的时间）检验——这是防过拟合的关键：
   训练好看、验证难看 = 曲线拟合，不能用

## 输出

一条可直接落进 config.yaml 的配置建议，但**只在验证段也为正时**才给。
"""
import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import secrets_store  # noqa: E402
import yaml  # noqa: E402

import patterns.builtin  # noqa: F401  导入即注册
import patterns.structure  # noqa: F401
from core.candle import Candle  # noqa: E402
from decision.backtest import run_backtest  # noqa: E402
from decision.config import DecisionConfig  # noqa: E402
from patterns.base import all_patterns  # noqa: E402
from storage import Storage  # noqa: E402


def ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def load(store: Storage, symbol: str, interval: str) -> list[Candle]:
    # 分页读全量，避免短周期历史被单次 LIMIT 截断
    rows = store.candle_history_all(symbol, interval, 0, 2**62)
    out = [Candle(open_time=int(r["t"]), open=float(r["o"]), high=float(r["h"]),
                  low=float(r["l"]), close=float(r["c"]), volume=float(r["v"]),
                  trades=int(r["trades"] or 0), interval=interval, closed=True)
           for r in rows]
    out.sort(key=lambda c: c.open_time)
    return out


def cfg_with(sl: float, tp: float) -> DecisionConfig:
    base = DecisionConfig()
    return DecisionConfig(
        **{**base.__dict__, "watch_sl_atr_mult": sl, "watch_tp_atr_mult": tp})


def summarize(res) -> dict:
    m = res.metrics
    return {
        "trades": m.get("trades", 0),
        "winRate": m.get("winRate", 0),
        "pnl": m.get("pnl", 0),
        "expectancy": m.get("expectancy", 0),
        "profitFactor": m.get("profitFactor"),
        "maxDD": m.get("maxDrawdownPct", 0),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="btcusdt")
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--config", default="/opt/kline-monitor/config.yaml")
    ap.add_argument("--train", type=float, default=0.7,
                    help="训练段占比（其余为验证段）")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    try:
        cfg = secrets_store.decrypt_config(cfg, args.config)
    except Exception:
        pass
    store = Storage(cfg.get("mysql") or {})
    bars = load(store, args.symbol, args.interval)
    if len(bars) < 2000:
        print("数据不足")
        return 1

    cut = int(len(bars) * args.train)
    train, valid = bars[:cut], bars[cut:]
    print(f"{args.symbol} {args.interval} 共 {len(bars)} 根")
    print(f"  训练 {ts(train[0].open_time)} .. {ts(train[-1].open_time)}（{len(train)} 根）")
    print(f"  验证 {ts(valid[0].open_time)} .. {ts(valid[-1].open_time)}（{len(valid)} 根）")

    pats = list(all_patterns().values())
    base = DecisionConfig()

    # ---- 1) 训练段：每个形态单独跑，留下正期望的 ----
    print("\n=== 形态筛选（训练段，单形态独立回测）")
    keep = []
    for p in pats:
        res = run_backtest(train, [p], base, symbol=args.symbol)
        s = summarize(res)
        if s["trades"] >= 30 and s["expectancy"] > 0:
            keep.append(p.key)
            print(f"  ✓ {p.key:24s} {s['trades']:>4d} 笔  胜率 {s['winRate']*100:5.1f}%  "
                  f"期望 {s['expectancy']:+.3f}")
        elif s["trades"]:
            print(f"  ✗ {p.key:24s} {s['trades']:>4d} 笔  胜率 {s['winRate']*100:5.1f}%  "
                  f"期望 {s['expectancy']:+.3f}")
    if not keep:
        print("\n训练段没有任何形态是正期望——这套形态库在 1h 上不可用。")
        return 2
    print(f"\n保留 {len(keep)} 个: {', '.join(keep)}")

    kept = [p for p in pats if p.key in keep]

    # ---- 2) 训练段：网格搜 SL/TP ----
    print("\n=== 参数搜索（训练段，只用保留的形态）")
    best = None
    for sl in (1.0, 1.5, 2.0, 2.5):
        for tp in (2.0, 3.0, 4.5, 6.0, 8.0):
            res = run_backtest(train, kept, cfg_with(sl, tp), symbol=args.symbol)
            s = summarize(res)
            if s["trades"] < 100:
                continue
            flag = ""
            if best is None or s["expectancy"] > best[2]["expectancy"]:
                best = (sl, tp, s)
                flag = " ←"
            print(f"  SL {sl:>3.1f}×ATR  TP {tp:>3.1f}×ATR  "
                  f"{s['trades']:>4d} 笔  胜率 {s['winRate']*100:5.1f}%  "
                  f"期望 {s['expectancy']:+.3f}  盈亏比 {s['profitFactor'] or 0:.2f}"
                  f"  回撤 {s['maxDD']:.1f}%{flag}")
    if best is None:
        print("没有任何参数组合产出足够样本")
        return 3
    sl, tp, ts_ = best
    print(f"\n训练段最优: SL {sl}×ATR / TP {tp}×ATR  期望 {ts_['expectancy']:+.3f}")

    # ---- 3) 验证段：这才是能不能用的判据 ----
    print("\n=== 验证段（训练段之后的时间，未参与调参）")
    vres = run_backtest(valid, kept, cfg_with(sl, tp), symbol=args.symbol)
    vs = summarize(vres)
    print(f"  {vs['trades']} 笔  胜率 {vs['winRate']*100:.1f}%  "
          f"期望 {vs['expectancy']:+.3f}  盈亏比 {vs['profitFactor'] or 0:.2f}  "
          f"回撤 {vs['maxDD']:.1f}%  总盈亏 {vs['pnl']:+.2f}")

    if vs["expectancy"] > 0 and vs["trades"] >= 30:
        print("\n✅ 验证段也是正期望 → 这组配置可以上线")
        print(f"   patterns: {keep}")
        print(f"   watch_sl_atr_mult: {sl}")
        print(f"   watch_tp_atr_mult: {tp}")
        # 同时给全区间成绩，作为参考
        full = run_backtest(bars, kept, cfg_with(sl, tp), symbol=args.symbol)
        fs = summarize(full)
        print(f"   全区间: {fs['trades']} 笔  期望 {fs['expectancy']:+.3f}  "
              f"盈亏 {fs['pnl']:+.2f}  回撤 {fs['maxDD']:.1f}%")
    else:
        print("\n❌ 验证段不赚钱 → 训练段的好看是过拟合，不能上线")
        print("   下一步该做的：换周期（3m/15m）、换形态库，或加趋势过滤，")
        print("   而不是继续在这个组合上拧参数。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
