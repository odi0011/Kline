#!/usr/bin/env python3
"""用 candle_history（后复权、9 年）跑真实回测。

为什么要这个脚本：decision/backtest.py 早就写好了（T+1 开盘成交、
止损优先、跳空按开盘、双边手续费），但一直没接到任何入口，
等于没有回测能力。这里把它接到真实历史数据上。

用法：
    venv/bin/python scripts/backtest_history.py --symbol btcusdt --interval 1h
    venv/bin/python scripts/backtest_history.py --symbol btcusdt --interval 1h --split
"""
import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import secrets_store  # noqa: E402
import yaml  # noqa: E402

from core.candle import Candle  # noqa: E402
from decision.backtest import monte_carlo, run_backtest  # noqa: E402
from decision.config import DecisionConfig  # noqa: E402
from storage import Storage  # noqa: E402
from patterns.base import all_patterns  # noqa: E402


def _ts(ms: int) -> str:
    if not ms:
        return "-"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def load(store: Storage, symbol: str, interval: str) -> list[Candle]:
    rows = store.candle_history_range(symbol, interval, 0, 2**62, 500_000)
    out = []
    for r in rows:
        out.append(Candle(
            open_time=int(r["t"]), open=float(r["o"]), high=float(r["h"]),
            low=float(r["l"]), close=float(r["c"]), volume=float(r["v"]),
            trades=int(r["trades"] or 0), interval=interval, closed=True,
            buy_vol=float(r.get("buyVol") or 0),
            sell_vol=float(r.get("sellVol") or 0)))
    out.sort(key=lambda c: c.open_time)
    return out


def report(title: str, res, mc=None) -> None:
    m = res.metrics
    if not m.get("trades"):
        print(f"\n{title}: 0 笔交易")
        return
    print(f"\n--- {title}")
    print(f"  区间     {_ts(res.trades[0].entry_ts)} .. {_ts(res.trades[-1].exit_ts)}")
    print(f"  交易     {m['trades']} 笔（胜 {m['wins']} / 负 {m['losses']}）胜率 {m['winRate']*100:.1f}%")
    print(f"  盈亏     {m['pnl']:+.2f}  期望 {m['expectancy']:+.4f}/笔  "
          f"盈亏比 {m['profitFactor'] or 0:.2f}")
    print(f"  回撤     {m['maxDrawdownPct']:.2f}%  平均持仓 {m['avgHoldBars']:.1f} 根")
    if mc:
        print(f"  蒙特卡洛 5% 分位回撤 {mc.get('p05DrawdownPct')}%  "
              f"爆仓概率 {mc.get('ruinProbability')}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="btcusdt")
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--config", default="/opt/kline-monitor/config.yaml")
    ap.add_argument("--split", action="store_true",
                    help="按时间三折：早期/中期/近期分别回测，看策略是否稳定")
    ap.add_argument("--fee-bps", type=float, default=4.0)
    ap.add_argument("--slip-bps", type=float, default=1.0)
    args = ap.parse_args()

    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    try:
        cfg = secrets_store.decrypt_config(cfg, args.config)
    except Exception:
        pass
    store = Storage(cfg.get("mysql") or {})
    bars = load(store, args.symbol, args.interval)
    if len(bars) < 200:
        print(f"{args.symbol} {args.interval} 只有 {len(bars)} 根，先导入历史")
        return 1
    print(f"{args.symbol} {args.interval}: {len(bars)} 根  "
          f"{_ts(bars[0].open_time)} .. {_ts(bars[-1].open_time)}")

    pats = list(all_patterns().values())
    dcfg = DecisionConfig()

    # 全区间
    whole = run_backtest(bars, pats, dcfg, symbol=args.symbol,
                         fee_bps=args.fee_bps, slippage_bps=args.slip_bps)
    mc = monte_carlo([t.pnl for t in whole.trades], iterations=400) if whole.trades else None
    report("全区间", whole, mc)

    if args.split:
        n = len(bars)
        for name, a, b in (("早期", 0, n // 3), ("中期", n // 3, 2 * n // 3),
                           ("近期", 2 * n // 3, n)):
            chunk = bars[a:b]
            if len(chunk) < 200:
                continue
            res = run_backtest(chunk, pats, dcfg, symbol=args.symbol,
                               fee_bps=args.fee_bps, slippage_bps=args.slip_bps)
            report(f"{name}（{_ts(chunk[0].open_time)} 起）", res)

    # 按形态拆解：哪个形态真的赚钱，哪个是噪音
    if whole.trades:
        by_pat: dict[str, list] = {}
        for t in whole.trades:
            by_pat.setdefault(t.pattern, []).append(t)
        print("\n--- 按形态（只列 10 笔以上的）")
        rows = []
        for key, ts in by_pat.items():
            if len(ts) < 10:
                continue
            wins = sum(1 for t in ts if t.pnl > 0)
            rows.append((key, len(ts), wins / len(ts) * 100, sum(t.pnl for t in ts)))
        rows.sort(key=lambda r: -r[3])
        for key, n, wr, pnl in rows:
            print(f"  {key:24s} {n:>5d} 笔  胜率 {wr:5.1f}%  盈亏 {pnl:+9.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
