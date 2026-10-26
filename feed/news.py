"""交易所公告聚合：币安 / OKX 的官方公告（真实数据，不需要 key）。

两个接口都是公开的：
  币安  /bapi/composite/v1/public/cms/article/list/query?type=1&pageNo=1&pageSize=N
  OKX   /api/v5/support/announcements?page=1

后台每 10 分钟拉一次（公告变化很慢，不需要高频），结果缓存并在客户端连入时下发。
网络失败时保留上一次结果，不抛异常打断主循环。
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request

# 币安这个接口 pageSize 上限很小（>5 会 400 Bad Request），固定 5 并翻页凑数
BINANCE_URL = ("https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
               "?type=1&pageNo={page}&pageSize=5")
OKX_URL = "https://www.okx.com/api/v5/support/announcements?page=1"
# 用常规浏览器 UA：币安 WAF 会直接拒绝带 bot 标识的 UA
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
FETCH_TIMEOUT = 12


def _get(url: str, proxy: str | None = None, *, lang: bool = False) -> dict | None:
    """lang=True 追加 Accept-Language/Referer（OKX 靠它返回中文标题；
    币安带多余头反而会被 WAF 拦，所以默认只发 UA）。"""
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if lang:
        headers["Accept-Language"] = "zh-CN,zh;q=0.9,en;q=0.8"
        headers["Referer"] = "https://www.okx.com/"
    req = urllib.request.Request(url, headers=headers)
    try:
        if proxy:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler(
                {"http": proxy, "https": proxy}))
            with opener.open(req, timeout=FETCH_TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
        return None


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


def fetch_binance(limit: int = 12, proxy: str | None = None) -> list[dict]:
    """币安公告（接口支持中文）。返回 [{source,title,url,ts,cat}]，按时间倒序。

    pageSize 上限是 5（传 12 直接 400），所以按需翻页。
    """
    out = []
    pages = max(1, min(4, (limit + 4) // 5))
    for page in range(1, pages + 1):
        data = _get(BINANCE_URL.format(page=page), proxy)
        if not data:
            break
        try:
            catalogs = ((data.get("data") or {}).get("catalogs") or [])
            for cat in catalogs:
                cat_name = cat.get("catalogName") or ""
                for art in (cat.get("articles") or []):
                    code = art.get("code")
                    title = _strip_html(art.get("title") or "")
                    if not title:
                        continue
                    out.append({
                        "source": "币安",
                        "cat": cat_name,
                        "title": title,
                        "url": (f"https://www.binance.com/zh-CN/support/announcement/{code}"
                                if code else ""),
                        "ts": int(art.get("releaseDate") or 0),
                    })
        except Exception:
            break
    # 去重（翻页可能重复）
    seen, uniq = set(), []
    for it in sorted(out, key=lambda x: -x["ts"]):
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        uniq.append(it)
    return uniq[:limit]


def fetch_okx(limit: int = 12, proxy: str | None = None) -> list[dict]:
    """OKX 公告（英文标题，接口只给英文）。返回 [{source,title,url,ts,cat}]。"""
    data = _get(OKX_URL, proxy, lang=True)
    if not data:
        return []
    out = []
    try:
        for grp in (data.get("data") or []):
            for it in (grp.get("details") or []):
                title = _strip_html(it.get("title") or "")
                if not title:
                    continue
                out.append({
                    "source": "欧易",
                    "cat": it.get("annType") or "",
                    "title": title,
                    "url": it.get("url") or "",
                    "ts": int(it.get("pTime") or 0),
                })
    except Exception:
        return []
    out.sort(key=lambda x: -x["ts"])
    return out[:limit]


def fetch_all(limit_each: int = 12, proxy: str | None = None) -> list[dict]:
    """合并两家公告，按时间倒序。"""
    items = fetch_binance(limit_each, proxy) + fetch_okx(limit_each, proxy)
    items.sort(key=lambda x: -x["ts"])
    for it in items:
        it["fetchedAt"] = int(time.time() * 1000)
    return items
