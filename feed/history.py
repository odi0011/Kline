"""历史K线导入：从 Binance 公开数据仓下载 -> 后复权拼接 -> 落库。

## 为什么需要"后复权"

永续合约的 K 线本身**不复权**：资金费率每 8 小时结算一次，长期持有者
的成本会漂移，直接用合约价做多年回测会把"资金费带来的基准偏移"
当成策略收益。而合约 2019-12 才上线，再往前只有现货。

所以这里做两件事：

1. **合约段用合约价，合约上线前用现货价补齐**；
2. 在拼接点算一个**比例因子**，把现货段整体缩放到合约的基准上，
   保证跨段连续（这就是后复权：以最新价为基准，向前调整历史价）。

后复权 vs 前复权的取舍：
  · 前复权会随着"最新价"变化，历史数据每次都要重算——不适合入库；
  · 后复权以拼接点为锚，历史段一次性算好就不变，
    而且回测看的是**相对涨跌幅**，后复权不影响任何指标。
  这正是不落"前复权"的原因。

## 数据来源

`data.binance.vision`（币安官方公开数据仓，免 key）：
  · 合约：data/futures/um/{monthly,daily}/klines/<SYM>/<IV>/
  · 现货：data/spot/{monthly,daily}/klines/<SYM>/<IV>/
实测最早：现货 2017-08、合约 2020-01（BTCUSDT 1h）。

CSV 列：open_time,open,high,low,close,volume,close_time,quote_volume,
        count,taker_buy_volume,taker_buy_quote_volume,ignore
**2026 年起部分文件带表头**，所以要按首行是否数字判断，不能假定。
"""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone

BASE = "https://data.binance.vision/data"
UA = "Mozilla/5.0 (compatible; kline-history-importer/1.0)"
# 各周期的毫秒数（这里只用需要的几种）
IV_MS = {"5m": 300_000, "15m": 900_000, "30m": 1_800_000,
         "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "1d": 86_400_000}


def _get(url: str, timeout: int = 45) -> bytes | None:
    """下载；404 返回 None（未来月份/未上线时是常态，不是错误）。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


def _parse_csv(raw: bytes) -> list[dict]:
    """zip 字节 -> K线行。自动跳过表头，缺列的行直接丢弃。"""
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
        name = zf.namelist()[0]
        text = zf.read(name).decode("utf-8", "replace")
    except (zipfile.BadZipFile, IndexError, KeyError):
        return []
    out = []
    for line in text.splitlines():
        parts = line.split(",")
        if len(parts) < 11:
            continue
        try:
            t = int(parts[0])
        except ValueError:
            continue                      # 表头行（open_time）
        try:
            vol = float(parts[5])
            buy = float(parts[9]) if len(parts) > 9 and parts[9] else 0.0
            out.append({
                "t": t,
                "o": float(parts[1]), "h": float(parts[2]),
                "l": float(parts[3]), "c": float(parts[4]),
                "v": vol,
                # count 是成交笔数；老文件里偶尔为空，按 0 处理
                "trades": int(float(parts[8])) if parts[8] else 0,
                "buyVol": buy,
                "sellVol": max(0.0, vol - buy),
            })
        except (ValueError, IndexError):
            continue
    return out


def _months(start: tuple[int, int], end: tuple[int, int]) -> list[str]:
    y, m = start
    out = []
    while (y, m) <= end:
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def download_monthly(symbol: str, interval: str, market: str,
                     start: tuple[int, int], end: tuple[int, int],
                     progress=None) -> list[dict]:
    """按月下载。market: futures | spot。"""
    folder = "futures/um" if market == "futures" else "spot"
    sym = symbol.upper()
    rows: list[dict] = []
    months = _months(start, end)
    for i, ym in enumerate(months):
        url = (f"{BASE}/{folder}/monthly/klines/{sym}/{interval}/"
               f"{sym}-{interval}-{ym}.zip")
        raw = _get(url)
        if raw is None:
            if progress:
                progress(f"{ym} 无文件")
            continue
        got = _parse_csv(raw)
        rows.extend(got)
        if progress:
            progress(f"{ym}: {len(got)} 根")
    return rows


def download_daily(symbol: str, interval: str, market: str,
                   days: list[str], progress=None) -> list[dict]:
    """按日下载（补最新未归档的部分）。days 形如 '2026-11-04'。"""
    folder = "futures/um" if market == "futures" else "spot"
    sym = symbol.upper()
    rows: list[dict] = []
    for day in days:
        url = (f"{BASE}/{folder}/daily/klines/{sym}/{interval}/"
               f"{sym}-{interval}-{day}.zip")
        raw = _get(url)
        if raw is None:
            continue
        got = _parse_csv(raw)
        rows.extend(got)
        if progress and got:
            progress(f"{day}: {len(got)} 根")
    return rows


def back_adjust(spot_rows: list[dict], futures_rows: list[dict]) -> tuple[list[dict], dict]:
    """把现货段按拼接点的比例缩放，接到合约段前面（后复权）。

    **缩放而不是平移**：价格是指数变化的，比例因子才保持涨跌幅不变。
    因子取"合约第一根 open / 现货在同一时刻或之前的最后一根 close"，
    即"如果那时就有合约，它会是什么价"。

    返回 (合并后的行, 说明)。合并后每行带 `source` 标记，
    差异因子写进说明里，便于核查。
    """
    if not spot_rows or not futures_rows:
        return (spot_rows or futures_rows), {"adjusted": False,
                                            "reason": "只有单边数据"}
    fut_first = min(r["t"] for r in futures_rows)
    # 现货里 <= 合约首根开盘时间的最后一根
    before = [r for r in spot_rows if r["t"] <= fut_first]
    if not before:
        return futures_rows, {"adjusted": False,
                              "reason": "现货段与合约段没有重叠，无法求因子"}
    anchor_spot = max(before, key=lambda r: r["t"])
    anchor_fut = min(futures_rows, key=lambda r: r["t"])
    if anchor_spot["c"] <= 0:
        return futures_rows, {"adjusted": False, "reason": "锚点价格非法"}

    factor = anchor_fut["o"] / anchor_spot["c"]
    scaled = []
    for r in before:
        scaled.append({
            "t": r["t"],
            "o": r["o"] * factor, "h": r["h"] * factor,
            "l": r["l"] * factor, "c": r["c"] * factor,
            # **成交量不缩放**：它是绝对量，缩放会伪造流动性
            "v": r["v"], "trades": r["trades"],
            "buyVol": r["buyVol"], "sellVol": r["sellVol"],
        })
    for r in futures_rows:
        scaled.append(dict(r))
    scaled.sort(key=lambda r: r["t"])
    # 同一时间戳以合约为准（现货段只保留合约上线前的部分）
    dedup: dict[int, dict] = {}
    for r in scaled:
        dedup[r["t"]] = r
    merged = [dedup[t] for t in sorted(dedup)]
    return merged, {
        "adjusted": True,
        "factor": round(factor, 8),
        "anchorTime": anchor_fut["t"],
        "anchorSpotClose": anchor_spot["c"],
        "anchorFuturesOpen": anchor_fut["o"],
        "spotRows": len(before),
        "futuresRows": len(futures_rows),
        "total": len(merged),
    }


def split_sources(merged: list[dict], futures_first_ms: int):
    """把合并后的行拆回 (现货段, 合约段)，落库时带各自的 source。"""
    spot = [dict(r, source="spot") for r in merged if r["t"] < futures_first_ms]
    fut = [dict(r, source="futures") for r in merged if r["t"] >= futures_first_ms]
    return spot, fut


def fmt_ts(ms: int) -> str:
    if not ms:
        return "-"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def month_range(from_ts_ms: int, to_ts_ms: int) -> tuple[tuple[int, int], tuple[int, int]]:
    a = datetime.fromtimestamp(from_ts_ms / 1000, tz=timezone.utc)
    b = datetime.fromtimestamp(to_ts_ms / 1000, tz=timezone.utc)
    return (a.year, a.month), (b.year, b.month)
