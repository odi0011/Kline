"""GIPHY 贴纸搜索：让 Agent 从"只会用 3 张内置图"变成能用海量表情包。

为什么用 GIPHY 而不是自建素材库：

* **不在我们服务器上存图**。只拿到 CDN 地址，图片由浏览器直接去 GIPHY 加载，
  仓库/磁盘都不用扩张。
* **量级不同**。内置三张图表达力有上限，GIPHY 是数百万张的贴纸库。
* **有专门的 stickers 端点**（`/v1/stickers/search`），返回透明底贴纸，
  不是普通 GIF，落在聊天气泡里不会带一块黑底。
* 返回 `images.original.width/height`，前端可以先用真实像素占位，
  加载完不会撑开气泡（CLS）——这正是之前手写 `STICKER_SIZES` 想解决的问题。

配额与义务（GIPHY 的硬性要求，别删）：

* 免费 beta key 只有 **100 次/小时**。所以本地做结果缓存，同一个查询
  一小时内只打一次网络；缓存命中不消耗配额。
* API 使用方**必须显示 "Powered by GIPHY" 署名**，前端已在贴纸下方渲染。

没配 key 时 `search_stickers` 返回 ok=False，调用方回退到内置三张图，
不报错、不影响对话。
"""
from __future__ import annotations

import os
import re
import threading
import time
import urllib.parse
import urllib.request

# 复用搜索模块的 HTTP 层：代理、超时、DNS 校验、禁自动重定向都一致，
# 不再写第二套（两套 HTTP 迟早会在代理/超时上行为不一致）。
from feed.search import HEADERS, _clean_proxy, _open, _short_error

GIPHY_URL = "https://api.giphy.com/v1/stickers/search"
KEY_ENV = "KLINE_GIPHY_KEY"
DEFAULT_TIMEOUT = 8.0
MAX_QUERY_CHARS = 60
# 贴纸地址只允许这几个域名：返回值会直接进前端 <img src>，
# 不该让任何第三方地址借这条路出现在页面上。
ALLOWED_MEDIA_SUFFIXES = (".giphy.com", ".giphyusercontent.com")
# 配额有限：同查询缓存 1 小时。100 次/小时对个人使用足够，
# 但模型可能反复问同一个情绪词，缓存能把重复调用吃掉。
CACHE_TTL = 3600.0
CACHE_MAX = 128

_cache: dict[tuple, tuple[float, list]] = {}
_cache_lock = threading.Lock()


def _clean_query(query) -> str:
    return re.sub(r"\s+", " ", str(query or "")).strip()[:MAX_QUERY_CHARS]


def _media_host_ok(url: str) -> bool:
    """贴纸 CDN 地址必须是 GIPHY 域名（https）。"""
    try:
        parts = urllib.parse.urlsplit(str(url or ""))
    except ValueError:
        return False
    if parts.scheme != "https":
        return False
    host = (parts.hostname or "").lower()
    return any(host == s.lstrip(".") or host.endswith(s) for s in ALLOWED_MEDIA_SUFFIXES)


def _pick_image(images: dict) -> dict | None:
    """挑一张用于展示的贴纸：优先 original（最清晰），退回固定宽度变体。

    只取 https + GIPHY 域名的；宽高必须是正数，否则前端没法预占尺寸。
    """
    for key in ("original", "downsized", "fixed_width", "fixed_height"):
        item = images.get(key)
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        try:
            width = int(float(item.get("width") or 0))
            height = int(float(item.get("height") or 0))
        except (TypeError, ValueError):
            continue
        # 展示尺寸收一下：GIPHY 有的贴纸原图宽到 480，气泡里放不下
        if not _media_host_ok(url) or width <= 0 or height <= 0:
            continue
        return {"url": url, "width": width, "height": height}
    return None


def _call_api(query: str, count: int, api_key: str, proxy: str, timeout: float) -> list[dict]:
    params = urllib.parse.urlencode({
        "api_key": api_key, "q": query, "limit": count,
        "rating": "pg-13",          # 交易助手场景，不要过火内容
        "lang": "en",               # GIPHY 的标签体系以英文为主
    })
    request = urllib.request.Request(f"{GIPHY_URL}?{params}", headers=dict(HEADERS))
    response = _open(request, proxy, timeout)
    try:
        raw = response.read(2 * 1024 * 1024)
    finally:
        try:
            response.close()
        except Exception:
            pass
    import json
    data = json.loads(raw.decode("utf-8", "replace"))
    if not isinstance(data, dict):
        raise ValueError("返回格式异常")
    rows = data.get("data")
    if not isinstance(rows, list):
        raise ValueError("返回格式异常")
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        image = _pick_image(row.get("images") or {})
        if not image:
            continue
        out.append({
            "id": str(row.get("id") or "")[:64],
            "title": re.sub(r"\s+", " ", str(row.get("title") or "")).strip()[:200],
            "url": image["url"],
            "width": image["width"],
            "height": image["height"],
            "pageUrl": str(row.get("url") or "")[:400],
        })
    return out


def search_stickers(query, count: int = 1, api_key: str = "", proxy: str = "",
                    timeout: float = DEFAULT_TIMEOUT) -> dict:
    """按情绪词搜贴纸（永不抛异常）。

    返回 {"ok", "results":[{id,title,url,width,height,pageUrl}], "cached", "error"}。
    没配 key / 网络失败都只返回 ok=False，调用方据此回退内置表情。
    """
    cleaned = _clean_query(query)
    if not cleaned:
        return {"ok": False, "results": [], "cached": False, "error": "搜索词为空"}
    key = str(api_key or os.environ.get(KEY_ENV) or "").strip()
    if not key:
        return {"ok": False, "results": [], "cached": False,
                "error": f"未配置 {KEY_ENV}，无法联网找表情包"}
    try:
        limit = max(1, min(10, int(count)))
    except (TypeError, ValueError):
        limit = 1
    cache_key = (cleaned.lower(), limit)
    now = time.time()
    with _cache_lock:
        hit = _cache.get(cache_key)
        if hit and now - hit[0] < CACHE_TTL:
            return {"ok": True, "results": hit[1], "cached": True, "error": ""}
    try:
        results = _call_api(cleaned, limit, key, _clean_proxy(proxy),
                            float(timeout or DEFAULT_TIMEOUT))
    except Exception as exc:
        return {"ok": False, "results": [], "cached": False,
                "error": _short_error(exc)}
    if not results:
        return {"ok": False, "results": [], "cached": False, "error": "没有找到贴纸"}
    with _cache_lock:
        if len(_cache) >= CACHE_MAX:
            # 简单淘汰：清掉过期项；还满就丢最早的一半
            alive = {k: v for k, v in _cache.items() if now - v[0] < CACHE_TTL}
            if len(alive) >= CACHE_MAX:
                for k in list(alive)[: CACHE_MAX // 2]:
                    alive.pop(k, None)
            _cache.clear()
            _cache.update(alive)
        _cache[cache_key] = (now, results)
    return {"ok": True, "results": results, "cached": False, "error": ""}


def clear_cache() -> None:
    """测试/排障用：清空搜索结果缓存。"""
    with _cache_lock:
        _cache.clear()


MAX_IMAGE_BYTES = 4 * 1024 * 1024   # 单张贴纸上限：超过这个不是图，是别的东西


def download_image(url: str, proxy: str = "", timeout: float = DEFAULT_TIMEOUT
                   ) -> tuple[bytes, str]:
    """把贴纸图片下载到内存，返回 (bytes, content_type)。

    走 search 模块的 _open：同样有 DNS 校验、禁自动重定向、代理支持。
    落盘由调用方负责（ai/sticker_cache.py）——这样"下载"和"存哪"分开，
    测试下可以只断言下载，不碰磁盘。
    """
    if not _media_host_ok(url):
        raise ValueError("贴纸地址不在 GIPHY 域名下")
    request = urllib.request.Request(url, headers=dict(HEADERS))
    response = _open(request, proxy, timeout)
    try:
        raw = response.read(MAX_IMAGE_BYTES + 1)
        headers = getattr(response, "headers", None)
        content_type = ""
        if headers is not None:
            try:
                content_type = str(headers.get("Content-Type") or "")
            except Exception:
                content_type = ""
    finally:
        try:
            response.close()
        except Exception:
            pass
    if not raw:
        raise ValueError("贴纸内容为空")
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("贴纸文件过大")
    return bytes(raw), content_type
