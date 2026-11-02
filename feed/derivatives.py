"""衍生品数据入口：优先币安，被地区限制时自动回退欧易。

线上服务器访问币安期货 REST 会拿到 HTTP 451（地域限制），但 WS 域名
可用。之前每轮都在 451 上失败，整块指标永远空白。这里做一次探测：
币安不可用就整体改用欧易，并把"当前数据来自哪家"告诉前端。
"""
from __future__ import annotations

import time

from feed import market_data, okx

# 币安可用性探测结果缓存：避免每轮都先撞一次 451
_PROBE_TTL_MS = 10 * 60_000
_probe = {"ts": 0, "ok": None}


def binance_available() -> bool:
    now = int(time.time() * 1000)
    if _probe["ok"] is not None and now - _probe["ts"] < _PROBE_TTL_MS:
        return _probe["ok"]
    # 用一个最轻的公开接口探测：拿不到行情就认为不可用
    row = market_data._get_json("/fapi/v1/time", {})
    ok = isinstance(row, dict) and bool(row.get("serverTime"))
    _probe.update(ts=now, ok=ok)
    return ok


def derivatives_snapshot(symbol: str, period: str = "5m", proxy: str | None = None) -> dict:
    """按可用性选数据源；返回结构与币安版本一致，额外带 venue 字段。"""
    if binance_available():
        row = market_data.derivatives_snapshot(symbol, period, proxy)
        if row.get("price") is not None:
            return {**row, "venue": "binance"}
        # 币安这一币种偶发失败，单币回退欧易
    return okx.derivatives_snapshot(symbol, period)
