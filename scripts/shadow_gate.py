#!/usr/bin/env python3
"""放行 / 拦截 并行影子对照：闸门到底有没有挑出好信号？

## 为什么单独做这个

`signal_lift.py` 用 decision_traces + hits 事后关联，只能覆盖写进流水的
信号，且 10 分钟 ±0.15% 的口径与生产（0.4% 止损 / rr3 / 60min 超时）
不一致；被质量门在入口挡掉的信号连 trace 都没有，样本天然偏向"放行的"。

这里换一个干净做法：**每个有质量门判定的 confirmed 信号，无论放行还是
拦截，都用完全相同的规则在真实 K 线上虚拟开一次仓**。两组唯一的差别
就是闸门的判定，于是期望差直接回答"闸门有没有区分力"。

## 口径（与生产对齐，且对两组完全一致）

- 信号来自 `signal_features`（永久保留，入口实时落库，覆盖所有 confirmed）
- 结算用 `candle_archive` 的 **1m** K 线，取 `open_time > signal.ts` 的第一根
  开盘入场（严格防前视），统一尺度避免 1m/3m 信号不可比
- 止损 0.4%、止盈 = 止损 × rr(3.0)、最多持有 60 根 1m
- 同根上下都触及判止损；跳空按开盘价成交
- 扣双边 4bps 手续费 + 1bp 滑点；名义仓位 1000 USDT
- 胜率用 `decision.metrics.decided_win_rate`，回撤用 `max_drawdown`

## 不改变任何真实下单行为

只读 signal_features / candle_archive，只写自己的 `shadow_gate_trades` 表
（不在 prune 名单里，永久保留）。可重复运行，默认跳过已结算，
`--refresh` 强制重算。

## 用法

    venv/bin/python scripts/shadow_gate.py --config /opt/kline-monitor/config.yaml
    ... --days 30 --json /tmp/shadow_gate.json
    ... --refresh        # 参数改了之后重算
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

import pymysql  # noqa: E402
import yaml  # noqa: E402

import secrets_store  # noqa: E402
from decision.metrics import decided_win_rate, equity_curve, max_drawdown  # noqa: E402

DDL = """CREATE TABLE IF NOT EXISTS shadow_gate_trades(
    feature_id BIGINT PRIMARY KEY,
    ts BIGINT NOT NULL,
    symbol VARCHAR(16), `interval` VARCHAR(8),
    pattern VARCHAR(40), direction VARCHAR(8),
    quality_allowed TINYINT, quality_reason VARCHAR(255),
    regime VARCHAR(12), score DOUBLE, gap DOUBLE, noise_ratio DOUBLE,
    entry DOUBLE, exit_price DOUBLE,
    outcome VARCHAR(12), ret_pct DOUBLE, pnl DOUBLE,
    hold_bars INT, bars_available INT,
    settled_at BIGINT,
    INDEX idx_sgt_ts(ts),
    INDEX idx_sgt_allowed(quality_allowed)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""

UPSERT = ("INSERT INTO shadow_gate_trades(feature_id,ts,symbol,`interval`,pattern,"
          "direction,quality_allowed,quality_reason,regime,score,gap,noise_ratio,"
          "entry,exit_price,outcome,ret_pct,pnl,hold_bars,bars_available,settled_at) "
          "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
          "ON DUPLICATE KEY UPDATE outcome=VALUES(outcome),ret_pct=VALUES(ret_pct),"
          "pnl=VALUES(pnl),entry=VALUES(entry),exit_price=VALUES(exit_price),"
          "hold_bars=VALUES(hold_bars),bars_available=VALUES(bars_available),"
          "settled_at=VALUES(settled_at)")

SIGNAL_SQL = (
    "SELECT id,ts,symbol,`interval`,pattern,direction,quality_allowed,"
    "quality_reason,regime,score,gap,noise_ratio "
    "FROM signal_features "
    "WHERE quality_allowed IS NOT NULL AND direction IN ('bullish','bearish') "
    "AND ts>=%s ORDER BY ts ASC")


def db_config(path: str) -> dict:
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    cfg = secrets_store.decrypt_config(cfg, str(Path(path).resolve()))
    m = cfg.get("mysql") or {}
    return {"host": m.get("host", "127.0.0.1"), "port": int(m.get("port", 3306)),
            "user": m.get("user"), "password": str(m.get("password") or ""),
            "database": m.get("database", "kline"), "charset": "utf8mb4",
            "connect_timeout": 8}


def settle(sig: dict, bars: list[tuple], times: list[int], *, sl_pct: float,
           rr: float, hold_bars: int, fee_bps: float, slip_bps: float,
           notional: float) -> dict | None:
    """对一条信号做虚拟结算；bars 为该币 1m K 线 (open_time,o,h,l,c) 升序。"""
    ts = int(sig["ts"])
    j = bisect.bisect_right(times, ts)
    if j >= len(bars):
        return None
    window = bars[j:j + hold_bars]
    if not window:
        return None
    direction = str(sig["direction"])
    sign = 1 if direction == "bullish" else -1
    entry = float(window[0][1]) * (1 + sign * slip_bps / 10_000)
    if entry <= 0:
        return None
    sl = entry * (1 - sign * sl_pct)
    tp = entry * (1 + sign * sl_pct * rr)
    exit_price = None
    outcome = "timeout"
    held = len(window)
    for k, (_, o, h, l, c) in enumerate(window):
        hit_sl = (o <= sl) if sign > 0 else (o >= sl)
        hit_tp = (h >= tp) if sign > 0 else (l <= tp)
        sl_touch = (l <= sl) if sign > 0 else (h >= sl)
        if hit_sl:
            exit_price, outcome, held = float(o), "gap_stop", k + 1
            break
        # 同一根内上下都触及：保守判止损
        if sl_touch and hit_tp:
            exit_price, outcome, held = sl, "stop", k + 1
            break
        if sl_touch:
            exit_price, outcome, held = sl, "stop", k + 1
            break
        if hit_tp:
            exit_price, outcome, held = tp, "target", k + 1
            break
    if exit_price is None:
        exit_price = float(window[-1][4])
    exit_price *= (1 - sign * slip_bps / 10_000)
    ret = sign * (exit_price - entry) / entry - 2 * (fee_bps + slip_bps) / 10_000
    return {"outcome": outcome, "ret_pct": round(ret * 100, 6),
            "pnl": round(ret * notional, 6),
            "entry": round(entry, 10), "exit_price": round(exit_price, 10),
            "hold_bars": held, "bars_available": len(window)}


def group_stats(rows: list[dict], base: float = 1000.0) -> dict:
    wins = sum(1 for r in rows if r["pnl"] > 0)
    losses = sum(1 for r in rows if r["pnl"] < 0)
    # 权益基准必须和 pnl 的名义仓位一致，否则回撤百分比会被放大/缩小
    curve = equity_curve([r["pnl"] for r in rows], base)
    dd = max_drawdown(curve)
    return {"n": len(rows), "wins": wins, "losses": losses,
            "winrate": decided_win_rate(wins, losses),
            "expectancyPct": round(sum(r["ret_pct"] for r in rows) / len(rows), 4)
            if rows else 0.0,
            "pnl": round(sum(r["pnl"] for r in rows), 2),
            "maxDDPct": dd["maxPct"]}


def reason_bucket(reason: str) -> str:
    r = str(reason or "")
    if "影线噪声" in r:
        return "高影线噪声"
    if "时段" in r or "12:00-18:00" in r:
        return "UTC 12-18 黑窗"
    if "形态" in r:
        return "形态黑名单"
    if "波动" in r or "分位" in r:
        return "极端波动"
    return "放行/其他"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/opt/kline-monitor/config.yaml")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--sl-pct", type=float, default=0.004)
    ap.add_argument("--rr", type=float, default=3.0)
    ap.add_argument("--hold-min", type=int, default=60)
    ap.add_argument("--fee-bps", type=float, default=4.0)
    ap.add_argument("--slip-bps", type=float, default=1.0)
    ap.add_argument("--notional", type=float, default=1000.0)
    ap.add_argument("--refresh", action="store_true", help="重算已结算的行")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    conn = pymysql.connect(**db_config(args.config), autocommit=True)
    cur = conn.cursor()
    cur.execute(DDL)
    since = int(time.time() * 1000) - args.days * 86_400_000
    cur.execute(SIGNAL_SQL, (since,))
    cols = ("id", "ts", "symbol", "interval", "pattern", "direction",
            "quality_allowed", "quality_reason", "regime", "score", "gap",
            "noise_ratio")
    signals = [dict(zip(cols, r)) for r in cur.fetchall()]
    print(f"有质量门判定的方向信号: {len(signals)} 条", flush=True)
    if not signals:
        return 1

    done = set()
    if not args.refresh:
        cur.execute("SELECT feature_id FROM shadow_gate_trades")
        done = {int(r[0]) for r in cur.fetchall()}
    pending = [s for s in signals if int(s["id"]) not in done]
    print(f"待结算 {len(pending)} 条（已结算 {len(done)} 条）", flush=True)

    # 一次性读全部 1m K 线并按币分组，避免逐条查库
    min_ts = min(int(s["ts"]) for s in signals)
    cur.execute("SELECT symbol,open_time,open,high,low,close FROM candle_archive "
                "WHERE `interval`='1m' AND open_time>=%s ORDER BY symbol,open_time ASC",
                (min_ts,))
    by_symbol: dict[str, list[tuple]] = defaultdict(list)
    for sym, t, o, h, l, c in cur.fetchall():
        by_symbol[str(sym)].append((int(t), float(o), float(h), float(l), float(c)))
    index = {sym: [b[0] for b in bars] for sym, bars in by_symbol.items()}
    print(f"1m K线: {sum(len(v) for v in by_symbol.values())} 根 / "
          f"{len(by_symbol)} 个币", flush=True)

    now_ms = int(time.time() * 1000)
    inserted = no_data = 0
    batch = []
    for s in pending:
        sym = str(s["symbol"] or "")
        bars = by_symbol.get(sym) or []
        res = settle(s, bars, index.get(sym, []), sl_pct=args.sl_pct, rr=args.rr,
                     hold_bars=args.hold_min, fee_bps=args.fee_bps,
                     slip_bps=args.slip_bps, notional=args.notional)
        if res is None:
            no_data += 1
            res = {"outcome": "no_data", "ret_pct": 0.0, "pnl": 0.0,
                   "entry": 0.0, "exit_price": 0.0, "hold_bars": 0,
                   "bars_available": 0}
        batch.append((int(s["id"]), int(s["ts"]), sym, s["interval"],
                      s["pattern"], s["direction"], int(s["quality_allowed"]),
                      (str(s["quality_reason"] or "")[:250]), s["regime"],
                      s["score"], s["gap"], s["noise_ratio"],
                      res["entry"], res["exit_price"], res["outcome"],
                      res["ret_pct"], res["pnl"], res["hold_bars"],
                      res["bars_available"], now_ms))
        if len(batch) >= 1000:
            cur.executemany(UPSERT, batch)
            inserted += len(batch)
            batch = []
    if batch:
        cur.executemany(UPSERT, batch)
        inserted += len(batch)
    print(f"本次写入 {inserted} 条，其中无后续K线 {no_data} 条", flush=True)

    # ---- 对照报表：读全量表，按放行/拦截分组 ----
    cur.execute("SELECT feature_id,ts,symbol,`interval`,pattern,direction,"
                "quality_allowed,quality_reason,regime,score,gap,noise_ratio,"
                "outcome,ret_pct,pnl FROM shadow_gate_trades WHERE ts>=%s "
                "ORDER BY ts ASC", (since,))
    rowcols = ("feature_id", "ts", "symbol", "interval", "pattern", "direction",
               "quality_allowed", "quality_reason", "regime", "score", "gap",
               "noise_ratio", "outcome", "ret_pct", "pnl")
    allrows = [dict(zip(rowcols, r)) for r in cur.fetchall()]
    valid = [r for r in allrows if r["outcome"] != "no_data" and
             int(r["feature_id"]) in {int(s["id"]) for s in signals}]
    allowed = [r for r in valid if int(r["quality_allowed"]) == 1]
    blocked = [r for r in valid if int(r["quality_allowed"]) == 0]

    print(f"\n=== 放行 / 拦截 影子对照（{len(valid)} 笔可结算，"
          f"排除 no_data {len(allrows)-len(valid)} 笔）", flush=True)
    print(f"口径: 1m 下一根开盘入场 / 止损 {args.sl_pct*100:.2f}% / "
          f"rr {args.rr:.1f} / 最多 {args.hold_min} 根 / 双边 "
          f"{args.fee_bps:.0f}bps + 滑点 {args.slip_bps:.0f}bps / 名义 "
          f"{args.notional:.0f} USDT")
    print(f"    {'分组':<16}{'笔数':>7}{'胜率':>9}{'期望%':>10}{'总盈亏':>11}{'回撤%':>9}")
    stats = lambda rows: group_stats(rows, args.notional)  # noqa: E731
    ga, gb = stats(allowed), stats(blocked)
    for label, g in (("闸门放行", ga), ("闸门拦截", gb)):
        wr = f"{g['winrate']:.1f}%" if g["winrate"] is not None else "-"
        print(f"    {label:<16}{g['n']:>7}{wr:>9}{g['expectancyPct']:>+10.4f}"
              f"{g['pnl']:>+11.2f}{g['maxDDPct']:>9.4f}")
    gain = round(ga["expectancyPct"] - gb["expectancyPct"], 4)
    killed = decided_win_rate(gb["wins"], gb["losses"])
    print(f"\n    放行增益(放行期望 - 拦截期望) = {gain:+.4f} 个百分点/笔")
    print(f"    误杀率 = 被拦信号里「如果开仓会赢」的比例 = "
          f"{killed if killed is not None else 0:.1f}%")
    if gain > 0.01:
        print("    ✅ 闸门有正向区分力")
    elif gain < -0.01:
        print("    ❌ 闸门是反向的：被拦的信号反而更好")
    else:
        print("    ⚠️ 闸门基本没有区分力（两组期望差接近 0）")

    print("\n=== 按拦截原因（只看被拦组）")
    by_reason: dict[str, list] = defaultdict(list)
    for r in blocked:
        by_reason[reason_bucket(r["quality_reason"])].append(r)
    for k, rows in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        g = stats(rows)
        wr = f"{g['winrate']:.1f}%" if g["winrate"] is not None else "-"
        print(f"    {k:<18}{g['n']:>6} 笔  胜率 {wr:>7}  "
              f"期望 {g['expectancyPct']:+.4f}%  盈亏 {g['pnl']:+.2f}")

    print("\n=== 按天（放行率与两组期望差）")
    by_day: dict[str, list] = defaultdict(list)
    for r in valid:
        day = datetime.fromtimestamp(int(r["ts"]) / 1000,
                                     timezone.utc).strftime("%Y-%m-%d")
        by_day[day].append(r)
    print(f"    {'日期':<12}{'放行率':>9}{'放行n':>7}{'放行E':>10}"
          f"{'拦截n':>7}{'拦截E':>10}{'差':>10}")
    day_report = []
    for day in sorted(by_day):
        rows = by_day[day]
        a = [r for r in rows if int(r["quality_allowed"]) == 1]
        b = [r for r in rows if int(r["quality_allowed"]) == 0]
        ea = stats(a)["expectancyPct"] if a else 0.0
        eb = stats(b)["expectancyPct"] if b else 0.0
        rate = len(a) / len(rows) if rows else 0
        print(f"    {day:<12}{rate:>8.1%}{len(a):>7}{ea:>+10.4f}"
              f"{len(b):>7}{eb:>+10.4f}{ea-eb:>+10.4f}")
        day_report.append({"day": day, "passRate": round(rate, 4),
                           "passN": len(a), "blockN": len(b),
                           "passE": ea, "blockE": eb, "diff": round(ea - eb, 4)})

    result = {"generatedAt": datetime.now(timezone.utc).isoformat(),
              "params": vars(args), "signals": len(signals),
              "settled": len(valid), "noData": len(allrows) - len(valid),
              "allowed": ga, "blocked": gb, "gain": gain,
              "killRate": killed,               "reasons": {
                  k: stats(v) for k, v in by_reason.items()},
              "daily": day_report}
    if args.json:
        Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print(f"\n    JSON -> {args.json}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
