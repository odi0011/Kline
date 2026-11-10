#!/usr/bin/env python3
"""策略效果报表：只读线上库，把"到底赚没赚钱、闸门有没有在干活"算成数字。

用法：
    python scripts/strategy_report.py
    python scripts/strategy_report.py --config /opt/kline-monitor/config.yaml --days 30
    python scripts/strategy_report.py --json report.json

只读，不写库、不连交易所。config.yaml 里加密的密钥会用 secrets_store 解密。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import yaml
    import pymysql
except ImportError as exc:
    sys.exit(f"缺少依赖 {exc.name}：pip install pyyaml pymysql")

from decision.metrics import decided_win_rate, equity_curve, max_drawdown  # noqa: E402


def db_config(path: Path) -> dict:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    import secrets_store
    cfg = secrets_store.decrypt_config(cfg, str(path.resolve()))
    m = cfg.get("mysql") or {}
    if not m.get("user"):
        sys.exit(f"{path} 里没有可用的 mysql 配置")
    return {
        "host": m.get("host", "127.0.0.1"),
        "port": int(m.get("port", 3306)),
        "user": m.get("user"),
        "password": str(m.get("password") or ""),
        "database": m.get("database", "kline"),
        "charset": "utf8mb4",
        "connect_timeout": 5,
    }


def _i(value) -> int:
    return int(value or 0)


def _f(value) -> float:
    return round(float(value or 0), 4)


def bucket(reason: str) -> str:
    """把带具体分数的拦截原因归成大类，便于看闸门结构。"""
    r = str(reason or "")
    if "十字星" in r or "没有方向" in r:
        return "中性形态无方向"
    if "清算级联" in r:
        return "清算级联"
    if "市场状态数据不足" in r:
        return "行情数据不足"
    if "EMA" in r or "多周期" in r or "方向为" in r:
        return "多周期方向冲突"
    if "方向" in r and ("不一致" in r or "重新评估" in r):
        return "方向重评估"
    if "共鸣分" in r or "分离度" in r or "评分" in r:
        return "评分/分离度不足"
    if "风控" in r or "冷却" in r or "持仓" in r or "限额" in r:
        return "统一风控"
    return "其他"


def collect(conn, days: int) -> dict:
    cur = conn.cursor()
    now = int(time.time() * 1000)
    since = now - days * 86_400_000

    def q(sql, args=()):
        cur.execute(sql, args)
        return cur.fetchall()

    out: dict = {"generated_at": datetime.now(timezone.utc).isoformat(),
                 "days": days}

    outcomes = {str(r[0]): _i(r[1]) for r in q(
        "SELECT outcome, COUNT(*) FROM hits GROUP BY outcome")}
    oldest = q("SELECT MIN(ts) FROM hits WHERE outcome='pending'")[0][0]
    out["hits"] = {
        "outcomes": outcomes,
        "total": sum(outcomes.values()),
        "oldest_pending_ms": int(oldest) if oldest else None,
        "oldest_pending_days": round((now - int(oldest)) / 86_400_000, 1) if oldest else 0,
    }

    out["patterns"] = [
        {"pattern": str(r[0]), "wins": _i(r[1]), "losses": _i(r[2]),
         "winrate": round(_i(r[1]) / (_i(r[1]) + _i(r[2])), 4)
         if (_i(r[1]) + _i(r[2])) else 0.0}
        for r in q(
            "SELECT pattern, SUM(outcome='win'), SUM(outcome='loss') FROM hits "
            "WHERE ts>=%s AND outcome IN ('win','loss') "
            "GROUP BY pattern ORDER BY COUNT(*) DESC LIMIT 20", (since,))
    ]

    out["traces"] = [
        {"kind": str(r[0]), "status": str(r[1]), "count": _i(r[2])}
        for r in q(
            "SELECT kind, status, COUNT(*) FROM decision_traces WHERE ts>=%s "
            "GROUP BY kind, status ORDER BY COUNT(*) DESC", (since,))
    ]

    skips: dict[str, int] = {}
    for reason, count in q(
            "SELECT reason, COUNT(*) FROM quant_events "
            "WHERE kind='skip' AND ts>=%s GROUP BY reason", (since,)):
        key = bucket(reason)
        skips[key] = skips.get(key, 0) + _i(count)
    out["skips"] = sorted(
        ({"reason": k, "count": v} for k, v in skips.items()),
        key=lambda x: -x["count"])

    row = q("SELECT COUNT(*), COALESCE(SUM(pnl),0), SUM(pnl>0), "
            "SUM(reason='removed') FROM paper_trades WHERE closed_ts>=%s", (since,))[0]
    out["paper_all"] = {"trades": _i(row[0]), "pnl": _f(row[1]),
                        "wins": _i(row[2]), "removed": _i(row[3])}

    # 胜率统一用 decision.metrics.decided_win_rate：分母只算有明确输赢的
    # 交易。旧实现用 wins/全部笔数，把平价离场算进分母，系统性偏低
    # （线上全量 12.6% vs 正确 52.2%），与前端 paper._db_summary 也对不上。
    row = q("SELECT COUNT(*), COALESCE(SUM(pnl),0), SUM(pnl>0), SUM(pnl<0), "
            "SUM(pnl=0), AVG(CASE WHEN pnl>0 THEN pnl END), "
            "AVG(CASE WHEN pnl<0 THEN pnl END) FROM paper_trades "
            "WHERE closed_ts>=%s AND reason<>'removed'", (since,))[0]
    n, w, l, flat = _i(row[0]), _i(row[2]), _i(row[3]), _i(row[4])
    avg_win, avg_loss = _f(row[5]), _f(row[6])
    # 胜率本身不能判断好坏：payout 决定打平线。名义 rr3 若实际只兑现
    # 到 1.07，打平胜率就会从 25% 抬到 48%，45% 的胜率必然是亏的。
    payout = round(avg_win / abs(avg_loss), 3) if avg_loss else None
    break_even = (round(abs(avg_loss) / (avg_win + abs(avg_loss)) * 100, 2)
                  if (avg_win + abs(avg_loss)) else None)
    out["paper_real"] = {
        "trades": n, "pnl": _f(row[1]),
        "wins": w, "losses": l, "flat": flat, "decided": w + l,
        "winrate": (decided_win_rate(w, l) or 0.0) / 100,
        "naiveWinrate": round(w / n, 4) if n else 0.0,
        "avgWin": avg_win, "avgLoss": avg_loss,
        "payout": payout, "breakEvenWinrate": break_even,
    }

    out["paper_by_reason"] = [
        {"reason": str(r[0]), "trades": _i(r[1]), "pnl": _f(r[2]), "wins": _i(r[3])}
        for r in q(
            "SELECT reason, COUNT(*), COALESCE(SUM(pnl),0), SUM(pnl>0) FROM paper_trades "
            "WHERE closed_ts>=%s AND reason<>'removed' "
            "GROUP BY reason ORDER BY COUNT(*) DESC", (since,))
    ]
    # 反手是模拟仓最大的纯磨损项，单独标出来便于按天监控
    out["paper_real"]["flipTrades"] = next(
        (r["trades"] for r in out["paper_by_reason"]
         if r["reason"] == "check_flip"), 0)
    out["paper_real"]["flipPnl"] = next(
        (r["pnl"] for r in out["paper_by_reason"]
         if r["reason"] == "check_flip"), 0.0)
    out["paper_by_grade"] = [
        {"grade": str(r[0]), "trades": _i(r[1]), "pnl": _f(r[2]), "wins": _i(r[3])}
        for r in q(
            "SELECT grade, COUNT(*), COALESCE(SUM(pnl),0), SUM(pnl>0) FROM paper_trades "
            "WHERE closed_ts>=%s AND reason<>'removed' "
            "GROUP BY grade ORDER BY COUNT(*) DESC", (since,))
    ]

    row = q("SELECT COUNT(*), COALESCE(SUM(pnl),0), SUM(pnl>0), SUM(pnl<0), "
            "SUM(pnl=0) FROM quant_trades WHERE ts>=%s", (since,))[0]
    n, w, l, flat = _i(row[0]), _i(row[2]), _i(row[3]), _i(row[4])
    out["live"] = {"trades": n, "pnl": _f(row[1]),
                   "wins": w, "losses": l, "flat": flat, "decided": w + l,
                   "winrate": (decided_win_rate(w, l) or 0.0) / 100}
    # 明细必须和主数字用同一个时间窗。以前这里漏了 WHERE ts>=%s，
    # "近 1 天"报表里会混进全表历史（例如修复前那 6 笔保护单补挂失败），
    # 看起来像新代码还在犯老毛病。
    out["live_reasons"] = [
        {"reason": str(r[0]), "count": _i(r[1])}
        for r in q("SELECT reason, COUNT(*) FROM quant_trades WHERE ts>=%s "
                   "GROUP BY reason ORDER BY COUNT(*) DESC LIMIT 10", (since,))
    ]

    row = q("SELECT COUNT(*), MIN(open_time), MAX(open_time) FROM candle_archive")[0]
    out["archive"] = {"bars": _i(row[0]), "from_ms": int(row[1] or 0),
                      "to_ms": int(row[2] or 0)}

    # ---- 最大回撤：三条独立曲线，都用 decision.metrics 同一算法 ----
    # paper/live 用名义本金 10000 起算（与回测 initial_equity 对齐），
    # 真实账户直接用 equity.bal 采样绝对值。
    def _pnls(sql):
        return [float(r[0] or 0) for r in q(sql, (since,))]

    paper_pnls = _pnls(
        "SELECT pnl FROM paper_trades WHERE closed_ts>=%s AND reason<>'removed' "
        "ORDER BY closed_ts ASC, id ASC")
    live_pnls = _pnls(
        "SELECT pnl FROM quant_trades WHERE ts>=%s ORDER BY ts ASC, id ASC")
    acct = q("SELECT MIN(acct) FROM equity WHERE ts>=%s", (since,))[0][0]
    eq_curve = []
    if acct:
        eq_curve = [float(r[0] or 0) for r in q(
            "SELECT bal FROM equity WHERE acct=%s AND ts>=%s ORDER BY ts ASC",
            (acct, since))]
    out["drawdown"] = {
        "paper": max_drawdown(equity_curve(paper_pnls, 10_000.0)),
        "live": max_drawdown(equity_curve(live_pnls, 10_000.0)),
        "account": max_drawdown(eq_curve) if eq_curve else {},
        "accountPoints": len(eq_curve),
        "accountStart": eq_curve[0] if eq_curve else None,
        "accountEnd": eq_curve[-1] if eq_curve else None,
    }

    # ---- 近 7 天趋势：按天看胜率/净盈亏是否恶化 ----
    trend_since = now - 7 * 86_400_000
    out["trend"] = [
        {"day": str(r[0]), "trades": _i(r[1]), "wins": _i(r[2]),
         "losses": _i(r[3]), "pnl": _f(r[4]),
         "winrate": decided_win_rate(_i(r[2]), _i(r[3]))}
        for r in q(
            "SELECT DATE(FROM_UNIXTIME(closed_ts/1000)), COUNT(*), SUM(pnl>0), "
            "SUM(pnl<0), COALESCE(SUM(pnl),0) FROM paper_trades "
            "WHERE closed_ts>=%s AND reason<>'removed' "
            "GROUP BY DATE(FROM_UNIXTIME(closed_ts/1000)) ORDER BY 1 DESC LIMIT 7",
            (trend_since,))
    ]
    return out


def render(d: dict) -> None:
    h = d["hits"]
    total = h["total"] or 1
    print(f"\n=== 策略效果报表（近 {d['days']} 天，{d['generated_at'][:19]} UTC）===\n")

    print("[1] 命中判定健康度")
    for k in ("win", "loss", "flat", "pending", "neutral", "no_data"):
        if k in h["outcomes"]:
            print(f"    {k:<10} {h['outcomes'][k]:>7}  ({h['outcomes'][k]/total:5.1%})")
    print(f"    合计 {h['total']} 条；最老 pending {h['oldest_pending_days']} 天；"
          f"K线归档 {d['archive']['bars']} 根")
    print("    neutral = 不参与判定（中性形态 / 盘中预警 live）")
    if h["outcomes"].get("pending", 0) / total > 0.3:
        print("    !! pending 占比过高，胜率样本不完整")

    print("\n[2] 形态方向准确率（只看已判定的 win/loss）")
    print(f"    {'形态':<24}{'胜':>7}{'负':>7}{'胜率':>9}")
    for p in d["patterns"][:12]:
        print(f"    {p['pattern']:<24}{p['wins']:>7}{p['losses']:>7}{p['winrate']:>8.1%}")

    print("\n[3] 决策闸门")
    for t in d["traces"][:8]:
        print(f"    {t['kind']:<8}{t['status'] or '(空)':<14}{t['count']:>7}")
    print("    skip 原因归类：")
    for s in d["skips"][:8]:
        print(f"      {s['reason']:<18}{s['count']:>7}")

    pa, pr = d["paper_all"], d["paper_real"]
    print("\n[4] 模拟仓绩效")
    print(f"    全量流水     {pa['trades']:>7} 笔  净 {pa['pnl']:>10.2f}  "
          f"盈 {pa['wins']:>6} 笔  （其中 removed {pa['removed']} 笔）")
    print(f"    剔除 removed {pr['trades']:>7} 笔  净 {pr['pnl']:>10.2f}  "
          f"盈 {pr['wins']:>6} 笔  胜率 {pr['winrate']:.1%}")
    print(f"    胜率口径: 胜/(胜+负) = {pr['wins']}/{pr['decided']} "
          f"（平价 {pr['flat']} 笔不计入）；旧口径 胜/全部 = "
          f"{pr['naiveWinrate']:.1%}，与前端不一致，已弃用")
    payout = pr.get("payout")
    be = pr.get("breakEvenWinrate")
    pay_text = f"{payout:.3f}" if payout is not None else "-"
    be_text = f"{be:.1f}%" if be is not None else "-"
    print(f"    盈亏比口径: 均盈 {pr['avgWin']:+.3f} / 均亏 {pr['avgLoss']:+.3f} "
          f"→ payout {pay_text}  打平胜率 {be_text}"
          f"（当前 {pr['winrate']:.1%}）")
    flips = pr.get("flipTrades", 0)
    print(f"    反手 {flips} 笔 ({flips / pr['trades']:.1%})  "
          f"合计 {pr.get('flipPnl', 0.0):+.2f} —— 短持有反手是主要磨损")
    print("    按平仓原因：")
    for r in d["paper_by_reason"][:8]:
        print(f"      {r['reason']:<14}{r['trades']:>7} 笔  净 {r['pnl']:>10.2f}  盈 {r['wins']:>6}")
    print("    按信号等级（剔除 removed）：")
    for g in d["paper_by_grade"]:
        print(f"      {g['grade']:<14}{g['trades']:>7} 笔  净 {g['pnl']:>10.2f}  盈 {g['wins']:>6}")

    lv = d["live"]
    print("\n[5] 真实成交（测试网账户）")
    print(f"    {lv['trades']} 笔  净 {lv['pnl']:.2f}  盈 {lv['wins']} 笔  "
          f"胜率 {lv['winrate']:.1%}（{lv['wins']}/{lv['decided']}）")
    for r in d["live_reasons"][:6]:
        print(f"      {r['reason'][:34]:<36}{r['count']:>5}")

    dd = d.get("drawdown") or {}
    print("\n[6] 最大回撤（统一口径：权益曲线从历史峰值回落，正数 = 跌了多少）")
    for key, label in (("paper", "模拟仓(名义本金10000)"),
                       ("live", "实盘成交(同上)"),
                       ("account", "真实账户(equity采样)")):
        x = dd.get(key) or {}
        if not x:
            continue
        print(f"    {label:<26} 最大 {x['maxPct']:>7.4f}% ({x['maxAbs']:>9.2f})  "
              f"当前 {x['currentPct']:>7.4f}% ({x['currentAbs']:>8.2f})  "
              f"峰值 {x['peak']:.2f}")
    if dd.get("accountStart") is not None:
        print(f"    账户权益 {dd['accountStart']:.2f} → {dd['accountEnd']:.2f}"
              f"（{dd['accountPoints']} 个采样点）")

    trend = list(reversed(d.get("trend") or []))
    print("\n[7] 近 7 天趋势（模拟仓，剔除 removed；累计从 0 起）")
    if not trend:
        print("    无样本")
    else:
        cum = 0.0
        print(f"    {'日期':<12}{'笔数':>7}{'胜率':>9}{'当日净':>11}{'累计净':>11}")
        for t in trend:
            cum += t["pnl"]
            wr = f"{t['winrate']:.1f}%" if t["winrate"] is not None else "-"
            print(f"    {t['day']:<12}{t['trades']:>7}{wr:>9}"
                  f"{t['pnl']:>+11.2f}{cum:>+11.2f}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description="只读策略效果报表")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"), help="config.yaml 路径")
    ap.add_argument("--days", type=int, default=7, help="统计窗口天数")
    ap.add_argument("--json", default="", help="同时把结构化结果写到该 JSON 文件")
    args = ap.parse_args()

    path = Path(args.config)
    if not path.exists():
        sys.exit(f"找不到配置文件：{path}")
    conn = pymysql.connect(**db_config(path))
    try:
        data = collect(conn, max(1, args.days))
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
