"""feed/search.py：无依赖联网搜索 / 网页读取 / SSRF 防护测试（全程不联网）。"""
from __future__ import annotations

import email.message
import json
import socket
import urllib.error
import urllib.parse
import urllib.request

import pytest

from feed import search

# ---------- 离线 HTTP 替身 ----------

class FakeResponse:
    def __init__(self, body=b"", status=200, headers=None, url=""):
        self._body = body if isinstance(body, bytes) else str(body).encode("utf-8")
        self.status = int(status)
        self.headers = ({"Content-Type": "text/html; charset=utf-8"}
                        if headers is None else headers)
        self._url = url
        self.closed = False

    def read(self, amount=-1):
        if amount is None or amount < 0:
            return self._body
        return self._body[:amount]

    def geturl(self):
        return self._url

    def close(self):
        self.closed = True


def _html_headers(ctype="text/html; charset=utf-8"):
    return {"Content-Type": ctype}


def _routes_open(routes: dict):
    """按 URL 关键字路由的假 _open：routes = {"serper"/"duckduckgo"/"bing": bytes|Exception|callable}。"""
    calls = []

    def fake_open(request, proxy="", timeout=search.DEFAULT_TIMEOUT):
        url = request.get_full_url()
        calls.append({"url": url, "request": request, "proxy": proxy,
                      "timeout": timeout})
        for marker, value in routes.items():
            if marker in url:
                if isinstance(value, Exception):
                    raise value
                if callable(value):
                    return value(request)
                return FakeResponse(value)
        raise AssertionError(f"未预期的请求: {url}")

    return fake_open, calls


def _sequence_open(*items):
    """按调用顺序返回响应的假 _open（用于重定向/多跳测试）。"""
    calls = []

    def fake_open(request, proxy="", timeout=search.DEFAULT_TIMEOUT):
        index = len(calls)
        calls.append(request.get_full_url())
        item = items[min(index, len(items) - 1)]
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(request)
        return item

    return fake_open, calls


def _http_error(code=302, location="", url="https://example.com/start"):
    headers = email.message.Message()
    if location:
        headers["Location"] = location
    return urllib.error.HTTPError(url, code, "redirect", headers, None)


class _FakeRawResponse:
    """http.client 原始响应的替身（_PinnedResponse 只需 status/headers/read/close）。"""

    def __init__(self, body=b"", status=200, headers=None):
        self.status = int(status)
        self.headers = headers or {"Content-Type": "text/html; charset=utf-8"}
        self._body = body if isinstance(body, bytes) else str(body).encode("utf-8")

    def read(self, amount=-1):
        if amount is None or amount < 0:
            return self._body
        return self._body[:amount]

    def close(self):
        pass


class _FakePinnedConnection:
    """固定 IP 连接替身：记录 request 参数，getresponse 返回给定原始响应。"""

    def __init__(self, raw=None, captured=None):
        self._raw = raw if raw is not None else _FakeRawResponse(PAGE_HTML.encode("utf-8"))
        self.captured = captured if captured is not None else {}

    def request(self, method, path, body=None, headers=None):
        self.captured.update(
            method=method, path=path, body=body,
            headers={str(key).lower(): str(value)
                     for key, value in (headers or {}).items()})

    def getresponse(self):
        return self._raw

    def close(self):
        pass


def _fake_connection(raw=None, captured=None):
    return _FakePinnedConnection(raw=raw, captured=captured)


def _ddg_html(count: int) -> str:
    parts = ["<html><body>"]
    for index in range(count):
        parts.append(
            f'<div class="result"><a class="result__a" '
            f'href="https://example.com/{index}">标题 {index}</a>'
            f'<a class="result__snippet">摘要 {index}</a></div>')
    parts.append("</body></html>")
    return "".join(parts)


DDG_HTML = """<html><body>
<div class="result results_links">
  <h2 class="result__title"><a rel="nofollow" class="result__a"
    href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fbtc%3Fa%3D1%26b%3D2&amp;rut=xyz">Bitcoin &amp; ETF 新闻</a></h2>
  <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fbtc">比特币 &amp; ETF 的最新消息</a>
</div>
<div class="result results_links">
  <h2><a class="result__a" href="https://other.example.org/page">Second &amp; Result</a></h2>
  <a class="result__snippet">第二 &amp; 结果</a>
</div>
<div class="result results_links">
  <a class="result__a" href="https://example.com/btc?a=1&b=2">Duplicate</a>
  <a class="result__snippet">重复链接</a>
</div>
<div class="result results_links">
  <a class="result__a" href="javascript:alert(1)">Bad Scheme</a>
  <a class="result__snippet">非 http</a>
</div>
<div class="result results_links">
  <a class="result__a" href="http://127.0.0.1/admin">Internal</a>
  <a class="result__snippet">内网地址</a>
</div>
</body></html>"""

YAHOO_HTML = """<html><body>
<div class="dd fst algo algo-sr relsrch Sr">
  <div class="compTitle"><a href="https://r.search.yahoo.com/_ylt=A2/RV=2/RU=https%3a%2f%2fnews.example.com%2Fa/RK=2/RS=x">
    <div class="thmb"><img src="x"/></div>
    <h3 class="title"><span>Yahoo Title One</span></h3></a></div>
  <div class="compText"><p class="fc-dustygray">Yahoo snippet &amp; one</p></div>
</div>
<div class="dd algo algo-sr relsrch Sr">
  <div><a href="https://r.search.yahoo.com/_ylt=A3/RV=2/RU=https%3a%2f%2fnews.example.com%2fb/RK=2">
    <h3><span>Yahoo Title Two</span></h3></a></div>
  <div><p>Yahoo snippet two</p></div>
</div>
</body></html>"""

BING_HTML = """<html><body><ol id="b_results">
<li class="b_algo">
  <h2><a href="https://news.example.com/a">Bing Title One</a></h2>
  <div class="b_caption"><p class="b_lineclamp2">Bing snippet one &amp; more</p></div>
</li>
<li class="b_algo">
  <h2><a href="https://news.example.com/b">Bing Title Two</a></h2>
  <div class="b_caption"><p>Bing snippet two</p></div>
</li>
<li class="b_algo">
  <h2><a href="https://news.example.com/a">Bing Duplicate</a></h2>
  <p>dup</p>
</li>
</ol></body></html>"""

PAGE_HTML = """<!DOCTYPE html><html><head><title>测试 &amp; 标题</title>
<style>.cls { color: red; }</style><script>alert(1)</script>
<meta name="x" content="y"><noscript>无脚本内容</noscript></head>
<body><nav>导航</nav><h1>Hello</h1>
<p>第一段&nbsp;文字 &amp; 实体 &#39;quoted&#39;</p>
<div>第二段<br>换行</div>
<svg><text>svg文本</text></svg><template><p>模板内容</p></template>
</body></html>"""


def _getaddrinfo_returning(*ips):
    """假 socket.getaddrinfo：把任意域名解析成给定 IP（用于 DNS/SSRF 测试）。"""

    def fake(host, port=None, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 (str(ip), port or 0)) for ip in ips]

    return fake


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """默认域名解析到公网 IP：单测不依赖真实 DNS/联网。

    需要模拟私网/通配 DNS 的用例在自己的 monkeypatch 里覆盖本 fixture。
    """
    monkeypatch.setattr(search.socket, "getaddrinfo",
                        _getaddrinfo_returning("93.184.216.34"))


@pytest.fixture(autouse=True)
def _clear_serper_key(monkeypatch):
    """默认无 Serper key：验证"无 key 也能用"是默认路径。"""
    monkeypatch.delenv(search.SERPER_KEY_ENV, raising=False)


def _header(request, name):
    for key, value in request.header_items():
        if key.lower() == name.lower():
            return value
    return None


# ---------- web_search：DDG 解析 ----------

def test_duckduckgo_parses_filters_and_dedupes(monkeypatch):
    fake, calls = _routes_open({"duckduckgo": DDG_HTML})
    monkeypatch.setattr(search, "_open", fake)

    out = search.web_search("比特币 ETF", count=5)

    assert set(out) == {"ok", "provider", "query", "results", "count", "error"}
    assert out["ok"] is True and out["provider"] == "duckduckgo"
    assert out["query"] == "比特币 ETF" and out["error"] == ""
    assert out["count"] == len(out["results"]) == 2
    assert [r["url"] for r in out["results"]] == [
        "https://example.com/btc?a=1&b=2", "https://other.example.org/page"]
    assert out["results"][0]["title"] == "Bitcoin & ETF 新闻"
    assert out["results"][0]["snippet"] == "比特币 & ETF 的最新消息"
    assert out["results"][1]["title"] == "Second & Result"
    # 内网/非 http 结果必须被过滤掉
    raw = json.dumps(out, ensure_ascii=False)
    assert "javascript" not in raw and "127.0.0.1" not in raw
    assert "html.duckduckgo.com" in calls[0]["url"] and "q=" in calls[0]["url"]
    assert "比特币" in urllib.parse.unquote(calls[0]["url"])
    # 普通浏览器 UA + 中文 Accept-Language
    assert "Mozilla/5.0" in (_header(calls[0]["request"], "User-Agent") or "")
    assert (_header(calls[0]["request"], "Accept-Language") or "").startswith("zh-CN")


def test_ddgs_real_url_decodes_uddg_entities(monkeypatch):
    fake, _ = _routes_open({"duckduckgo": DDG_HTML})
    monkeypatch.setattr(search, "_open", fake)
    out = search.web_search("q")
    assert out["results"][0]["url"] == "https://example.com/btc?a=1&b=2"


def test_count_is_clamped(monkeypatch):
    fake, _ = _routes_open({"duckduckgo": _ddg_html(12)})
    monkeypatch.setattr(search, "_open", fake)

    assert search.web_search("q", count=99)["count"] == 10
    assert len(search.web_search("q", count=99)["results"]) == 10
    assert search.web_search("q", count=-3)["count"] == 1
    assert search.web_search("q", count=0)["count"] == 1
    assert search.web_search("q", count="bad")["count"] == 5
    assert search.web_search("q", count=None)["count"] == 5


# ---------- web_search：回退链 ----------

def test_ddg_failure_falls_back_to_yahoo(monkeypatch):
    """DDG 挂了要落到 Yahoo，**不是** Bing。

    线上实测：Bing 对中文查询会返回与主题无关的结果（查 FOMC 给
    current.com / 剑桥词典），有结果就会截停回退链，等于拿垃圾当答案。
    所以顺序是 DDG → Yahoo → Bing。
    """
    fake, calls = _routes_open({
        "duckduckgo": urllib.error.URLError("ddg down"),
        "yahoo": YAHOO_HTML,
    })
    monkeypatch.setattr(search, "_open", fake)

    out = search.web_search("q", count=5)
    assert out["ok"] is True and out["provider"] == "yahoo"
    assert out["count"] == 2
    assert out["results"][0]["url"] == "https://news.example.com/a"
    assert out["results"][0]["title"] == "Yahoo Title One"
    assert out["results"][0]["snippet"] == "Yahoo snippet & one"
    assert "duckduckgo.com" in calls[0]["url"]
    assert "search.yahoo.com" in calls[1]["url"] and "p=q" in calls[1]["url"]


def test_empty_ddg_results_fall_back_to_yahoo(monkeypatch):
    fake, calls = _routes_open({
        "duckduckgo": "<html><body>没有结果</body></html>",
        "yahoo": YAHOO_HTML,
    })
    monkeypatch.setattr(search, "_open", fake)
    out = search.web_search("q")
    assert out["ok"] is True and out["provider"] == "yahoo"
    assert len(calls) == 2


def test_yahoo_failure_falls_back_to_bing(monkeypatch):
    fake, calls = _routes_open({
        "duckduckgo": urllib.error.URLError("ddg down"),
        "yahoo": urllib.error.URLError("yahoo down"),
        "bing": BING_HTML,
    })
    monkeypatch.setattr(search, "_open", fake)
    out = search.web_search("q")
    assert out["ok"] is True and out["provider"] == "bing"
    assert out["count"] == 2
    assert len(calls) == 3


def test_all_providers_fail_returns_error_without_raising(monkeypatch):
    fake, calls = _routes_open({
        "duckduckgo": urllib.error.URLError("ddg down"),
        "yahoo": urllib.error.URLError("yahoo down"),
        "bing": TimeoutError("slow"),
    })
    monkeypatch.setattr(search, "_open", fake)

    out = search.web_search("q", timeout=1)
    assert out["ok"] is False and out["provider"] == ""
    assert out["results"] == [] and out["count"] == 0
    assert out["error"] and len(out["error"]) <= 200
    assert "duckduckgo" in out["error"] and "bing" in out["error"]
    assert len(calls) == 3


# ---------- Yahoo 解析 ----------

def test_yahoo_parser_extracts_wrapped_links():
    """Yahoo 的链接是 r.search.yahoo.com/.../RU=<encoded>/，必须还原。"""
    parser = search._YahooParser()
    search._feed_parser(parser, YAHOO_HTML)
    assert len(parser.results) == 2
    assert search._yahoo_real_url(parser.results[0]["href"]) == \
        "https://news.example.com/a"
    assert parser.results[0]["snippet"] == "Yahoo snippet & one"


def test_yahoo_real_url_passthrough_and_double_encoding():
    assert search._yahoo_real_url("https://plain.example.com/x") == \
        "https://plain.example.com/x"
    # RU 段被二次转义时再解一层
    wrapped = ("https://r.search.yahoo.com/x/RU=https%253A%252F%252Fexample.com"
               "%252Fa/RK=2")
    assert search._yahoo_real_url(wrapped) == "https://example.com/a"
    assert search._yahoo_real_url("") == ""


def test_fetch_bytes_retries_once_on_5xx(monkeypatch):
    """搜索站点会瞬时限流 5xx，隔一下重试一次就该过。"""
    calls = []

    class _Resp:
        def __init__(self, status, body):
            self.status = status
            self._body = body
            self.headers = {"Content-Type": "text/html; charset=utf-8"}

        def read(self, n=-1):
            return self._body

        def close(self):
            pass

    def fake_open(request, proxy="", timeout=10):
        calls.append(request.get_full_url())
        return _Resp(500, b"") if len(calls) == 1 else _Resp(200, b"<html>ok</html>")

    monkeypatch.setattr(search, "_open", fake_open)
    monkeypatch.setattr(search.time, "sleep", lambda _s: None)
    text = search._fetch_text(
        __import__("urllib.request", fromlist=["Request"]).Request("https://x.test/"),
        "", 5, 1000, retries=1)
    assert "ok" in text and len(calls) == 2


# ---------- web_search：Serper 优先 ----------

def test_serper_key_takes_priority_and_parses_organic(monkeypatch):
    monkeypatch.setenv(search.SERPER_KEY_ENV, "test-key-123")
    captured = {}

    def serper(request):
        captured["headers"] = {k.lower(): v for k, v in request.header_items()}
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["method"] = request.get_method()
        captured["url"] = request.get_full_url()
        return FakeResponse(json.dumps({
            "organic": [
                {"title": "Serper One", "link": "https://serper.example.com/1",
                 "snippet": "S1"},
                {"title": "Serper Two", "link": "https://serper.example.com/2",
                 "snippet": "S2"},
                {"title": "bad", "link": "ftp://example.com/x", "snippet": ""},
            ],
        }).encode(), headers={"Content-Type": "application/json"})

    fake, calls = _routes_open({"serper": serper})
    monkeypatch.setattr(search, "_open", fake)

    out = search.web_search("btc", count=3)

    assert out["ok"] is True and out["provider"] == "serper"
    assert [r["url"] for r in out["results"]] == [
        "https://serper.example.com/1", "https://serper.example.com/2"]
    assert out["count"] == 2
    assert captured["url"] == search.SERPER_URL
    assert captured["method"] == "POST"
    assert captured["headers"]["x-api-key"] == "test-key-123"
    assert captured["body"] == {"q": "btc", "num": 3, "hl": "zh-cn"}
    assert len(calls) == 1, "Serper 成功时不应再请求 DDG/Bing"


def test_serper_failure_falls_back_to_duckduckgo(monkeypatch):
    monkeypatch.setenv(search.SERPER_KEY_ENV, "bad-key")
    fake, calls = _routes_open({
        "serper": urllib.error.HTTPError(search.SERPER_URL, 401, "unauthorized",
                                         email.message.Message(), None),
        "duckduckgo": _ddg_html(2),
    })
    monkeypatch.setattr(search, "_open", fake)

    out = search.web_search("q")
    assert out["ok"] is True and out["provider"] == "duckduckgo"
    assert [call["url"] for call in calls][0] == search.SERPER_URL
    assert "duckduckgo" in calls[1]["url"]


# ---------- web_search：脏输入 / 参数透传 ----------

def test_bad_query_returns_structured_error(monkeypatch):
    calls = []

    def fake_open(*args, **kwargs):
        calls.append(args)
        raise AssertionError("空搜索词不应发起任何网络请求")

    monkeypatch.setattr(search, "_open", fake_open)
    for bad in ("", "   ", None, 123, "\n\t"):
        out = search.web_search(bad)
        assert out["ok"] is False and out["results"] == [] and out["provider"] == ""
        assert out["error"] and len(out["error"]) <= 200
    assert calls == []


def test_long_query_is_truncated_to_200_chars(monkeypatch):
    fake, calls = _routes_open({"duckduckgo": _ddg_html(1)})
    monkeypatch.setattr(search, "_open", fake)
    out = search.web_search("x" * 500)
    assert out["ok"] is True
    assert len(out["query"]) == 200
    assert out["query"] == "x" * 200


def test_query_whitespace_is_collapsed(monkeypatch):
    fake, calls = _routes_open({"duckduckgo": _ddg_html(1)})
    monkeypatch.setattr(search, "_open", fake)
    out = search.web_search("  a   b  ")
    assert out["query"] == "a b"


def test_proxy_and_timeout_are_forwarded(monkeypatch):
    fake, calls = _routes_open({"duckduckgo": _ddg_html(1)})
    monkeypatch.setattr(search, "_open", fake)
    search.web_search("q", proxy="http://127.0.0.1:7890", timeout=3.5)
    assert calls[0]["proxy"] == "http://127.0.0.1:7890"
    assert calls[0]["timeout"] == 3.5


# ---------- fetch_page：正文抽取 ----------

def test_fetch_page_extracts_title_and_clean_text(monkeypatch):
    fake, calls = _sequence_open(FakeResponse(
        PAGE_HTML.encode("utf-8"), url="https://example.com/news/1"))
    monkeypatch.setattr(search, "_open", fake)

    out = search.fetch_page("https://example.com/news/1")
    assert set(out) == {"ok", "url", "finalUrl", "title", "text",
                        "truncated", "error"}
    assert out["ok"] is True and out["error"] == ""
    assert out["url"] == "https://example.com/news/1"
    assert out["finalUrl"] == "https://example.com/news/1"
    assert out["title"] == "测试 & 标题"
    text = out["text"]
    assert "alert(1)" not in text and ".cls" not in text
    assert "无脚本内容" not in text and "svg文本" not in text and "模板内容" not in text
    assert "第一段 文字 & 实体 'quoted'" in text
    assert "第二段 换行" in text
    assert "\n" not in text and "  " not in text, "空白必须折叠"
    assert out["truncated"] is False
    assert calls == ["https://example.com/news/1"]


def test_fetch_page_truncates_text_to_max_chars(monkeypatch):
    fake, _ = _sequence_open(FakeResponse(PAGE_HTML.encode("utf-8")))
    monkeypatch.setattr(search, "_open", fake)
    out = search.fetch_page("https://example.com/a", max_chars=10)
    assert out["ok"] is True
    assert len(out["text"]) == 10
    assert out["truncated"] is True


def test_fetch_page_caps_download_at_512kb(monkeypatch):
    huge = ("<html><body><p>" + "啊" * 300000 + "</p></body></html>").encode("utf-8")
    fake, _ = _sequence_open(FakeResponse(huge))
    monkeypatch.setattr(search, "_open", fake)
    out = search.fetch_page("https://example.com/big")
    assert out["ok"] is True
    assert out["truncated"] is True
    assert len(out["text"]) == 8000, "默认正文上限 8000 字"


def test_fetch_page_decodes_gbk_by_header_and_fallback(monkeypatch):
    body = ("<html><head><title>中文标题</title></head><body><p>中文内容测试</p>"
            "</body></html>").encode("gbk")
    fake, _ = _sequence_open(FakeResponse(body, headers=_html_headers("text/html; charset=gbk")))
    monkeypatch.setattr(search, "_open", fake)
    out = search.fetch_page("https://example.com/gbk")
    assert out["ok"] and out["title"] == "中文标题" and "中文内容测试" in out["text"]

    # 没有 charset 头：utf-8 解不出来时回退 gbk/gb18030
    fake2, _ = _sequence_open(FakeResponse(body, headers=_html_headers("text/html")))
    monkeypatch.setattr(search, "_open", fake2)
    out2 = search.fetch_page("https://example.com/gbk2")
    assert out2["ok"] and "中文内容测试" in out2["text"]


def test_fetch_page_http_error_status(monkeypatch):
    fake, _ = _sequence_open(FakeResponse(b"", status=404))
    monkeypatch.setattr(search, "_open", fake)
    out = search.fetch_page("https://example.com/missing")
    assert out["ok"] is False
    assert "404" in out["error"]


# ---------- fetch_page：SSRF 防护 ----------

UNSAFE_URLS = [
    "ftp://example.com/file",
    "file:///C:/Windows/win.ini",
    "javascript:alert(1)",
    "data:text/html,<b>x</b>",
    "",
    "   ",
    None,
    123,
    "http://localhost/admin",
    "http://localhost:8080/",
    "http://127.0.0.1/",
    "http://127.1.2.3/",
    "http://10.1.2.3/",
    "http://172.16.0.1/",
    "http://172.31.255.255/",
    "http://192.168.0.1/",
    "http://169.254.169.254/latest/meta-data/",
    "http://100.100.100.200/latest/meta-data/",
    "http://[::1]/",
    "http://[::ffff:127.0.0.1]/",
    "http://0.0.0.0/",
    "http://foo.local/x",
    "http://metadata.google.internal/computeMetadata/v1/",
    "http://user:pass@example.com/",
    "http://2130706433/",
    "http://127%2e0%2e0%2e1/",
    "http://0x7f.0.0.1/",
    "http://0177.0.0.1/",
]


@pytest.mark.parametrize("url", UNSAFE_URLS)
def test_fetch_page_rejects_unsafe_urls(monkeypatch, url):
    """不安全 URL 必须 ok=False，且**从未调用 _open**。

    旧写法让 _open 抛 AssertionError，被 fetch_page 的兜底 except 吞掉后
    仍返回 ok=False —— 断言因此假绿（verifier 已指出）。这里改成记录调用。
    """
    calls = []

    def spy_open(*args, **kwargs):
        calls.append(args)
        return FakeResponse(b"")

    monkeypatch.setattr(search, "_open", spy_open)
    out = search.fetch_page(url)
    assert out["ok"] is False
    assert out["text"] == "" and out["truncated"] is False
    assert out["error"] and len(out["error"]) <= 200
    assert calls == [], f"不安全 URL 不得发起任何请求: {url!r}"


NUMERIC_HOST_URLS = [
    "http://2130706433/",            # 十进制整数形式的 127.0.0.1
    "http://0177.0.0.1/",            # 八进制
    "http://0x7f.0.0.1/",            # 十六进制
    "http://127.1/",                 # 短写
    "http://127%2e0%2e0%2e1/",       # 百分号编码的点
    "http://0177.0.0.1:8080/x",
]


@pytest.mark.parametrize("url", NUMERIC_HOST_URLS)
def test_numeric_or_encoded_ip_hosts_are_rejected_without_request(monkeypatch, url):
    """正则 d+→\\d+ 的回归：数值/编码形式 IP 必须拒绝且 _open 从未被调用。"""
    calls = []

    def spy_open(*args, **kwargs):
        calls.append(args)
        return FakeResponse(b"")

    monkeypatch.setattr(search, "_open", spy_open)
    out = search.fetch_page(url)
    assert out["ok"] is False, f"应拒绝数值/编码 IP: {url!r}"
    assert out["error"] and len(out["error"]) <= 200
    assert calls == [], f"数值/编码 IP 不得发起请求: {url!r}"


# ---------- fetch_page：重定向逐跳校验 ----------

def test_redirect_to_internal_is_blocked(monkeypatch):
    fake, calls = _sequence_open(FakeResponse(
        b"", status=302, headers={"Location": "http://127.0.0.1/secret"}))
    monkeypatch.setattr(search, "_open", fake)
    out = search.fetch_page("https://example.com/start")
    assert out["ok"] is False
    assert "重定向" in out["error"]
    assert len(calls) == 1, "重定向目标不安全时不能继续请求"


def test_redirect_http_error_to_internal_is_blocked(monkeypatch):
    fake, calls = _sequence_open(_http_error(302, "http://10.0.0.5/internal"))
    monkeypatch.setattr(search, "_open", fake)
    out = search.fetch_page("https://example.com/start")
    assert out["ok"] is False and "重定向" in out["error"]
    assert len(calls) == 1


def test_public_redirects_are_followed(monkeypatch):
    fake, calls = _sequence_open(
        FakeResponse(b"", status=301, headers={"Location": "/next"}),
        FakeResponse(PAGE_HTML.encode("utf-8")))
    monkeypatch.setattr(search, "_open", fake)
    out = search.fetch_page("https://example.com/start")
    assert out["ok"] is True
    assert out["finalUrl"] == "https://example.com/next"
    assert calls == ["https://example.com/start", "https://example.com/next"]


def test_too_many_redirects_are_rejected(monkeypatch):
    counter = {"n": 0}

    def fake_open(request, proxy="", timeout=search.DEFAULT_TIMEOUT):
        counter["n"] += 1
        return FakeResponse(b"", status=302,
                            headers={"Location": f"/hop{counter['n']}"})

    monkeypatch.setattr(search, "_open", fake_open)
    out = search.fetch_page("https://example.com/0")
    assert out["ok"] is False
    assert "重定向次数" in out["error"]
    assert counter["n"] == 4, "最多 3 跳：第 4 次重定向必须拒绝"


# ---------- fetch_page：网络异常 ----------

@pytest.mark.parametrize("error", [
    urllib.error.URLError("dns failed"),
    TimeoutError("timed out"),
    OSError("connection reset"),
])
def test_fetch_page_network_errors_are_structured(monkeypatch, error):
    fake, _ = _sequence_open(error)
    monkeypatch.setattr(search, "_open", fake)
    out = search.fetch_page("https://example.com/a")
    assert out["ok"] is False
    assert out["error"] and len(out["error"]) <= 200
    assert out["text"] == ""


# ---------- _open：代理（目标 DNS 仍本地校验）/ 禁自动重定向 ----------

def test_proxy_opener_has_proxy_and_no_redirect(monkeypatch):
    captured = {}

    class FakeOpener:
        def open(self, request, timeout=None):
            captured["timeout"] = timeout
            return FakeResponse(b"ok")

    def fake_build(*handlers):
        captured["handlers"] = list(handlers)
        return FakeOpener()

    monkeypatch.setattr(search.urllib.request, "build_opener", fake_build)
    request = urllib.request.Request("https://example.com")

    response = search._open(request, "http://127.0.0.1:7890", 3)
    assert response.read() == b"ok"
    assert captured["timeout"] == 3.0
    assert any(isinstance(handler, search._NoRedirect)
               for handler in captured["handlers"])
    assert any(isinstance(handler, urllib.request.ProxyHandler)
               for handler in captured["handlers"])


def test_proxy_path_still_validates_target_dns_locally(monkeypatch):
    """代理场景：目标 host 仍必须本地解析校验；私网解析时连代理请求都不发。"""
    proxied = []
    monkeypatch.setattr(
        search, "_open_via_proxy",
        lambda request, proxy, timeout: proxied.append(
            (request.full_url, proxy, timeout)) or FakeResponse(PAGE_HTML.encode()))

    monkeypatch.setattr(search.socket, "getaddrinfo",
                        _getaddrinfo_returning("127.0.0.1"))
    out = search.fetch_page("https://example.com/a", proxy="http://127.0.0.1:7890")
    assert out["ok"] is False and proxied == []
    assert "内网" in out["error"] or "保留" in out["error"]

    monkeypatch.setattr(search.socket, "getaddrinfo",
                        _getaddrinfo_returning("93.184.216.34"))
    out2 = search.fetch_page("https://example.com/a", proxy="http://127.0.0.1:7890")
    assert out2["ok"] is True
    assert proxied and proxied[0][1] == "http://127.0.0.1:7890"


def test_direct_path_does_not_use_proxy_opener(monkeypatch):
    def forbidden_proxy(*args, **kwargs):
        raise AssertionError("空 proxy 不应走代理")

    monkeypatch.setattr(search, "_open_via_proxy", forbidden_proxy)
    calls = []
    monkeypatch.setattr(search, "_pinned_connection",
                        lambda scheme, hostname, ip, port, timeout:
                        calls.append((scheme, hostname, ip, port))
                        or _fake_connection())
    out = search.fetch_page("https://example.com/a")
    assert out["ok"] is True
    assert calls and calls[0][0] == "https" and calls[0][2] == "93.184.216.34"


# ---------- DNS 解析后校验真实 IP（SSRF-DNS / nip.io 绕过修复） ----------

@pytest.mark.parametrize("ip", [
    "127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.10",
    "169.254.169.254", "::1", "0.0.0.0",
])
def test_domain_resolving_to_private_ip_is_rejected(monkeypatch, ip):
    """URL 字面是公网域名，但 DNS 解析到内网/保留地址 → 拒绝且不发起请求。"""
    monkeypatch.setattr(search.socket, "getaddrinfo", _getaddrinfo_returning(ip))
    calls = []

    def spy_open(*args, **kwargs):
        calls.append(args)
        return FakeResponse(b"")

    monkeypatch.setattr(search, "_open", spy_open)
    out = search.fetch_page("https://evil.example.com/secret")
    assert out["ok"] is False
    assert out["text"] == ""
    assert out["error"] and len(out["error"]) <= 200
    assert "内网" in out["error"] or "保留" in out["error"]
    assert calls == [], "DNS 解析到内网时不得调用 _open"


def test_nip_io_wildcard_dns_cannot_reach_localhost(monkeypatch):
    """verifier 实测攻击路径：127.0.0.1.nip.io → 127.0.0.1 必须被拒绝。"""
    monkeypatch.setattr(search.socket, "getaddrinfo", _getaddrinfo_returning("127.0.0.1"))
    calls = []
    monkeypatch.setattr(search, "_open",
                        lambda *args, **kwargs: calls.append(args) or FakeResponse(b""))
    out = search.fetch_page("http://127.0.0.1.nip.io:8080/secret")
    assert out["ok"] is False and calls == []
    assert "内网" in out["error"] or "保留" in out["error"]


def test_metadata_wildcard_dns_cannot_reach_metadata_service(monkeypatch):
    monkeypatch.setattr(search.socket, "getaddrinfo",
                        _getaddrinfo_returning("169.254.169.254"))
    calls = []
    monkeypatch.setattr(search, "_open",
                        lambda *args, **kwargs: calls.append(args) or FakeResponse(b""))
    out = search.fetch_page("http://169.254.169.254.nip.io/latest/meta-data/")
    assert out["ok"] is False and calls == []


def test_any_private_answer_among_ips_is_rejected(monkeypatch):
    """解析出多个 IP 时，只要有一个是内网就整体拒绝（不能挑个公网的连）。"""
    monkeypatch.setattr(search.socket, "getaddrinfo",
                        _getaddrinfo_returning("93.184.216.34", "127.0.0.1"))
    calls = []
    monkeypatch.setattr(search, "_open",
                        lambda *args, **kwargs: calls.append(args) or FakeResponse(b""))
    out = search.fetch_page("https://mixed.example.com/a")
    assert out["ok"] is False and calls == []


def test_domain_resolving_to_public_ip_proceeds(monkeypatch):
    monkeypatch.setattr(search.socket, "getaddrinfo",
                        _getaddrinfo_returning("93.184.216.34"))
    fake, calls = _sequence_open(FakeResponse(PAGE_HTML.encode("utf-8")))
    monkeypatch.setattr(search, "_open", fake)
    out = search.fetch_page("https://example.com/news/1")
    assert out["ok"] is True and out["title"] == "测试 & 标题"
    assert calls == ["https://example.com/news/1"]


def test_dns_failure_with_real_open_is_structured_error(monkeypatch):
    """真实 _open：DNS 解析失败在连接前就返回结构化中文 error（不抛、不连）。"""
    def boom(*args, **kwargs):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(search.socket, "getaddrinfo", boom)
    out = search.fetch_page("https://does-not-exist.example.com/")
    assert out["ok"] is False and out["text"] == ""
    assert "解析" in out["error"] and len(out["error"]) <= 200


def test_mocked_open_is_not_blocked_by_unresolvable_host(monkeypatch):
    """提前解析失败时把结论交给 _open：离线 mock _open 的用例不受真实 DNS 影响。

    安全性不受影响：真实 _open 在连接前仍会完整解析+校验+固定 IP，解析失败一样拒绝。
    """
    def boom(*args, **kwargs):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(search.socket, "getaddrinfo", boom)
    fake, calls = _sequence_open(FakeResponse(PAGE_HTML.encode("utf-8")))
    monkeypatch.setattr(search, "_open", fake)
    out = search.fetch_page("https://public.example/start")
    assert out["ok"] is True and calls == ["https://public.example/start"]


def test_open_dns_failure_raises_dns_error(monkeypatch):
    def boom(*args, **kwargs):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(search.socket, "getaddrinfo", boom)
    with pytest.raises(search._DNSError):
        search._open(urllib.request.Request("https://does-not-exist.example.com/"))


def test_redirect_hop_revalidates_dns(monkeypatch):
    """重定向每一跳都要重新解析校验：第一跳公网、目标解析到内网 → 拒绝。"""
    def fake_gai(host, port=None, *args, **kwargs):
        ip = "93.184.216.34" if host == "example.com" else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0))]

    monkeypatch.setattr(search.socket, "getaddrinfo", fake_gai)
    fake, calls = _sequence_open(FakeResponse(
        b"", status=302, headers={"Location": "https://internal.evil.com/x"}))
    monkeypatch.setattr(search, "_open", fake)
    out = search.fetch_page("https://example.com/start")
    assert out["ok"] is False and "内网" in out["error"]
    assert calls == ["https://example.com/start"], "第二跳在解析阶段就被拒绝"


def test_open_rejects_private_dns_directly(monkeypatch):
    monkeypatch.setattr(search.socket, "getaddrinfo", _getaddrinfo_returning("127.0.0.1"))
    with pytest.raises(search._UnsafeURLError):
        search._open(urllib.request.Request("https://evil.example.com/"))


# ---------- 直连固定 IP（防 DNS rebinding TOCTOU） ----------

def test_fetch_page_connects_to_validated_ip_with_original_host(monkeypatch):
    """直连必须连接"解析校验后的 IP"，Host 头/SNI 仍用原域名。"""
    resolved = []

    def fake_gai(host, port=None, *args, **kwargs):
        resolved.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 ("93.184.216.34", port or 0))]

    monkeypatch.setattr(search.socket, "getaddrinfo", fake_gai)
    captured = {}
    fake_connection = _fake_connection(captured=captured)

    def fake_pinned(scheme, hostname, ip, port, timeout):
        captured.update(scheme=scheme, hostname=hostname, ip=ip, port=port,
                        timeout=timeout)
        return fake_connection

    monkeypatch.setattr(search, "_pinned_connection", fake_pinned)
    out = search.fetch_page("https://example.com/news/1?a=1")
    assert out["ok"] is True and out["title"] == "测试 & 标题"
    assert captured["ip"] == "93.184.216.34", "必须连接本地解析并校验后的 IP"
    assert captured["hostname"] == "example.com"
    assert captured["scheme"] == "https" and captured["port"] == 443
    assert captured["path"] == "/news/1?a=1"
    assert captured["headers"].get("host") == "example.com", "Host 头必须是原域名"
    assert "Mozilla" in captured["headers"].get("user-agent", "")
    assert resolved, "_open 连接前应重新解析并校验（防 DNS 变量绕过）"


def test_pinned_http_connection_targets_validated_ip(monkeypatch):
    seen = {}

    class FakeSock:
        def close(self):
            pass

    def fake_create_connection(address, timeout=None):
        seen["address"] = address
        seen["timeout"] = timeout
        return FakeSock()

    monkeypatch.setattr(search.socket, "create_connection", fake_create_connection)
    connection = search._PinnedHTTPConnection("example.com", "93.184.216.34", 8080, 3)
    assert connection.host == "example.com"
    connection.connect()
    assert seen["address"] == ("93.184.216.34", 8080)
    assert seen["timeout"] == 3


def test_pinned_https_connection_keeps_sni_hostname_and_verifies_cert():
    connection = search._PinnedHTTPSConnection("example.com", "93.184.216.34", 443, 3)
    assert connection.host == "example.com", "SNI/证书域名必须是原 hostname"
    assert connection._pinned_ip == "93.184.216.34"
    assert connection._context.verify_mode == search.ssl.CERT_REQUIRED
    assert connection._context.check_hostname is True


def test_search_provider_request_is_resolved_and_pinned(monkeypatch):
    """搜索 provider 请求同样走 DNS 校验 + 固定 IP 连接。"""
    monkeypatch.setenv(search.SERPER_KEY_ENV, "test-key")
    monkeypatch.setattr(search.socket, "getaddrinfo",
                        _getaddrinfo_returning("93.184.216.34"))
    payload = json.dumps({"organic": [
        {"title": "T", "link": "https://ok.example.com/1", "snippet": "S"}]}).encode()
    captured = {}
    connection = _fake_connection(
        raw=_FakeRawResponse(payload, headers={"Content-Type": "application/json"}),
        captured=captured)

    def fake_pinned(scheme, hostname, ip, port, timeout):
        captured.update(scheme=scheme, hostname=hostname, ip=ip, port=port)
        return connection

    monkeypatch.setattr(search, "_pinned_connection", fake_pinned)
    out = search.web_search("btc")
    assert out["ok"] is True and out["provider"] == "serper"
    assert captured["hostname"] == "google.serper.dev"
    assert captured["ip"] == "93.184.216.34"
    assert captured["method"] == "POST" and captured["path"] == "/search"


def test_search_provider_private_dns_blocked_without_connection(monkeypatch):
    monkeypatch.setattr(search.socket, "getaddrinfo", _getaddrinfo_returning("127.0.0.1"))
    connections = []

    def fake_pinned(*args, **kwargs):
        connections.append(args)
        return _fake_connection()

    monkeypatch.setattr(search, "_pinned_connection", fake_pinned)
    out = search.web_search("q")
    assert out["ok"] is False and out["provider"] == ""
    assert connections == [], "DNS 解析到内网时不得建立任何连接"
    assert "内网" in out["error"] or "保留" in out["error"]
