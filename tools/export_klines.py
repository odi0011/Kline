"""导出 K线长期归档（candle_archive 表）为 CSV，供回测使用。

用法（在仓库根目录执行）：
  python tools/export_klines.py --symbol btcusdt --interval 1m \
      --from 2026-01-01 --to 2026-02-01 --out btc_1m.csv

说明：
- 读取 config.yaml（支持密钥加密/环境变量覆盖），只读 candle_archive 表，不连交易所。
- --from/--to 支持 YYYY-MM-DD 或 YYYY-MM-DD HH:MM（本地时区），也接受 Unix 毫秒。
- 按时间分块拉取（默认 7 天/块），大区间不会一次把内存打爆。
- 输出列：open_time,datetime_utc,open,high,low,close,volume,trades
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import secrets_store  # noqa: E402
from core.candle import INTERVAL_MS  # noqa: E402
from storage import Storage  # noqa: E402

DEFAULT_CHUNK_MS = 7 * 86400_000


def parse_time(text: str) -> int:
    """解析时间参数：Unix 毫秒 / YYYY-MM-DD / YYYY-MM-DD HH:MM（本地时区）。"""
    raw = str(text or "").strip()
    if not raw:
        raise argparse.ArgumentTypeError("时间不能为空")
    try:
        value = int(raw)
        if value > 10_000_000_000:      # 毫秒
            return value
        return value * 1000             # 秒
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            naive = dt.datetime.strptime(raw, fmt)
            return int(naive.timestamp() * 1000)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"无法解析时间: {text}（用 YYYY-MM-DD 或 Unix 毫秒）")


def load_store(config_path: str) -> Storage:
    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    cfg = secrets_store.decrypt_config(cfg, config_path)
    return Storage(cfg.get("mysql", {}) or {})


def export(store: Storage, symbol: str, interval: str, start_ms: int, end_ms: int,
           out_path: str, chunk_ms: int = DEFAULT_CHUNK_MS) -> int:
    """分块导出到 CSV，返回写入行数（interval 只用于展示，不做聚合）。"""
    symbol = str(symbol or "").strip().lower()
    interval = str(interval or "").strip()
    if not symbol or not interval:
        raise ValueError("symbol/interval 不能为空")
    if end_ms < start_ms:
        raise ValueError("--to 必须晚于 --from")
    if chunk_ms <= 0:
        chunk_ms = DEFAULT_CHUNK_MS
    # 每次查询至少覆盖一根K线的时间跨度（INTERVAL_MS 本身就是毫秒），
    # 避免单根周期很长时把窗口切得比一根K线还小。
    step = max(chunk_ms, int(INTERVAL_MS.get(interval, 60_000)))
    rows_written = 0
    cursor = start_ms
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["open_time", "datetime_utc", "open", "high", "low",
                         "close", "volume", "trades"])
        while cursor <= end_ms:
            window_end = min(cursor + step, end_ms)
            rows = store.archive_range(symbol, interval, cursor, window_end,
                                       limit=200_000)
            if rows:
                for row in rows:
                    t = int(row.get("t") or 0)
                    stamp = dt.datetime.fromtimestamp(t / 1000, tz=dt.timezone.utc)
                    writer.writerow([t, stamp.strftime("%Y-%m-%d %H:%M:%S"),
                                     row.get("o"), row.get("h"), row.get("l"),
                                     row.get("c"), row.get("v"), row.get("trades")])
                rows_written += len(rows)
                cursor = int(rows[-1].get("t") or 0) + 1
            else:
                cursor = window_end + 1
            if cursor <= 0:             # 防御坏数据导致死循环
                break
    return rows_written


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="导出 K线长期归档为 CSV（回测用）")
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--symbol", required=True, help="如 btcusdt")
    ap.add_argument("--interval", required=True, help="如 1m / 3m / 15m / 1h / 4h")
    ap.add_argument("--from", dest="start", required=True, help="YYYY-MM-DD 或 Unix 毫秒")
    ap.add_argument("--to", dest="end", required=True, help="YYYY-MM-DD 或 Unix 毫秒")
    ap.add_argument("--out", required=True, help="输出 CSV 路径")
    ap.add_argument("--chunk-days", type=int, default=7, help="分块天数（默认 7）")
    args = ap.parse_args(argv)
    if not os.path.exists(args.config):
        print(f"[export] 找不到配置 {args.config}，请在部署目录执行")
        return 2
    try:
        store = load_store(args.config)
    except (ValueError, OSError, yaml.YAMLError, secrets_store.SecretsError) as exc:
        print(f"[export] 读取配置失败: {exc}")
        return 2
    if not store.enabled:
        print("[export] 存储层未启用（MySQL 未配置/连不上），无法导出")
        return 2
    try:
        start_ms, end_ms = parse_time(args.start), parse_time(args.end)
        count = export(store, args.symbol, args.interval, start_ms, end_ms,
                       args.out, chunk_ms=max(1, args.chunk_days) * 86400_000)
    except (ValueError, OSError, argparse.ArgumentTypeError) as exc:
        print(f"[export] 导出失败: {exc}")
        return 1
    print(f"[export] {args.symbol} {args.interval} -> {args.out}，共 {count} 根K线")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
