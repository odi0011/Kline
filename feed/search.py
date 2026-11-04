"""联网搜索与网页读取（标准库实现，无第三方依赖）。

给 Agent 提供"模型自己决定要不要搜"的两项能力：

  · web_search(query, count, proxy, timeout)
      Serper（环境变量 KLINE_SERPER_KEY）→ DuckDuckGo HTML → Bing HTML 逐级回退；
      无 key 也能用（DDG 默认，失败自动回退 Bing）。
  · fetch_page(url, proxy, timeout, max_chars)
      下载网页并抽取 title / 正文；最大下载 512KB，正文截断到 max_chars。

设计要点：
  · **不抛异常**：任何失败都返回结构化 {"ok": False, "error": "<中文说明>"}，
    Agent 侧不需要 try/except，模型也能把失败原因读给用户。
  · **安全（SSRF）**：只允许 http/https；拒绝 localhost / 私网 / 链路本地 /
    保留地址 / 云元数据地址。**所有请求（搜索 provider、网页、重定向的每一跳）
    在连接前都先 socket.getaddrinfo 本地解析，把解析出的每个 IP 用 _ip_blocked
    校验，任一内网/保留地址就拒绝**——不能只校验 URL 字面主机名（通配 DNS 如
    *.nip.io 会把任意域名解析到 127.0.0.1 / 169.254.169.254）。
  · **防 DNS rebinding**：直连时固定连接"校验过的 IP"，Host 头 / TLS SNI 仍用
    原域名（_PinnedHTTPConnection / _PinnedHTTPSConnection），解析校验后到建连
    之间不再二次解析，消除 TOCTOU。
  · **残余风险（代理）**：配置了代理时目标 host 仍在本地解析+校验，但实际由
    代理再次解析并连接，代理侧 DNS 无法固定（HTTP 代理用绝对 URI、HTTPS 代理
    需代理自己 CONNECT）。这部分 TOCTOU 仍存在，只在代理场景，直连不受影响。
  · 重定向最多 3 跳且每跳重新走"解析→校验→固定 IP 连接"（urllib 默认会自动
    跟随，这里显式关掉自动跳转，由 fetch_page 逐跳校验）。
  · **零依赖**：urllib + http.client + ssl + html.parser + json + re，不安装第三方包。

实现细节：
  · 所有网络请求都走模块级 `_open(request, proxy, timeout)`，测试直接
    monkeypatch 它就能在完全不联网的情况下覆盖解析/回退/安全逻辑；
    monkeypatch `socket.getaddrinfo` / `_pinned_connection` 可验证 DNS 校验与
    固定 IP 连接。
  · 响应解码顺序：响应头 charset → utf-8 → gbk/gb18030 → utf-8 / errors="replace"。
  · 搜索结果的 URL 再做一次 http(s)+公网校验，防止搜索引擎返回内网链接。
"""
from __future__ import annotations

import html
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

# 普通浏览器 UA（搜索/站点会拒绝带 bot 标识的 UA）
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
SERPER_URL = "https://google.serper.dev/search"
DDG_URL = "https://html.duckduckgo.com/html/"
BING_URL = "https://www.bing.com/search"
# Yahoo 作为最后一档：线上实测（2026-11）DDG 返回 202 空页、Bing 对中文
# 查询经常 0 个 b_algo 块，两个都挂时搜索整块不可用，而 Yahoo 仍返回完整结果。
YAHOO_URL = "https://search.yahoo.com/search"
SERPER_KEY_ENV = "KLINE_SERPER_KEY"

MAX_QUERY_CHARS = 200
MAX_RESULTS = 10
MAX_PAGE_BYTES = 512 * 1024        # 网页最大下载 512KB
MAX_SEARCH_BYTES = 2 * 1024 * 1024  # 搜索结果页最大 2MB
MAX_REDIRECTS = 3
DEFAULT_TIMEOUT = 10.0
MAX_ERROR_CHARS = 200
MAX_URL_CHARS = 4096

_REDIRECT_CODES = (301, 302, 303, 307, 308)
# 明确列出的云元数据地址（私网/保留网段校验也会拦住，这里单列便于给出准确错误）
_METADATA_HOSTS = frozenset({
    "169.254.169.254", "169.254.170.2", "100.100.100.200",
    "fd00:ec2::254", "metadata.google.internal", "metadata.azure.internal",
})
_BLOCKED_HOSTS = frozenset({"localhost", "0.0.0.0"})
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal")
# 抽取正文时整段跳过的标签
_SKIP_TAGS = frozenset({"script", "style", "noscript", "svg", "template"})
# 这些标签前后补换行，避免多段文字粘成一句
_BLOCK_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4",
    "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
    "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
})
_VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input",
                        "link", "meta", "param", "source", "track", "wbr"})


class _UnsafeURLError(ValueError):
    """URL 未通过安全校验（非 http(s)/内网/元数据/带凭据等）。"""


class _DNSError(_UnsafeURLError):
    """DNS 解析本身失败（不等于解析到内网）。

    fetch_page 的"提前解析+校验"遇到它时不下结论，交给 _open 在连接前再解析
    一次给出权威结论（_open 内部同样会完整解析+校验+固定 IP）。
    """


class _ProviderError(RuntimeError):
    """搜索 provider 请求或解析失败（会触发下一级回退）。"""


# ---------- 基础工具 ----------

def _short_error(message) -> str:
    """错误信息统一截断：中文说明，长度不超过 200 字。"""
    text = re.sub(r"\s+", " ", str(message or "")).strip()
    return text[:MAX_ERROR_CHARS] or "未知错误"


def _clean_query(query) -> str:
    """搜索词清洗：非字符串/空 → ""；内部空白折叠；长度截到 200 字。"""
    if not isinstance(query, str):
        return ""
    text = re.sub(r"\s+", " ", query).strip()
    return text[:MAX_QUERY_CHARS]


def _clip_count(count) -> int:
    """结果条数夹到 1..10（坏值按默认 5 处理）。"""
    try:
        value = int(float(count))
    except (TypeError, ValueError):
        value = 5
    return max(1, min(MAX_RESULTS, value))


def _clean_proxy(proxy) -> str:
    return proxy.strip() if isinstance(proxy, str) and proxy.strip() else ""


def _clean_timeout(timeout) -> float:
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    if not (value > 0):
        return DEFAULT_TIMEOUT
    return max(0.5, min(30.0, value))


def _collapse_ws(text) -> str:
    """空白折叠：换行/制表/多空格/NBSP 全部压成单个空格。"""
    value = str(text or "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def _header_value(headers, name: str) -> str:
    """从 email.Message 或普通 dict 里大小写不敏感地取响应头。"""
    if headers is None:
        return ""
    try:
        value = headers.get(name)
    except Exception:
        value = None
    if value:
        return str(value)
    try:
        items = list(headers.items())
    except Exception:
        return ""
    lowered = name.lower()
    for key, value in items:
        if str(key).lower() == lowered:
            return str(value or "")
    return ""


def _decode_body(raw: bytes, headers=None) -> str:
    """按 响应头 charset → utf-8 → gbk/gb18030 → replace 的顺序解码。"""
    charset = ""
    ctype = _header_value(headers, "Content-Type")
    match = re.search(r"charset\s*=\s*[\"']?([\w.:-]+)", ctype, re.I)
    if match:
        charset = match.group(1).strip().lower()
    elif ctype:
        # 少数站点把 charset 写成 text/html;charset=gb2312（无空格/引号）
        match = re.search(r"charset=([\w.:-]+)", ctype, re.I)
        if match:
            charset = match.group(1).strip().lower()
    candidates = []
    if charset and charset not in ("iso-8859-1", "latin-1"):
        candidates.append(charset)
    candidates.extend(["utf-8", "gbk", "gb18030"])
    seen = set()
    for encoding in candidates:
        if encoding in seen:
            continue
        seen.add(encoding)
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _close_response(response) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _describe_error(exc: Exception) -> str:
    """把网络异常翻译成 <=200 字的中文说明（不泄露内部堆栈）。"""
    if isinstance(exc, _UnsafeURLError):
        return _short_error(str(exc))
    if isinstance(exc, urllib.error.HTTPError):
        return f"服务返回 HTTP {getattr(exc, 'code', '') or '未知'}"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "网络请求超时"
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", "")
        return _short_error(f"网络连接失败（{reason}）")
    return _short_error(f"网络请求失败（{type(exc).__name__}）")


# ---------- 安全校验（SSRF） ----------

def _ip_blocked(ip) -> bool:
    """判断 IP 是否属于不允许访问的范围（私网/回环/链路本地/保留/多播/未指定）。"""
    if ip.version == 6:
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            return _ip_blocked(mapped)
        if getattr(ip, "sixtofour", None) is not None or getattr(ip, "teredo", None) is not None:
            return True
    return (not ip.is_global) or ip.is_multicast or ip.is_reserved or ip.is_unspecified


def _url_host(parts) -> str:
    """URL 里解码后的主机名：先 unquote 再判内网，防止 %2e 等编码绕过。"""
    return urllib.parse.unquote((parts.hostname or "").lower()).strip(".")


def _resolve_public_ips(host: str) -> list[str]:
    """DNS 解析 host 并逐个校验 IP，返回去重后的公网 IP 列表。

    这是 SSRF 的关键一层：*所有* 请求（搜索 provider、网页、重定向的每一跳）
    在建立连接前都必须先解析并校验真实 IP——只校验 URL 字面主机名会被
    *.nip.io 这类通配 DNS（任意域名解析到 127.0.0.1 / 169.254.169.254）绕过。
    任一解析结果是内网/保留地址就整体拒绝（抛 _UnsafeURLError，调用方不得连接）；
    DNS 解析失败抛 _DNSError（_open 连接前会再解析一次，仍失败则结构化报错）。
    """
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise _DNSError("域名解析失败，无法访问该地址") from exc
    ips: list[str] = []
    for info in infos or []:
        address = str((info[4] or ("",))[0] or "")
        try:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError as exc:
            raise _UnsafeURLError("域名解析结果异常，已拒绝访问") from exc
        if _ip_blocked(ip):
            raise _UnsafeURLError("域名解析到内网或保留地址，已拒绝访问")
        text = str(ip)
        if text not in ips:
            ips.append(text)
    if not ips:
        raise _DNSError("域名没有可用的公网地址")
    return ips


def _validate_public_url(url: str) -> str:
    """校验 URL 只指向公网 http(s) 资源，返回去空白后的 URL；不安全抛 _UnsafeURLError。"""
    if not isinstance(url, str):
        raise _UnsafeURLError("网址必须是字符串")
    candidate = url.strip()
    if not candidate:
        raise _UnsafeURLError("网址为空")
    if len(candidate) > MAX_URL_CHARS:
        raise _UnsafeURLError("网址过长")
    if re.search(r"[\s\x00-\x1f\x7f\\]", candidate):
        raise _UnsafeURLError("网址包含非法字符")
    try:
        parts = urllib.parse.urlsplit(candidate)
        # 先 unquote 再判定：%2e 等编码形式不能绕过内网/IP 检查
        host = _url_host(parts)
        parts.port  # 端口非法（非数字/超范围）时抛 ValueError
    except ValueError as exc:
        raise _UnsafeURLError("网址格式无效") from exc
    if parts.scheme.lower() not in ("http", "https"):
        raise _UnsafeURLError("只允许 http/https 网址")
    if not host:
        raise _UnsafeURLError("网址缺少主机名")
    if parts.username is not None or parts.password is not None:
        raise _UnsafeURLError("网址不能包含用户名或密码")
    if host in _METADATA_HOSTS:
        raise _UnsafeURLError("拒绝访问云元数据地址")
    if host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_SUFFIXES):
        raise _UnsafeURLError("拒绝访问本机或内网域名")
    bare = host.split("%", 1)[0].strip(".")
    try:
        ip = ipaddress.ip_address(bare)
    except ValueError:
        ip = None
    if ip is not None:
        if _ip_blocked(ip):
            raise _UnsafeURLError("拒绝访问内网或保留地址")
    else:
        labels = [label for label in bare.split(".") if label]
        # 十进制/十六进制/八进制整数形式的 IP（如 http://2130706433/、
        # http://0x7f.0.0.1/）会被部分系统解析成内网地址，统一拒绝。
        if labels and all(re.fullmatch(r"(?:0[xX][0-9a-fA-F]+|\d+)", label)
                          for label in labels):
            raise _UnsafeURLError("拒绝访问可疑的纯数字主机名")
    return candidate


def _resolve_result_url(url) -> str:
    """搜索结果 URL：http(s)+公网校验通过才保留，否则返回 ""。"""
    try:
        return _validate_public_url(url)
    except _UnsafeURLError:
        return ""


# ---------- HTTP 层（测试 monkeypatch 这里） ----------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁止 urllib 自动跟随重定向：fetch_page 需要逐跳重新做安全校验。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """连接"校验过的 IP"，Host 头仍用原域名（防 DNS rebinding 的 TOCTOU）。"""

    def __init__(self, hostname: str, ip: str, port: int, timeout: float):
        super().__init__(hostname, port, timeout=timeout)
        self._pinned_ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._pinned_ip, self.port),
                                             self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """连接"校验过的 IP"，TLS SNI 与证书校验仍用原域名（证书校验默认开启）。"""

    def __init__(self, hostname: str, ip: str, port: int, timeout: float):
        super().__init__(hostname, port, timeout=timeout,
                         context=ssl.create_default_context())
        self._pinned_ip = ip

    def connect(self):
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        # 连接目标是校验后的 IP；SNI / 证书域名仍是原 hostname
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _pinned_connection(scheme: str, hostname: str, ip: str, port: int,
                       timeout: float):
    """按 scheme 造一个固定 IP 的连接（测试 monkeypatch 这里可断言连接目标）。"""
    if scheme == "https":
        return _PinnedHTTPSConnection(hostname, ip, port, timeout)
    return _PinnedHTTPConnection(hostname, ip, port, timeout)


class _PinnedResponse:
    """把 http.client 的响应包成 urllib 风格（read/status/headers/geturl/close）。"""

    def __init__(self, raw, connection, url: str):
        self._raw = raw
        self._connection = connection
        self._url = url
        self.status = int(getattr(raw, "status", 0) or 0)
        self.headers = getattr(raw, "headers", None)

    def read(self, amount=-1):
        return self._raw.read(amount)

    def geturl(self) -> str:
        return self._url

    def close(self):
        for target in (self._raw, self._connection):
            close = getattr(target, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass


def _open_pinned(request: urllib.request.Request, parts, host: str, ip: str,
                 timeout: float):
    """用校验过的 IP 建连；Host 头 / SNI 仍用原域名（消除 DNS rebinding TOCTOU）。"""
    scheme = parts.scheme.lower()
    port = parts.port or (443 if scheme == "https" else 80)
    connection = _pinned_connection(scheme, host, ip, port, timeout)
    path = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
    headers = {str(key): str(value) for key, value in request.header_items()}
    host_header = f"[{host}]" if ":" in host else host
    if port != (443 if scheme == "https" else 80):
        host_header = f"{host_header}:{port}"
    headers.setdefault("Host", host_header)
    try:
        connection.request(request.get_method(), path,
                           body=request.data, headers=headers)
        raw = connection.getresponse()
    except Exception:
        try:
            connection.close()
        except Exception:
            pass
        raise
    return _PinnedResponse(raw, connection, request.full_url)


def _open_via_proxy(request: urllib.request.Request, proxy: str, timeout: float):
    """经 HTTP(S) 代理请求（目标 host 已在 _open 里本地解析 + 校验）。

    残余风险（TOCTOU）：代理会再次解析目标域名并自行连接，代理侧 DNS 不受本
    进程控制，因此代理场景做不到"固定连接校验后的 IP"；直连路径没有这个问题
    （见 _open_pinned / _resolve_public_ips）。
    """
    opener = urllib.request.build_opener(
        _NoRedirect(),
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return opener.open(request, timeout=timeout)


def _open(request: urllib.request.Request, proxy: str = "",
          timeout: float = DEFAULT_TIMEOUT):
    """发起一次 HTTP 请求并返回原始 response（**不跟随重定向**）。

    安全链路：URL 语法/字面 IP 校验 → getaddrinfo 本地解析 → 每个 IP 校验 →
    直连时固定用校验过的 IP 建连（Host/SNI 用原域名）。fetch_page 对重定向的
    每一跳都会重新调用本函数，所以每一跳都会重新解析+校验。

    单独抽成模块级函数：单元测试 monkeypatch 它即可完全离线。
    """
    target = _validate_public_url(request.full_url)
    parts = urllib.parse.urlsplit(target)
    host = _url_host(parts)
    ips = _resolve_public_ips(host)
    proxy_value = _clean_proxy(proxy)
    timeout_value = _clean_timeout(timeout)
    if proxy_value:
        return _open_via_proxy(request, proxy_value, timeout_value)
    return _open_pinned(request, parts, host, ips[0], timeout_value)


def _fetch_bytes(request: urllib.request.Request, proxy: str, timeout: float,
                 limit: int, retries: int = 0) -> tuple[bytes, object]:
    """发请求并读取最多 limit 字节，返回 (bytes, headers)。失败抛 _ProviderError。

    retries：5xx 是搜索站点的瞬时限流，隔一下重试一次通常就过。
    线上实测：Yahoo 连续请求几次后开始 500，几秒后同一请求恢复 200。
    """
    try:
        response = _open(request, proxy, timeout)
    except Exception as exc:
        raise _ProviderError(_describe_error(exc)) from exc
    try:
        status = int(getattr(response, "status", 0) or 0)
        if status >= 300:
            if 500 <= status < 600 and retries > 0:
                _close_response(response)
                time.sleep(1.2)
                return _fetch_bytes(request, proxy, timeout, limit, retries - 1)
            raise _ProviderError(f"服务返回 HTTP {status}")
        raw = response.read(limit + 1)
        headers = getattr(response, "headers", None)
    except _ProviderError:
        raise
    except Exception as exc:
        raise _ProviderError(_describe_error(exc)) from exc
    finally:
        _close_response(response)
    if raw is None:
        raw = b""
    if not isinstance(raw, (bytes, bytearray)):
        raise _ProviderError("服务返回了无法识别的内容")
    return bytes(raw)[:limit], headers


def _fetch_text(request: urllib.request.Request, proxy: str, timeout: float,
                limit: int = MAX_SEARCH_BYTES, retries: int = 0) -> str:
    raw, headers = _fetch_bytes(request, proxy, timeout, limit, retries)
    return _decode_body(raw, headers)


def _fetch_json(request: urllib.request.Request, proxy: str,
                timeout: float) -> dict:
    text = _fetch_text(request, proxy, timeout, MAX_SEARCH_BYTES)
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise _ProviderError("服务返回的不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise _ProviderError("服务返回的数据格式异常")
    return data


# ---------- 搜索结果解析 ----------

def _new_result(title, url, snippet) -> dict | None:
    """规格化一条搜索结果；URL 不合法/非公网返回 None。"""
    resolved = _resolve_result_url(url)
    if not resolved:
        return None
    title = _collapse_ws(title)
    snippet = _collapse_ws(snippet)
    if not title:
        title = resolved
    return {"title": title[:300], "url": resolved, "snippet": snippet[:600]}


def _dedupe_results(results: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for item in results:
        if not item:
            continue
        url = item.get("url") or ""
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(item)
    return out


def _ddg_real_url(href) -> str:
    """DDG 结果链接可能是 //duckduckgo.com/l/?uddg=<encoded>，还原真实 URL。"""
    raw = html.unescape(str(href or "")).strip()
    if raw.startswith("//"):
        raw = "https:" + raw
    if not raw:
        return ""
    try:
        parts = urllib.parse.urlsplit(raw)
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    if host.endswith("duckduckgo.com") and parts.path.startswith("/l/"):
        target = ""
        for pair in parts.query.split("&"):
            if pair.startswith("uddg="):
                target = pair[len("uddg="):]
                break
        if target:
            return html.unescape(urllib.parse.unquote(target))
    return raw


def _feed_parser(parser: HTMLParser, text: str) -> None:
    try:
        parser.feed(text or "")
        parser.close()
    except Exception:       # HTMLParser 对畸形 HTML 偶有异常，解析尽量不中断
        pass


class _DuckDuckGoParser(HTMLParser):
    """提取 DDG HTML 页里的 result__a（标题+链接）与 result__snippet（摘要）。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.titles: list[tuple[str, str]] = []
        self.snippets: list[str] = []
        self._capture: dict | None = None

    def handle_starttag(self, tag, attrs):
        attrs = {str(name).lower(): (value or "") for name, value in attrs}
        if self._capture is not None:
            if tag not in _VOID_TAGS:
                self._capture["depth"] += 1
            return
        classes = attrs.get("class") or ""
        if tag == "a" and "result__a" in classes:
            self._capture = {"kind": "title", "depth": 0,
                             "href": attrs.get("href") or "", "text": []}
        elif "result__snippet" in classes:
            self._capture = {"kind": "snippet", "depth": 0, "href": "", "text": []}

    def handle_startendtag(self, tag, attrs):
        if tag not in _VOID_TAGS:
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        capture = self._capture
        if capture is None or tag in _VOID_TAGS:
            return
        if capture["depth"] <= 0:
            self._capture = None
            if capture["kind"] == "title":
                self.titles.append((capture["href"], "".join(capture["text"])))
            else:
                self.snippets.append("".join(capture["text"]))
        else:
            capture["depth"] -= 1

    def handle_data(self, data):
        if self._capture is not None and data:
            self._capture["text"].append(data)


class _BingParser(HTMLParser):
    """提取 Bing 结果块（li.b_algo）里的首个链接与摘要段落。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results: list[dict] = []
        self._block: dict | None = None

    def handle_starttag(self, tag, attrs):
        attrs = {str(name).lower(): (value or "") for name, value in attrs}
        classes = attrs.get("class") or ""
        if self._block is None:
            if tag == "li" and "b_algo" in classes:
                self._block = {"depth": 0, "href": "", "title": [],
                               "snippet": [], "captures": []}
            return
        block = self._block
        if tag not in _VOID_TAGS:
            block["depth"] += 1
            for capture in block["captures"]:
                capture["depth"] += 1
        if tag == "a" and not block["href"]:
            block["captures"].append({"kind": "title", "depth": 0,
                                      "href": attrs.get("href") or "", "text": []})
        elif tag == "p" and not block["snippet"] and not any(
                capture["kind"] == "snippet" for capture in block["captures"]):
            block["captures"].append({"kind": "snippet", "depth": 0,
                                      "href": "", "text": []})

    def handle_startendtag(self, tag, attrs):
        if tag not in _VOID_TAGS:
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        block = self._block
        if block is None or tag in _VOID_TAGS:
            return
        if block["captures"]:
            capture = block["captures"][-1]
            if capture["depth"] > 0:
                capture["depth"] -= 1
                block["depth"] -= 1
                return
            block["captures"].pop()
            if capture["kind"] == "title":
                if not block["href"]:
                    block["href"] = capture["href"]
                block["title"] = capture["text"]
            else:
                block["snippet"] = capture["text"]
        block["depth"] -= 1
        if tag == "li" and block["depth"] <= 0:
            href = _collapse_ws(block["href"])
            title = _collapse_ws("".join(block["title"]))
            snippet = _collapse_ws("".join(block["snippet"]))
            self._block = None
            if href:
                self.results.append({"href": href, "title": title, "snippet": snippet})

    def handle_data(self, data):
        block = self._block
        if block is not None and block["captures"] and data:
            block["captures"][-1]["text"].append(data)


class _YahooParser(HTMLParser):
    """提取 Yahoo 结果块（div.dd.algo）里 h3 里的链接 + p 摘要。

    Yahoo 的链接是 r.search.yahoo.com 包装的跳转，真实地址在
    `/RU=<urlencoded>/`，由 _yahoo_real_url 还原。

    块边界按**同层 div 数**算：块内还有好几层 div（compTitle/compText），
    用总标签深度会在第一个内层 div 闭合时就误判块结束，摘要就丢了。
    """

    _BLOCK_CLASS = re.compile(r"(?:^|\s)algo(?:\s|$)")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results: list[dict] = []
        self._block: dict | None = None
        self._divs = 0
        self._in_title = False
        self._in_snippet = False

    def handle_starttag(self, tag, attrs):
        attrs = {str(n).lower(): (v or "") for n, v in attrs}
        classes = attrs.get("class") or ""
        if self._block is None:
            if (tag == "div" and "relsrch" in classes
                    and self._BLOCK_CLASS.search(classes)):
                self._block = {"href": "", "title": [], "snippet": []}
                self._divs = 1
            return
        if tag == "div":
            self._divs += 1
        elif tag == "a" and not self._block["href"]:
            href = attrs.get("href") or ""
            if "/RU=" in href or href.startswith("http"):
                self._block["href"] = href
                self._in_title = True
        elif tag == "p" and not self._block["snippet"]:
            self._in_snippet = True

    def handle_startendtag(self, tag, attrs):
        if tag not in _VOID_TAGS:
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if self._block is None:
            return
        if tag == "h3":
            self._in_title = False
        elif tag == "p":
            self._in_snippet = False
        elif tag == "div":
            self._divs -= 1
            if self._divs <= 0:
                block = self._block
                self._block = None
                self._in_title = self._in_snippet = False
                href = _collapse_ws(block["href"])
                if href:
                    self.results.append({
                        "href": href,
                        "title": _collapse_ws("".join(block["title"])),
                        "snippet": _collapse_ws("".join(block["snippet"]))})

    def handle_data(self, data):
        if self._block is None or not data:
            return
        if self._in_title:
            self._block["title"].append(data)
        elif self._in_snippet:
            self._block["snippet"].append(data)


def _yahoo_real_url(href) -> str:
    """Yahoo 结果链接是 r.search.yahoo.com/.../RU=<encoded>/...，还原真实 URL。"""
    raw = html.unescape(str(href or "")).strip()
    if not raw:
        return ""
    match = re.search(r"/RU=([^/]+)/", raw)
    if not match:
        return raw if raw.startswith("http") else ""
    decoded = urllib.parse.unquote(match.group(1))
    # Yahoo 的 RU 段有时把 % 再转义一次（%253A），再解一层
    if "%" in decoded:
        decoded = urllib.parse.unquote(decoded)
    return decoded


# ---------- 搜索 provider ----------

def _search_serper(query: str, count: int, proxy: str, timeout: float,
                   key: str) -> list[dict]:
    payload = json.dumps({"q": query, "num": count, "hl": "zh-cn"}).encode("utf-8")
    request = urllib.request.Request(
        SERPER_URL, data=payload, method="POST",
        headers={**HEADERS, "Content-Type": "application/json",
                 "Accept": "application/json", "X-API-KEY": key})
    data = _fetch_json(request, proxy, timeout)
    organic = data.get("organic")
    if not isinstance(organic, list):
        raise _ProviderError("Serper 返回的数据里没有 organic 结果")
    results = []
    for item in organic:
        if not isinstance(item, dict):
            continue
        results.append(_new_result(item.get("title"), item.get("link"),
                                   item.get("snippet")))
    return _dedupe_results(results)[:count]


def _search_duckduckgo(query: str, count: int, proxy: str,
                       timeout: float) -> list[dict]:
    url = DDG_URL + "?" + urllib.parse.urlencode({"q": query})
    request = urllib.request.Request(url, headers=dict(HEADERS))
    text = _fetch_text(request, proxy, timeout, MAX_SEARCH_BYTES)
    parser = _DuckDuckGoParser()
    _feed_parser(parser, text)
    results = []
    for index, (href, title) in enumerate(parser.titles):
        snippet = parser.snippets[index] if index < len(parser.snippets) else ""
        results.append(_new_result(title, _ddg_real_url(href), snippet))
    return _dedupe_results(results)[:count]


def _search_bing(query: str, count: int, proxy: str, timeout: float) -> list[dict]:
    url = BING_URL + "?" + urllib.parse.urlencode({"q": query, "setlang": "zh-CN"})
    request = urllib.request.Request(url, headers=dict(HEADERS))
    text = _fetch_text(request, proxy, timeout, MAX_SEARCH_BYTES)
    parser = _BingParser()
    _feed_parser(parser, text)
    results = [_new_result(item.get("title"), item.get("href"),
                           item.get("snippet")) for item in parser.results]
    return _dedupe_results(results)[:count]


def _search_yahoo(query: str, count: int, proxy: str, timeout: float) -> list[dict]:
    url = YAHOO_URL + "?" + urllib.parse.urlencode({"p": query})
    request = urllib.request.Request(url, headers=dict(HEADERS))
    # 搜索站点会瞬时限流 5xx；重试一次基本就过
    text = _fetch_text(request, proxy, timeout, MAX_SEARCH_BYTES, retries=1)
    parser = _YahooParser()
    _feed_parser(parser, text)
    results = [_new_result(item.get("title"), _yahoo_real_url(item.get("href")),
                           item.get("snippet")) for item in parser.results]
    return _dedupe_results(results)[:count]


def _search_ok(out: dict, provider: str, results: list[dict]) -> dict:
    out.update({"ok": True, "provider": provider, "results": results,
                "count": len(results), "error": ""})
    return out


def _web_search_impl(query, count, proxy, timeout) -> dict:
    out = {"ok": False, "provider": "", "query": "", "results": [],
           "count": 0, "error": ""}
    cleaned = _clean_query(query)
    if not cleaned:
        out["error"] = "搜索词为空（需要 1~200 字）"
        return out
    out["query"] = cleaned
    limit = _clip_count(count)
    proxy_value = _clean_proxy(proxy)
    timeout_value = _clean_timeout(timeout)
    errors: list[str] = []

    key = ""
    if isinstance(os.environ.get(SERPER_KEY_ENV), str):
        key = os.environ.get(SERPER_KEY_ENV, "").strip()
    if key:
        try:
            results = _search_serper(cleaned, limit, proxy_value, timeout_value, key)
        except Exception as exc:
            results = []
            errors.append("serper: " + _short_error(exc))
        if results:
            return _search_ok(out, "serper", results)
        if not errors:
            errors.append("serper: 无结果")

    # Yahoo 放在 Bing 前面：线上实测 Bing 对中文查询会返回与主题无关的
    # 结果（查 FOMC 给 current.com / 剑桥词典），有结果就会截停回退链，
    # 等于用垃圾结果冒充搜索成功。DDG 被限流(202)时 Yahoo 才是可靠的。
    for provider, search_fn in (("duckduckgo", _search_duckduckgo),
                                ("yahoo", _search_yahoo),
                                ("bing", _search_bing)):
        try:
            results = search_fn(cleaned, limit, proxy_value, timeout_value)
        except Exception as exc:
            errors.append(f"{provider}: {_short_error(exc)}")
            continue
        if results:
            return _search_ok(out, provider, results)
        errors.append(f"{provider}: 无结果")

    out["error"] = _short_error("搜索失败：" + "；".join(errors) if errors else "搜索失败")
    return out


def web_search(query: str, count: int = 5, proxy: str = "",
               timeout: float = 10.0) -> dict:
    """联网搜索，返回结构化结果（永不抛异常）。

    返回：
      {"ok": bool, "provider": "serper"|"duckduckgo"|"bing"|"",
       "query": str, "results": [{"title","url","snippet"}],
       "count": int, "error": str}

    provider 顺序：Serper（配置 KLINE_SERPER_KEY 时）→ DuckDuckGo HTML → Bing。
    任一 provider 成功且结果非空即返回；全部失败时 ok=False 并在 error 汇总原因。
    """
    try:
        return _web_search_impl(query, count, proxy, timeout)
    except Exception as exc:      # 兜底：公共 API 绝不向外抛异常
        return {"ok": False, "provider": "", "query": _clean_query(query),
                "results": [], "count": 0,
                "error": _short_error(f"搜索失败（{type(exc).__name__}）")}


# ---------- 网页读取 ----------

class _PageParser(HTMLParser):
    """收集 <title> 与正文：跳过脚本/样式等，块级标签处补换行。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.body_parts: list[str] = []
        self._in_title = False
        self._skip_depth = 0
        self._saw_body = False
        self._in_body = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "title":
            self._in_title = True
            return
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "body":
            self._saw_body = True
            self._in_body = True
            return
        if tag in _BLOCK_TAGS:
            self.body_parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag.lower() in ("br", "hr"):
            self.body_parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
            return
        if tag in _SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if tag == "body":
            self._in_body = False
            return
        if tag in _BLOCK_TAGS:
            self.body_parts.append("\n")

    def handle_data(self, data):
        if not data:
            return
        if self._in_title:
            self.title_parts.append(data)
            return
        if self._skip_depth:
            return
        if self._saw_body and not self._in_body:
            return
        self.body_parts.append(data)


def _redirect_target(base: str, location: str) -> tuple[str, str]:
    """解析并校验重定向目标；返回 (新URL, "") 或 ("", 错误说明)。"""
    if not isinstance(location, str) or not location.strip():
        return "", "重定向地址为空"
    try:
        candidate = urllib.parse.urljoin(base, location.strip())
    except ValueError:
        return "", "重定向地址无效"
    try:
        target = _validate_public_url(candidate)
    except _UnsafeURLError as exc:
        return "", f"重定向目标不安全：{exc}"
    return target, ""


def _fetch_page_impl(url, proxy, timeout, max_chars) -> dict:
    original = url.strip() if isinstance(url, str) else ""
    out = {"ok": False, "url": original, "finalUrl": "", "title": "",
           "text": "", "truncated": False, "error": ""}
    try:
        current = _validate_public_url(original)
    except _UnsafeURLError as exc:
        out["error"] = _short_error(exc)
        return out
    proxy_value = _clean_proxy(proxy)
    timeout_value = _clean_timeout(timeout)
    try:
        char_limit = int(float(max_chars))
    except (TypeError, ValueError):
        char_limit = 8000
    char_limit = max(0, min(200_000, char_limit))

    redirects = 0
    while True:
        # 每一跳都先做一次"提前"的 DNS 解析+校验：解析到内网/保留地址时在发起
        # 请求前就拒绝（可测试 _open 未被调用）。解析本身失败（_DNSError）不在这里
        # 下结论，交给 _open 在连接前再解析一次并给出权威结论——_open 内部的
        # "解析+固定 IP 连接"才是防 rebinding 的关键，这一步只是尽早失败。
        try:
            _resolve_public_ips(_url_host(urllib.parse.urlsplit(current)))
        except _DNSError:
            pass
        except _UnsafeURLError as exc:
            out["finalUrl"] = current
            out["error"] = _short_error(exc)
            return out
        request = urllib.request.Request(current, headers=dict(HEADERS))
        try:
            response = _open(request, proxy_value, timeout_value)
        except urllib.error.HTTPError as exc:
            code = int(getattr(exc, "code", 0) or 0)
            location = _header_value(getattr(exc, "headers", None), "Location")
            _close_response(exc)
            out["finalUrl"] = current
            if code in _REDIRECT_CODES and location:
                redirects += 1
                if redirects > MAX_REDIRECTS:
                    out["error"] = "重定向次数过多（最多 3 跳）"
                    return out
                target, why = _redirect_target(current, location)
                if not target:
                    out["error"] = _short_error(why)
                    return out
                current = target
                continue
            out["error"] = _short_error(f"网页请求失败（HTTP {code or '未知'}）")
            return out
        except Exception as exc:
            out["finalUrl"] = current
            out["error"] = _short_error(_describe_error(exc))
            return out

        try:
            status = int(getattr(response, "status", 0) or 0)
            location = _header_value(getattr(response, "headers", None), "Location")
            if status in _REDIRECT_CODES and location:
                redirects += 1
                if redirects > MAX_REDIRECTS:
                    out["finalUrl"] = current
                    out["error"] = "重定向次数过多（最多 3 跳）"
                    return out
                target, why = _redirect_target(current, location)
                if not target:
                    out["finalUrl"] = current
                    out["error"] = _short_error(why)
                    return out
                current = target
                continue
            if status >= 400:
                out["finalUrl"] = current
                out["error"] = _short_error(f"网页请求失败（HTTP {status}）")
                return out
            raw = response.read(MAX_PAGE_BYTES + 1)
            headers = getattr(response, "headers", None)
            final_url = str(getattr(response, "geturl", lambda: current)() or current)
        except Exception as exc:
            out["finalUrl"] = current
            out["error"] = _short_error(_describe_error(exc))
            return out
        finally:
            _close_response(response)

        if raw is None:
            raw = b""
        if not isinstance(raw, (bytes, bytearray)):
            out["finalUrl"] = current
            out["error"] = "网页响应格式异常"
            return out
        raw = bytes(raw)
        truncated = len(raw) > MAX_PAGE_BYTES
        if truncated:
            raw = raw[:MAX_PAGE_BYTES]
        text = _decode_body(raw, headers)
        parser = _PageParser()
        _feed_parser(parser, text)
        title = _collapse_ws("".join(parser.title_parts))
        body = _collapse_ws("".join(parser.body_parts))
        if char_limit and len(body) > char_limit:
            body = body[:char_limit]
            truncated = True
        out.update({"ok": True, "finalUrl": final_url or current,
                    "title": title, "text": body,
                    "truncated": bool(truncated), "error": ""})
        return out


def fetch_page(url: str, proxy: str = "", timeout: float = 10.0,
               max_chars: int = 8000) -> dict:
    """下载网页并抽取 title / 正文（永不抛异常）。

    返回：
      {"ok": bool, "url": str, "finalUrl": str, "title": str,
       "text": str, "truncated": bool, "error": str}

    安全限制：只允许 http/https 公网地址；拒绝 localhost/私网/链路本地/云元数据；
    每个请求（含重定向每一跳）连接前都本地 DNS 解析并校验真实 IP（通配 DNS /
    nip.io 不能绕过）；直连时固定连接校验后的 IP（Host/SNI 仍用原域名），
    消除 DNS rebinding TOCTOU；代理场景目标 host 仍本地校验，但代理侧会再次
    解析（残余 TOCTOU，见 _open_via_proxy 注释）；重定向最多 3 跳；最大下载
    512KB；正文截断到 max_chars。
    """
    try:
        return _fetch_page_impl(url, proxy, timeout, max_chars)
    except Exception as exc:      # 兜底：公共 API 绝不向外抛异常
        original = url.strip() if isinstance(url, str) else ""
        return {"ok": False, "url": original, "finalUrl": "", "title": "",
                "text": "", "truncated": False,
                "error": _short_error(f"网页读取失败（{type(exc).__name__}）")}
