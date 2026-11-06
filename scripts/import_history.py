#!/usr/bin/env python3
"""导入多年历史K线（后复权）到 candle_history，供回测使用。

用法（在服务器上跑）：
    venv/bin/python scripts/import_history.py --symbols btcusdt ethusdt \
        --interval 1h --from 2017-08

默认行为：
  · 合约段：data/futures/um（最早的 2020-01）
  · 合约上线前：用现货补，并在拼接点做**后复权**缩放
  · 已存在的行 REPLACE，反复跑是幂等的
  · 只写 candle_history（历史仓），不动 candle_archive（实时归档）
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import secrets_store  # noqa: E402
import yaml  # noqa: E402

from feed import history as H  # noqa: E402
from storage import Storage  # noqa: E402

# 永续合约上线的月份（BTC/ETH 最早；其他币更晚，下载 404 会自动跳过）
FUTURES_FIRST = (2020, 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=["btcusdt"])
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--from", dest="since", default="2017-08")
    ap.add_argument("--config", default="/opt/kline-monitor/config.yaml")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    try:
        cfg = secrets_store.decrypt_config(cfg, args.config)
    except Exception:
        pass
    store = Storage(cfg.get("mysql") or {})
    if not store.enabled:
        print("数据库不可用")
        return 1

    sy, sm = (int(x) for x in args.since.split("-"))
    today = time.gmtime()
    end = (today.tm_year, today.tm_mon)

    for raw_sym in args.symbols:
        sym = raw_sym.lower()
        print(f"\n=== {sym} {args.interval} 从 {args.since}")
        t0 = time.time()

        # 1) 合约段（月频，到上个月；本月用日频补）
        fut_rows = H.download_monthly(sym, args.interval, "futures",
                                      FUTURES_FIRST, end,
                                      progress=lambda s: print("  合约", s))
        print(f"  合约月度合计 {len(fut_rows)} 根，{time.time()-t0:.1f}s")
        if not fut_rows:
            print("  合约段为空，跳过（该币可能还没上线）")
            continue

        # 2) 现货段（只取合约上线前，省流量）
        spot_rows = []
        if (sy, sm) < FUTURES_FIRST:
            spot_rows = H.download_monthly(sym, args.interval, "spot",
                                           (sy, sm), FUTURES_FIRST,
                                           progress=lambda s: print("  现货", s))
            print(f"  现货月度合计 {len(spot_rows)} 根")

        # 3) 后复权拼接
        merged, info = H.back_adjust(spot_rows, fut_rows)
        print(f"  后复权: {info}")

        fut_first = min(r["t"] for r in fut_rows)
        spot_part, fut_part = H.split_sources(merged, fut_first)
        n1 = store.save_candle_history(sym, args.interval, spot_part, "spot")
        n2 = store.save_candle_history(sym, args.interval, fut_part, "futures")
        span = [r for r in store.candle_history_span(sym, args.interval)]
        print(f"  落库 spot={n1} futures={n2}")
        for row in span:
            print(f"  {row['source']:8s} {row['count']:>7d} 根  "
                  f"{H.fmt_ts(row['from'])} .. {H.fmt_ts(row['to'])}")
        print(f"  用时 {time.time()-t0:.1f}s")

    print("\n完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
