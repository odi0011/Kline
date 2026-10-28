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
  · 相关性筛选：标题/摘要命中当前展示币种（BTC/ETH/…）或该币全名时优先保留；
    命中"加密/比特币/美联储"等宽泛关键词次之；筛选后按时间稳定升序展示。
  · 只保留最近 N 小时的内容，避免旧闻占据版面。
  · 全部走标准库（urllib/http.client、ElementTree、HTMLParser），不安装新依赖。
"""
from __future__ import annotations

import base64
import http.client
import ipaddress
import json
import queue
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

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


# 精确匹配，不允许任意子域；RSS 的文章落地页域名需显式列入。
ARTICLE_HOSTS = frozenset({
    "binance.com", "www.binance.com", "okx.com", "www.okx.com",
    "cointelegraph.com", "www.cointelegraph.com", "coindesk.com", "www.coindesk.com",
    "theblock.co", "www.theblock.co", "decrypt.co", "www.decrypt.co",
    "cryptoslate.com", "www.cryptoslate.com", "cn.investing.com",
    "feeds.a.dj.com", "wsj.com", "www.wsj.com", "forexlive.com", "www.forexlive.com",
})
ARTICLE_MAX_BYTES = 2 * 1024 * 1024
ARTICLE_MAX_REDIRECTS = 4
_DNS_SLOTS = threading.BoundedSemaphore(4)


class _ArticleError(ValueError):
    pass


def _validate_article_url(url: str) -> str:
    if not isinstance(url, str) or not url or re.search(r"[\s\\\x00-\x1f\x7f]", url):
        raise _ArticleError("不安全的新闻地址")
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port
    except ValueError as exc:
        raise _ArticleError("无效的新闻地址") from exc
    if (parts.scheme != "https" or parts.username is not None or parts.password is not None
            or port not in (None, 443) or host not in ARTICLE_HOSTS
            or parts.netloc.lower() not in (host, host + ":443")):
        raise _ArticleError("仅允许 HTTPS 可信新闻域名（不含凭据或自定义端口）")
    return host


def _public_addresses(host: str, timeout: float) -> list[str]:
    """有界 DNS 等待；所有答案必须公网，连接时固定 IP，避免二次解析/rebinding。"""
    if not _DNS_SLOTS.acquire(blocking=False):
        raise _ArticleError("DNS 查询繁忙，请稍后重试")
    result = queue.Queue(maxsize=1)

    def resolve():
        try:
            result.put(socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
        except OSError as exc:
            result.put(exc)
        finally:
            _DNS_SLOTS.release()

    threading.Thread(target=resolve, daemon=True).start()
    try:
        records = result.get(timeout=max(0.001, timeout))
    except queue.Empty as exc:
        raise _ArticleError("新闻域名解析超时") from exc
    if isinstance(records, Exception):
        raise _ArticleError("新闻域名解析失败")
    addresses = list(dict.fromkeys(record[4][0] for record in records))
    if not addresses:
        raise _ArticleError("新闻域名没有公网地址")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        # 也拒绝 IPv6 转换/隧道地址，防止嵌入内网 IPv4 绕过。
        if (not ip.is_global or ip.is_multicast or ip.is_reserved
                or (ip.version == 6 and (ip.ipv4_mapped or ip.sixtofour or ip.teredo))):
            raise _ArticleError("拒绝非公网新闻地址")
    return addresses


class _ArticleConnection(http.client.HTTPSConnection):
    """直连或经服务器配置的 HTTP CONNECT 代理；目标 IP 固定，TLS 校验原域名。"""
    def __init__(self, host, address, timeout, proxy=None):
        super().__init__(host, 443, timeout=timeout, context=ssl.create_default_context())
        self.address = address
        self.proxy_address = None
        if proxy:
            try:
                parsed = urlsplit(proxy)
                if parsed.scheme != "http" or not parsed.hostname or parsed.path not in ("", "/"):
                    raise ValueError
                self.proxy_address = (parsed.hostname, parsed.port or 80)
                headers = {"Host": host + ":443"}
                if parsed.username is not None:
                    from urllib.parse import unquote
                    auth = f"{unquote(parsed.username)}:{unquote(parsed.password or '')}"
                    headers["Proxy-Authorization"] = "Basic " + base64.b64encode(auth.encode()).decode()
                self.set_tunnel(address, 443, headers)
            except ValueError as exc:
                raise _ArticleError("正文抓取仅支持服务器配置的 HTTP CONNECT 代理") from exc

    def connect(self):
        self.sock = socket.create_connection(self.proxy_address or (self.address, 443), self.timeout)
        try:
            if self.proxy_address:
                self._tunnel()
            self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)
        except Exception:
            self.close()
            raise


def _request_article(url, host, address, proxy, timeout):
    """单跳请求，不自动跟随跳转、不继承环境代理、不发送 cookie。"""
    conn = _ArticleConnection(host, address, timeout, proxy)
    connected_socket = None

    def abort():
        sock = connected_socket or conn.sock
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    watchdog = threading.Timer(timeout, abort)
    watchdog.daemon = True
    watchdog.start()
    try:
        parts = urlsplit(url)
        path = (parts.path or "/") + ("?" + parts.query if parts.query else "")
        conn.request("GET", path, headers={"Host": host, "User-Agent": UA,
                     "Accept": "text/html,application/xhtml+xml", "Accept-Encoding": "identity"})
        connected_socket = conn.sock
        with conn.getresponse() as response:
            headers = response.headers
            if response.status in (301, 302, 303, 307, 308):
                return response.status, headers, b""
            if response.status != 200:
                raise _ArticleError(f"新闻源返回 HTTP {response.status}")
            if headers.get("Content-Encoding", "identity").lower() not in ("identity", ""):
                raise _ArticleError("新闻源返回不支持的压缩正文")
            if headers.get_content_type() not in ("text/html", "application/xhtml+xml"):
                raise _ArticleError("新闻源未返回 HTML 正文")
            length = headers.get("Content-Length")
            if length and (not length.isdigit() or int(length) > ARTICLE_MAX_BYTES):
                raise _ArticleError("新闻正文超过 2MB 或长度无效")
            raw = response.read(ARTICLE_MAX_BYTES + 1)
            if len(raw) > ARTICLE_MAX_BYTES:
                raise _ArticleError("新闻正文超过 2MB")
            return response.status, headers, raw
    finally:
        watchdog.cancel()
        conn.close()


class _ArticleParser(HTMLParser):
    SKIP = {"script", "style", "nav", "footer", "header", "aside", "form", "noscript", "svg", "template"}
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    BLOCK = {"p", "div", "section", "article", "main", "li", "br", "h1", "h2", "h3", "h4", "blockquote", "tr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.parts = []
        self.regions = []
        self.jsonld = []
        self.json_buffer = None

    def handle_starttag(self, tag, attrs):
        tag = tag.rsplit(":", 1)[-1]
        attrs = dict(attrs)
        if tag == "script" and attrs.get("type", "").split(";")[0].strip().lower() == "application/ld+json":
            self.json_buffer = []
        if tag in self.BLOCK:
            self.handle_data("\n")
        blocked = (tag in self.SKIP or "hidden" in attrs or attrs.get("aria-hidden") == "true"
                   or bool(self.stack and self.stack[-1][1]))
        if tag not in self.VOID:
            self.stack.append((tag, blocked))
        if tag in ("article", "main") and not blocked:
            self.regions.append({"tag": tag, "depth": len(self.stack), "parts": [], "open": True})

    def handle_startendtag(self, tag, attrs):
        tag = tag.rsplit(":", 1)[-1]
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.rsplit(":", 1)[-1]
        if tag == "script" and self.json_buffer is not None:
            self.jsonld.append("".join(self.json_buffer))
            self.json_buffer = None
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                for region in self.regions:
                    if region["depth"] > len(self.stack):
                        region["open"] = False
                break
        if tag in self.BLOCK:
            self.handle_data("\n")

    def handle_data(self, data):
        if self.json_buffer is not None:
            self.json_buffer.append(data)
        if self.stack and self.stack[-1][1]:
            return
        self.parts.append(data)
        for region in self.regions:
            if region["open"]:
                region["parts"].append(data)


def _clean_text(parts) -> str:
    text = "".join(parts).replace("\xa0", " ")
    return "\n\n".join(re.sub(r"[^\S\n]+", " ", line).strip()
                       for line in text.splitlines() if line.strip())


def _plain_text(html: str) -> str:
    parser = _ArticleParser()
    parser.feed(html or "")
    parser.close()
    return _clean_text(parser.parts)


def _extract_article(html: str) -> tuple[str, bool]:
    parser = _ArticleParser()
    parser.feed(html)
    parser.close()
    bodies = []
    paywall = False
    for raw in parser.jsonld:
        try:
            nodes = [json.loads(raw)]
        except (ValueError, RecursionError):
            continue
        while nodes:
            node = nodes.pop()
            if isinstance(node, dict):
                if node.get("isAccessibleForFree") in (False, "false", "False"):
                    paywall = True
                body = node.get("articleBody")
                if isinstance(body, str) and body.strip():
                    cleaned = _plain_text(body)
                    if cleaned:
                        bodies.append(cleaned)
                nodes.extend(node.values())
            elif isinstance(node, list):
                nodes.extend(node)
    if bodies:
        return max(bodies, key=len), paywall
    for tag in ("article", "main"):
        candidates = [_clean_text(region["parts"]) for region in parser.regions if region["tag"] == tag]
        if any(candidates):
            return max(candidates, key=len), paywall
    return "", paywall


def fetch_article(item: dict, proxy: str | None = None) -> dict:
    """读取服务器当前 news 中的 item，绝不能直接传入客户端 dict/任意 URL。

    hub 必须按 URL 匹配当前 news，再传入匹配到的服务器 item（含 RSS content）。
    ok 表示有可显示文本；partial=True 表示摘要/截断内容，error 说明限制。
    所有联网跳转均重新验证域名和全部 DNS 答案，最多 4 跳、总计 12 秒、2MB。
    """
    def result(content="", partial=True, error=""):
        return {"ok": bool(content), "content": content, "partial": partial, "error": error}

    if not isinstance(item, dict):
        return result(error="需要服务器已知的新闻条目")
    summary = _plain_text(str(item.get("desc") or "")[:ARTICLE_MAX_BYTES])
    try:
        url = item.get("url")
        _validate_article_url(url)
        # 仅信任服务器 RSS 解析器生成的标记；摘要字段从不冒充全文。
        full = item.get("content")
        if isinstance(full, str) and full.strip() and item.get("contentPartial") is False:
            encoded = full.encode("utf-8")
            limited = len(encoded) > ARTICLE_MAX_BYTES
            return result(encoded[:ARTICLE_MAX_BYTES].decode("utf-8", "ignore"), limited,
                          "RSS 正文超过 2MB，已截断" if limited else "")
        deadline = time.monotonic() + FETCH_TIMEOUT
        visited = set()
        for hop in range(ARTICLE_MAX_REDIRECTS + 1):
            if url in visited:
                raise _ArticleError("新闻地址重定向循环")
            visited.add(url)
            host = _validate_article_url(url)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _ArticleError("正文读取超时")
            address = _public_addresses(host, min(remaining, 3))[0]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _ArticleError("正文读取超时")
            status, headers, raw = _request_article(url, host, address, proxy, remaining)
            if time.monotonic() >= deadline:
                raise _ArticleError("正文读取超时")
            if status in (301, 302, 303, 307, 308):
                location = headers.get("Location", "")
                if not location or hop == ARTICLE_MAX_REDIRECTS:
                    raise _ArticleError("新闻地址重定向过多或缺少地址")
                # 原始 Location 也检查控制字符，避免 urljoin 静默剔除。
                if re.search(r"[\s\\\x00-\x1f\x7f]", location):
                    raise _ArticleError("不安全的新闻重定向")
                url = urljoin(url, location)
                continue
            charset = headers.get_content_charset() or "utf-8"
            try:
                html = raw.decode(charset, "replace")
            except LookupError:
                html = raw.decode("utf-8", "replace")
            content, partial = _extract_article(html)
            if not content:
                raise _ArticleError("未能提取正文（可能需要登录、订阅或动态加载）")
            compact = lambda text: re.sub(r"\s+", "", text)
            if summary and compact(content) in (compact(summary),
                    compact(str(item.get("title") or "")) + compact(summary)):
                partial = True
            return result(content, partial, "仅获取到摘要或受限正文，并非全文" if partial else "")
        raise _ArticleError("未能获取正文")
    except (_ArticleError, OSError, http.client.HTTPException, ValueError, RecursionError) as exc:
        message = str(exc) if isinstance(exc, _ArticleError) else "正文读取失败，请稍后重试"
        return result(summary, True, message + ("；仅显示 RSS 摘要，并非全文" if summary else ""))


def _parse_rss(xml: str) -> list[dict]:
    """RSS/Atom；摘要与 content:encoded 分开，全文不截成 400 字摘要。"""
    # 禁止实体声明，避免 XML 实体膨胀；ElementTree 不联网解析外部实体。
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", xml, re.I):
        return []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    items = []
    local = lambda tag: tag.rsplit("}", 1)[-1]
    for block in root.iter():
        if local(block.tag) not in ("item", "entry"):
            continue

        def pick(*names, exact=False):
            for name in names:
                for child in block:
                    if (child.tag if exact else local(child.tag)) == name:
                        return (child.text or "") + "".join(
                            ET.tostring(node, encoding="unicode") for node in child)
            return ""

        title = _plain_text(pick("title"))
        if not title:
            continue
        link = pick("link").strip()
        if not link:
            link = next((child.get("href", "") for child in block
                         if local(child.tag) == "link"
                         and child.get("rel", "alternate") == "alternate"), "")
        ts = 0
        for tag in ("pubDate", "published", "updated", "date"):
            raw = pick(tag).strip()
            if not raw:
                continue
            try:
                dt = parsedate_to_datetime(raw)
            except (TypeError, ValueError, OverflowError):
                try:
                    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except ValueError:
                    continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            ts = int(dt.timestamp() * 1000)
            break
        full = _plain_text(pick("{http://purl.org/rss/1.0/modules/content/}encoded",
                                "{http://www.w3.org/2005/Atom}content", "content", exact=True))
        desc = _plain_text(pick("description", "summary")) or full
        item = {"title": title, "url": link, "ts": ts or int(time.time() * 1000),
                "desc": desc[:400]}
        if full:
            item["content"] = full
            item["contentPartial"] = False
        items.append(item)
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
    """聚合全部来源，按相关性筛选后按时间稳定升序展示。

    shown_symbols: 前端当前展示的币种（如 ["btcusdt","ethusdt"]），
                   用于优先保留与该币相关的新闻。
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
    # 相关性用于筛选；展示始终稳定升序，最新消息在底部。
    return sorted(out[:limit_total], key=lambda x: x["ts"])
