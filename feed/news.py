"""新闻聚合：交易所公告 + 财经/加密媒体，按展示币种做相关性排序。

数据源（全部公开、无需 key、无需登录）：

  交易所公告
    币安  /bapi/composite/v1/public/cms/article/list/query    （中文）
    OKX   /api/v5/support/announcements                       （中文，需 Accept-Language）

  加密媒体（RSS）
    Cointelegraph   https://cointelegraph.com/rss
    CoinDesk        https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml
    Decrypt         https://decrypt.co/feed
    CryptoSlate     https://cryptoslate.com/feed/
    The Block       https://www.theblock.co/rss.xml

  综合财经（RSS，中文）
    英为财情 加密货币  https://cn.investing.com/rss/news_301.rss
    英为财情 外汇     https://cn.investing.com/rss/news_14.rss
    英为财情 商品     https://cn.investing.com/rss/news_25.rss
    Reuters/Bloomberg 类经 Yahoo Finance 备选：见 FINANCE_FEEDS

设计要点：
  · 每个源独立 try，一个挂掉不影响其余（真实环境里 RSS 站点经常抽风）。
  · 相关性排序：标题/摘要命中当前展示币种（BTC/ETH/…）或该币全名时置顶；
    命中"加密/比特币/美联储"等宽泛关键词次之；其余按时间。
  · 只保留最近 N 小时的内容，避免旧闻占据版面。
  · 全部走标准库（urllib + re），不引入 feedparser / requests 依赖。
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime

# 币安这个接口 pageSize 上限很小（>5 会 400 Bad Request），固定 5 并翻页凑数
BINANCE_URL = ("https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
               "?type=1&pageNo={page}&pageSize=5")
OKX_URL = "https://www.okx.com/api/v5/support/announcements?page=1"
# 用常规浏览器 UA：币安 WAF 会直接拒绝带 bot 标识的 UA
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
FETCH_TIMEOUT = 12
MAX_AGE_HOURS = 72          # 超过 72 小时的新闻不再展示

# RSS 源：(显示名, URL, 语言, 权重)
# 权重用于同相关性下的排序：交易所公告(100) > 主流加密媒体(60~80) > 综合财经(50)
RSS_FEEDS = [
    ("Cointelegraph", "https://cointelegraph.com/rss", "en", 78),
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml", "en", 76),
    ("The Block", "https://www.theblock.co/rss.xml", "en", 74),
    ("Decrypt", "https://decrypt.co/feed", "en", 68),
    ("CryptoSlate", "https://cryptoslate.com/feed/", "en", 60),
    ("英为财情·加密", "https://cn.investing.com/rss/news_301.rss", "zh", 66),
    ("英为财情·外汇", "https://cn.investing.com/rss/news_14.rss", "zh", 52),
    ("英为财情·商品", "https://cn.investing.com/rss/news_25.rss", "zh", 50),
    ("WSJ·市场", "https://feeds.a.dj.com/rss/RSSMarketsMain.xml", "en", 54),
    ("ForexLive", "https://www.forexlive.com/feed/news", "en", 48),
]

# 币种 → 标题里可能出现的别名（用于相关性打分，全小写）
# 注意：短别名（eth/ada/dot/sol）容易误命中英文单词片段，所以匹配时按词边界检查。
COIN_ALIASES = {
    "btc": ["btc", "bitcoin", "比特币"],
    "eth": ["eth", "ethereum", "以太坊", "以太币"],
    "bnb": ["bnb", "binance coin", "币安币"],
    "sol": ["sol", "solana"],
    "xrp": ["xrp", "ripple", "瑞波"],
    "doge": ["doge", "dogecoin", "狗狗币"],
    "ada": ["ada", "cardano"],
    "avax": ["avax", "avalanche"],
    "link": ["link", "chainlink"],
    "ltc": ["ltc", "litecoin", "莱特币"],
    "trx": ["trx", "tron", "波场"],
    "dot": ["dot", "polkadot"],
    "matic": ["matic", "polygon"],
    "ton": ["ton", "toncoin"],
    "tsla": ["tsla", "tesla", "特斯拉"],
    "xau": ["xau", "gold", "黄金"],
    "xag": ["xag", "silver", "白银"],
    "meta": ["meta platforms", "meta平台"],
    "aapl": ["aapl", "apple inc", "苹果公司"],
    "nvda": ["nvda", "nvidia", "英伟达"],
}
# 命中这些词说明是"大盘/宏观"新闻，与加密交易相关但不如具体币种精准
BROAD_WORDS = [
    "crypto", "cryptocurrency", "bitcoin", "ethereum", "blockchain", "defi",
    "stablecoin", "binance", "okx", "exchange", "etf", "sec", "fed", "fomc",
    "inflation", "cpi", "rate cut", "interest rate", "tariff",
    "加密", "数字货币", "区块链", "比特币", "以太坊", "美联储", "降息", "加息",
    "通胀", "关税", "监管", "稳定币", "现货 etf", "合约",
]


def _get_bytes(url: str, proxy: str | None = None, *, lang: bool = False) -> bytes | None:
    headers = {"User-Agent": UA, "Accept": "*/*"}
    if lang:
        headers["Accept-Language"] = "zh-CN,zh;q=0.9,en;q=0.8"
        headers["Referer"] = "https://www.okx.com/"
    req = urllib.request.Request(url, headers=headers)
    try:
        if proxy:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler(
                {"http": proxy, "https": proxy}))
            with opener.open(req, timeout=FETCH_TIMEOUT) as r:
                return r.read()
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
            return r.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return None


def _get_json(url: str, proxy: str | None = None, *, lang: bool = False) -> dict | None:
    raw = _get_bytes(url, proxy, lang=lang)
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None


def _strip_html(s: str) -> str:
    s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s or "", flags=re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"&nbsp;", " ", s)
    s = re.sub(r"&amp;", "&", s)
    s = re.sub(r"&lt;", "<", s)
    s = re.sub(r"&gt;", ">", s)
    s = re.sub(r"&quot;", '"', s)
    s = re.sub(r"&#39;|&apos;", "'", s)
    return re.sub(r"\s+", " ", s).strip()


def _parse_rss(xml: str) -> list[dict]:
    """极简 RSS/Atom 解析：不引第三方库，只取 item/entry 的标题、链接、时间、摘要。"""
    items = []
    # RSS 2.0 (<item>) 与 Atom (<entry>) 都支持
    blocks = re.findall(r"<item\b.*?</item>", xml, flags=re.S | re.I)
    if not blocks:
        blocks = re.findall(r"<entry\b.*?</entry>", xml, flags=re.S | re.I)
    for b in blocks:
        def pick(tag: str) -> str:
            m = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", b, flags=re.S | re.I)
            return _strip_html(m.group(1)) if m else ""
        title = pick("title")
        if not title:
            continue
        # link：RSS 是 <link>url</link>，Atom 是 <link href="url"/>
        link = pick("link")
        if not link:
            m = re.search(r'<link\b[^>]*href="([^"]+)"', b, flags=re.I)
            link = m.group(1) if m else ""
        # 时间：pubDate / published / updated / dc:date
        ts = 0
        for tag in ("pubDate", "published", "updated", "dc:date"):
            raw = pick(tag)
            if not raw:
                continue
            try:
                ts = int(parsedate_to_datetime(raw).timestamp() * 1000)
                break
            except Exception:
                try:  # ISO 8601 (Atom): 2026-10-27T10:00:00Z
                    iso = raw.replace("Z", "+00:00")
                    ts = int(time.mktime(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")) * 1000)
                    break
                except Exception:
                    continue
        if not ts:
            ts = int(time.time() * 1000)
        desc = pick("description") or pick("summary") or pick("content")
        items.append({"title": title, "url": link, "ts": ts, "desc": desc[:400]})
    return items


def fetch_binance(limit: int = 12, proxy: str | None = None) -> list[dict]:
    """币安公告（接口支持中文）。返回 [{source,title,url,ts,cat,weight}]，按时间倒序。

    pageSize 上限是 5（传 12 直接 400），所以按需翻页。
    """
    out = []
    pages = max(1, min(4, (limit + 4) // 5))
    for page in range(1, pages + 1):
        data = _get_json(BINANCE_URL.format(page=page), proxy)
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
                        "source": "币安", "lang": "zh", "weight": 100,
                        "cat": cat_name, "title": title,
                        "url": (f"https://www.binance.com/zh-CN/support/announcement/{code}"
                                if code else ""),
                        "ts": int(art.get("releaseDate") or 0),
                    })
        except Exception:
            break
    seen, uniq = set(), []
    for it in sorted(out, key=lambda x: -x["ts"]):
        k = it["url"] or it["title"]
        if k in seen:
            continue
        seen.add(k)
        uniq.append(it)
    return uniq[:limit]


def fetch_okx(limit: int = 12, proxy: str | None = None) -> list[dict]:
    """OKX 公告（加 Accept-Language 才会返回中文标题）。"""
    data = _get_json(OKX_URL, proxy, lang=True)
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
                    "source": "欧易", "lang": "zh", "weight": 96,
                    "cat": it.get("annType") or "", "title": title,
                    "url": it.get("url") or "",
                    "ts": int(it.get("pTime") or 0),
                })
    except Exception:
        return []
    out.sort(key=lambda x: -x["ts"])
    return out[:limit]


def fetch_rss(name: str, url: str, lang: str, weight: int,
              limit: int, proxy: str | None = None) -> list[dict]:
    """单个 RSS 源的抓取；失败返回空列表（不影响其它源）。"""
    raw = _get_bytes(url, proxy)
    if not raw:
        return []
    xml = raw.decode("utf-8", "replace")
    try:
        items = _parse_rss(xml)
    except Exception:
        return []
    for it in items:
        it.update({"source": name, "lang": lang, "weight": weight, "cat": "媒体"})
    return items[:limit]


def _score(item: dict, shown: list[str]) -> int:
    """相关性打分：命中的币种越具体分越高，宽泛宏观词次之。

    英文别名按词边界匹配（避免 "eth" 命中 "ether"/"method"、
    "sol" 命中 "solution" 之类的假阳性）；中文别名直接子串匹配。
    """
    text = f"{item.get('title','')} {item.get('desc','')}".lower()
    score = 0
    for sym in shown:
        base = re.sub(r"usdt$", "", sym.lower())
        for alias in COIN_ALIASES.get(base, [base]):
            if not alias:
                continue
            if alias.isascii() and alias.isalpha():
                if re.search(rf"\b{re.escape(alias)}\b", text):
                    score += 1000
                    break
            elif alias in text:
                score += 1000
                break
    if score == 0:
        for w in BROAD_WORDS:
            if w in text:
                score += 30
    return score


def fetch_all(limit_each: int = 12, proxy: str | None = None,
              shown_symbols: list[str] | None = None,
              limit_total: int = 60) -> list[dict]:
    """聚合全部来源，按「相关性 → 权重 → 时间」排序。

    shown_symbols: 前端当前展示的币种（如 ["btcusdt","ethusdt"]），
                   用于把与该币相关的新闻提到最前面。
    """
    shown = [s.lower() for s in (shown_symbols or [])]
    items: list[dict] = []
    items += fetch_binance(limit_each, proxy)
    items += fetch_okx(limit_each, proxy)
    for name, url, lang, weight in RSS_FEEDS:
        try:
            items += fetch_rss(name, url, lang, weight, limit_each, proxy)
        except Exception:
            continue

    # 去重（同标题或同链接）
    seen, uniq = set(), []
    for it in items:
        k = (it.get("url") or "").strip() or it.get("title", "")
        if not k or k in seen:
            continue
        seen.add(k)
        uniq.append(it)

    cutoff = int(time.time() * 1000) - MAX_AGE_HOURS * 3600_000
    now = int(time.time() * 1000)
    out = []
    for it in uniq:
        ts = int(it.get("ts") or 0)
        if ts and ts < cutoff:
            continue
        if ts > now + 3600_000:      # 源站时间异常（未来时间）→ 用当前时间兜底
            ts = now
        it["ts"] = ts or now
        it["rel"] = _score(it, shown)
        it["fetchedAt"] = now
        out.append(it)

    out.sort(key=lambda x: (-x["rel"], -int(x.get("weight") or 0), -x["ts"]))
    return out[:limit_total]
