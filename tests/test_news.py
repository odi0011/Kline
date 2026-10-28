"""Offline article security, parsing and ordering tests (standard library only)."""
import io
import json
import socket
import threading
import unittest
from datetime import datetime, timezone
from email.message import Message
from unittest.mock import Mock, patch

from feed import news


URL = "https://www.coindesk.com/markets/story"
PUBLIC = "93.184.216.34"


def headers(**values):
    result = Message()
    result["Content-Type"] = "text/html; charset=utf-8"
    for key, value in values.items():
        result[key.replace("_", "-")] = str(value)
    return result


def records(*addresses):
    return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))
            for ip in addresses]


class ArticleSecurityTests(unittest.TestCase):
    def test_reject_arbitrary_urls_credentials_ports_and_ip_literals(self):
        invalid = [
            "http://www.coindesk.com/a", "file:///etc/passwd", "https://evil.test/a",
            "https://coindesk.com.evil.test/a", "https://evil.coindesk.com/a",
            "https://user:secret@www.coindesk.com/a", "https://@www.coindesk.com/a",
            "https://www.coindesk.com:8443/a", "https://www.coindesk.com:/a",
            "https://127.0.0.1/a", "https://[::1]/a", "https://2130706433/a",
            "https://169.254.169.254/latest/meta-data", "https://localhost/a",
            "https://www.coindesk.com./a", "https://www.coindesk.com\\@evil.test/a",
            "https://www.coindesk.com/\na", " https://www.coindesk.com/a",
            "https://www.coindesk.com:bad/a", "https://[broken/a", "", None,
        ]
        with patch.object(news, "_request_article") as request, patch.object(news, "_public_addresses") as dns:
            for url in invalid:
                with self.subTest(url=url):
                    result = news.fetch_article({"url": url})
                    self.assertFalse(result["ok"])
                    self.assertTrue(result["partial"])
                    self.assertTrue(result["error"])
            request.assert_not_called()
            dns.assert_not_called()

    def test_exact_allowed_host_and_default_port(self):
        for host in news.ARTICLE_HOSTS:
            self.assertEqual(news._validate_article_url(f"https://{host}:443/a"), host)
        self.assertEqual(news._validate_article_url(URL.upper()), "www.coindesk.com")

    def test_requires_item_not_url(self):
        self.assertFalse(news.fetch_article(URL)["ok"])

    def test_all_dns_answers_must_be_public(self):
        blocked = ["127.0.0.1", "10.0.0.2", "172.16.1.1", "192.168.1.1", "169.254.169.254",
                   "100.64.0.1", "0.0.0.0", "224.0.0.1", "::1", "fc00::1", "fe80::1",
                   "::ffff:127.0.0.1", "2002:7f00:1::", "2001:db8::1", "ff02::1"]
        for address in blocked:
            with self.subTest(address=address), patch.object(socket, "getaddrinfo", return_value=records(PUBLIC, address)):
                with self.assertRaises(news._ArticleError):
                    news._public_addresses("www.coindesk.com", 1)
        with patch.object(socket, "getaddrinfo", return_value=records(PUBLIC, PUBLIC)):
            self.assertEqual(news._public_addresses("www.coindesk.com", 1), [PUBLIC])

    def test_internal_dns_returns_only_summary_without_connection(self):
        with patch.object(socket, "getaddrinfo", return_value=records("10.0.0.1")), patch.object(news, "_request_article") as request:
            result = news.fetch_article({"url": URL, "desc": "RSS 摘要"})
            self.assertTrue(result["ok"])
            self.assertTrue(result["partial"])
            self.assertEqual(result["content"], "RSS 摘要")
            self.assertIn("非公网", result["error"])
            request.assert_not_called()

    def test_dns_errors_and_empty_answers(self):
        for result in (OSError("failed"), []):
            with self.subTest(result=result), patch.object(socket, "getaddrinfo") as dns:
                if isinstance(result, Exception):
                    dns.side_effect = result
                else:
                    dns.return_value = result
                with self.assertRaises(news._ArticleError):
                    news._public_addresses("www.coindesk.com", 1)

    def test_dns_timeout_does_not_wait_for_stuck_resolver(self):
        release = threading.Event()
        finished = threading.Event()
        def resolve(*args, **kwargs):
            try:
                release.wait(2)
                return records(PUBLIC)
            finally:
                finished.set()
        with patch.object(socket, "getaddrinfo", side_effect=resolve):
            try:
                with self.assertRaisesRegex(news._ArticleError, "超时"):
                    news._public_addresses("www.coindesk.com", .01)
            finally:
                release.set()
                self.assertTrue(finished.wait(1))

    def test_direct_connection_pins_ip_and_uses_original_tls_hostname(self):
        context = Mock()
        with patch.object(news.ssl, "create_default_context", return_value=context), patch.object(socket, "create_connection") as connect:
            conn = news._ArticleConnection("www.coindesk.com", PUBLIC, 3)
            conn.connect()
            connect.assert_called_once_with((PUBLIC, 443), 3)
            context.wrap_socket.assert_called_once_with(connect.return_value, server_hostname="www.coindesk.com")
            conn.close()

    def test_proxy_connect_tunnels_to_verified_ip_not_remote_dns(self):
        context = Mock()
        with patch.object(news.ssl, "create_default_context", return_value=context), patch.object(socket, "create_connection") as connect:
            conn = news._ArticleConnection("www.coindesk.com", PUBLIC, 3, "http://127.0.0.1:7890")
            conn._tunnel = Mock()
            conn.connect()
            self.assertEqual(conn._tunnel_host, PUBLIC)
            self.assertEqual(conn._tunnel_port, 443)
            connect.assert_called_once_with(("127.0.0.1", 7890), 3)
            conn._tunnel.assert_called_once()
            self.assertEqual(context.wrap_socket.call_args.kwargs["server_hostname"], "www.coindesk.com")
            conn.close()

    def test_unsupported_proxy_fails_closed(self):
        result = None
        with patch.object(news, "_public_addresses", return_value=[PUBLIC]):
            result = news.fetch_article({"url": URL}, "socks5://127.0.0.1:1080")
        self.assertFalse(result["ok"])
        self.assertIn("代理", result["error"])

    def test_each_redirect_is_validated(self):
        for target in ("http://www.coindesk.com/a", "https://evil.test/a", "https://127.0.0.1/", "//localhost/a",
                       "https://user@www.coindesk.com/a", "https://www.coindesk.com:444/a", "\nhttps://www.coindesk.com/a"):
            with self.subTest(target=target), patch.object(news, "_public_addresses", return_value=[PUBLIC]), patch.object(
                    news, "_request_article", return_value=(302, headers(Location=target), b"")) as request:
                self.assertFalse(news.fetch_article({"url": URL})["ok"])
                self.assertEqual(request.call_count, 1)

    def test_redirect_rechecks_dns_and_blocks_rebinding(self):
        with patch.object(socket, "getaddrinfo", side_effect=[records(PUBLIC), records("127.0.0.1")]) as dns, patch.object(
                news, "_request_article", return_value=(302, headers(Location="/second"), b"")) as request:
            result = news.fetch_article({"url": URL})
            self.assertFalse(result["ok"])
            self.assertEqual(dns.call_count, 2)
            self.assertEqual(request.call_count, 1)

    def test_relative_redirect_and_approved_host_redirect(self):
        for target in ("/second", "https://decrypt.co/second"):
            with self.subTest(target=target), patch.object(news, "_public_addresses", return_value=[PUBLIC]), patch.object(news, "_request_article", side_effect=[
                (302, headers(Location=target), b""), (200, headers(), b"<article><p>Actual article.</p></article>")
            ]) as request:
                result = news.fetch_article({"url": URL})
                self.assertEqual(result["content"], "Actual article.")
                self.assertTrue(request.call_args.args[0].endswith("/second"))
                self.assertEqual(request.call_args.args[2], PUBLIC)

    def test_redirect_loop_and_limit(self):
        with patch.object(news, "_public_addresses", return_value=[PUBLIC]), patch.object(news, "_request_article", return_value=(302, headers(Location=URL), b"")) as request:
            self.assertIn("循环", news.fetch_article({"url": URL})["error"])
            self.assertEqual(request.call_count, 1)
        count = 0
        def redirect(*args):
            nonlocal count
            count += 1
            return 302, headers(Location=f"/hop/{count}"), b""
        with patch.object(news, "_public_addresses", return_value=[PUBLIC]), patch.object(news, "_request_article", side_effect=redirect):
            self.assertIn("过多", news.fetch_article({"url": URL})["error"])
            self.assertEqual(count, news.ARTICLE_MAX_REDIRECTS + 1)

    def test_total_deadline_not_reset_after_dns_or_redirects(self):
        with patch.object(news.time, "monotonic", side_effect=[0, 0, 13]), patch.object(news, "_public_addresses", return_value=[PUBLIC]), patch.object(news, "_request_article") as request:
            self.assertIn("超时", news.fetch_article({"url": URL})["error"])
            request.assert_not_called()
        with patch.object(news.time, "monotonic", side_effect=[0, 0, 1, 13]), patch.object(news, "_public_addresses", return_value=[PUBLIC]), patch.object(news, "_request_article", return_value=(200, headers(), b"<article>x</article>")):
            self.assertIn("超时", news.fetch_article({"url": URL})["error"])

    def test_network_failure_is_partial_only_if_summary_exists(self):
        with patch.object(news, "_public_addresses", return_value=[PUBLIC]), patch.object(news, "_request_article", side_effect=TimeoutError):
            self.assertFalse(news.fetch_article({"url": URL})["ok"])
            result = news.fetch_article({"url": URL, "desc": "<p>摘要</p><script>bad()</script>"})
            self.assertEqual(result["content"], "摘要")
            self.assertTrue(result["partial"])
            self.assertIn("并非全文", result["error"])


class ArticleTransportTests(unittest.TestCase):
    def request(self, body=b"<article>text</article>", status=200, response_headers=None):
        response = Mock()
        response.status = status
        response.headers = response_headers or headers()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.side_effect = io.BytesIO(body).read
        with patch.object(news, "_ArticleConnection") as factory:
            factory.return_value.getresponse.return_value = response
            try:
                result = news._request_article(URL, "www.coindesk.com", PUBLIC, None, 2)
                return result, response, factory.return_value
            finally:
                factory.return_value.close.assert_called_once()

    def test_read_is_bounded_and_explicit_identity_encoding(self):
        result, response, conn = self.request()
        self.assertEqual(result[2], b"<article>text</article>")
        response.read.assert_called_once_with(news.ARTICLE_MAX_BYTES + 1)
        self.assertEqual(conn.request.call_args.kwargs["headers"]["Accept-Encoding"], "identity")

    def test_oversized_body_without_content_length(self):
        with self.assertRaisesRegex(news._ArticleError, "2MB"):
            self.request(b"x" * (news.ARTICLE_MAX_BYTES + 1))

    def test_oversized_or_invalid_content_length(self):
        for length in (news.ARTICLE_MAX_BYTES + 1, "invalid", "-2"):
            with self.subTest(length=length), self.assertRaises(news._ArticleError):
                self.request(response_headers=headers(Content_Length=length))

    def test_compression_and_non_html_rejected(self):
        invalid_type = Message()
        invalid_type["Content-Type"] = "application/octet-stream"
        for value in (headers(Content_Encoding="gzip"), invalid_type):
            with self.assertRaises(news._ArticleError):
                self.request(response_headers=value)

    def test_http_failure_and_redirect_does_not_read_body(self):
        with self.assertRaisesRegex(news._ArticleError, "403"):
            self.request(status=403)
        result, response, _ = self.request(status=302, response_headers=headers(Location="/next"))
        self.assertEqual(result[0], 302)
        response.read.assert_not_called()


class ArticleParsingTests(unittest.TestCase):
    def fetch(self, html, **item):
        with patch.object(news, "_public_addresses", return_value=[PUBLIC]), patch.object(news, "_request_article", return_value=(200, headers(), html.encode())):
            return news.fetch_article({"url": URL, **item})

    def test_jsonld_graph_article_body_preferred_to_page_chrome(self):
        body = "First paragraph & factual text.\n\nSecond paragraph."
        data = {"@graph": [{"@type": "NewsArticle", "articleBody": body}]}
        html = '<script type="application/ld+json">' + json.dumps(data) + '</script><main>Teaser</main>'
        result = self.fetch(html)
        self.assertEqual(result["content"], body)
        self.assertFalse(result["partial"])

    def test_jsonld_arrays_html_and_invalid_blocks(self):
        html = '<script type="application/ld+json">broken</script><script type="application/ld+json">' + json.dumps([
            {"articleBody": "<p>正文 &amp; 数据</p><script>bad</script><p>第二段</p>"}
        ]).replace("</", "<\\/") + '</script>'
        self.assertEqual(self.fetch(html)["content"], "正文 & 数据\n\n第二段")

    def test_article_main_extraction_removes_active_and_navigation_content(self):
        html = """<body>outside<nav>top links</nav><main>teaser<article><header>site branding</header>
        <p>新闻 <b>正文</b> &amp; 数据</p><script>bad()</script><style>bad css</style>
        <nav>share links</nav><p>第二段<br>下一行</p><aside>advert</aside><footer>legal</footer>
        <div hidden>secret</div></article>related links</main></body>"""
        result = self.fetch(html)
        self.assertEqual(result["content"], "新闻 正文 & 数据\n\n第二段\n\n下一行")
        self.assertFalse(result["partial"])
        self.assertEqual(self.fetch("<main><p>Main article</p></main>")["content"], "Main article")

    def test_no_article_does_not_claim_whole_page_is_content(self):
        result = self.fetch("<body><nav>Site menu</nav><div>Subscribe now</div></body>", desc="RSS 摘要")
        self.assertEqual(result["content"], "RSS 摘要")
        self.assertTrue(result["partial"])
        self.assertIn("摘要", result["error"])

    def test_paywalled_jsonld_marked_partial(self):
        html = '<script type="application/ld+json">' + json.dumps({
            "articleBody": "Only the preview", "isAccessibleForFree": False
        }) + '</script>'
        self.assertTrue(self.fetch(html)["partial"])

    def test_page_returning_rss_summary_marked_partial(self):
        result = self.fetch("<article><p>Only a teaser.</p></article>", desc="Only a teaser.")
        self.assertTrue(result["partial"])
        self.assertIn("并非全文", result["error"])

    def test_short_fragment_prefers_longer_summary(self):
        summary = "这是一条完整的 RSS 摘要，包含比页面碎片更多的正文信息。" * 6
        result = self.fetch("<article><p>Cookie notice</p></article>", desc=summary)
        self.assertEqual(result["content"], summary)
        self.assertTrue(result["partial"])
        self.assertIn("并非全文", result["error"])

    def test_title_plus_summary_is_still_partial(self):
        result = self.fetch("<main><h1>Headline</h1><p>Only a teaser.</p></main>",
                            title="Headline", desc="Only a teaser.")
        self.assertTrue(result["partial"])
        self.assertIn("并非全文", result["error"])

    def test_rss_encoded_full_body_preserved_and_preferred_without_network(self):
        long_body = "完整新闻正文。" * 120
        xml = f'''<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item>
          <title>Title &amp; text</title><link>{URL}</link><description>Short summary</description>
          <content:encoded><![CDATA[<p>{long_body}</p><script>evil()</script><p>最后一段</p>]]></content:encoded>
          <pubDate>Tue, 27 Oct 2026 10:00:00 GMT</pubDate></item></channel></rss>'''
        item = news._parse_rss(xml)[0]
        self.assertEqual(item["title"], "Title & text")
        self.assertEqual(item["desc"], "Short summary")
        self.assertIn(long_body, item["content"])
        with patch.object(news, "_request_article") as request, patch.object(news, "_public_addresses") as dns:
            result = news.fetch_article(item)
            self.assertEqual(result["content"], long_body + "\n\n最后一段")
            self.assertFalse(result["partial"])
            request.assert_not_called()
            dns.assert_not_called()

    def test_summary_is_not_misclassified_as_encoded_content(self):
        xml = f"<rss><channel><item><title>News</title><link>{URL}</link><description>Summary</description></item></channel></rss>"
        item = news._parse_rss(xml)[0]
        self.assertNotIn("content", item)
        result = self.fetch("<body>Unextractable</body>", desc=item["desc"])
        self.assertEqual(result["content"], "Summary")
        self.assertTrue(result["partial"])

    def test_atom_links_content_and_timezone_independent_timestamp(self):
        xml = f'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>News</title>
          <link rel="self" href="https://evil.test/feed"/><link rel="alternate" href="{URL}"/>
          <published>2026-10-27T10:00:00+08:00</published><summary>Preview</summary>
          <content type="html">&lt;p&gt;Full article&lt;/p&gt;</content></entry></feed>'''
        item = news._parse_rss(xml)[0]
        self.assertEqual(item["url"], URL)
        self.assertEqual(item["content"], "Full article")
        expected = datetime(2026, 10, 27, 2, tzinfo=timezone.utc).timestamp() * 1000
        self.assertEqual(item["ts"], expected)

    def test_reject_entity_declarations_and_malformed_xml(self):
        self.assertEqual(news._parse_rss('<!DOCTYPE rss [<!ENTITY x "bad">]><rss/>'), [])
        self.assertEqual(news._parse_rss('<rss><item>'), [])

    def test_atom_xhtml_removes_namespaced_script_and_navigation(self):
        xml = f'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>News</title>
          <link href="{URL}"/><content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">
          <p>正文</p><nav>Menu</nav><script>bad()</script><style>bad css</style>
          <p>第二段</p><footer>Footer</footer></div></content></entry></feed>'''
        self.assertEqual(news._parse_rss(xml)[0]["content"], "正文\n\n第二段")

    def test_media_content_is_not_article_full_text(self):
        xml = f'''<rss xmlns:media="http://search.yahoo.com/mrss/"><channel><item>
          <title>News</title><link>{URL}</link><description>Summary</description>
          <media:content><media:description>Image caption</media:description></media:content>
          </item></channel></rss>'''
        item = news._parse_rss(xml)[0]
        self.assertNotIn("content", item)
        self.assertEqual(item["desc"], "Summary")

    def test_cached_full_content_still_requires_allowed_url(self):
        result = news.fetch_article({"url": "https://evil.test/a", "content": "fake", "contentPartial": False})
        self.assertFalse(result["ok"])

    def test_cached_full_body_size_limit_marks_partial(self):
        result = news.fetch_article({"url": URL, "content": "文" * news.ARTICLE_MAX_BYTES, "contentPartial": False})
        self.assertTrue(result["partial"])
        self.assertLessEqual(len(result["content"].encode()), news.ARTICLE_MAX_BYTES)

    def test_unknown_charset_falls_back_to_utf8(self):
        value = Message()
        value["Content-Type"] = "text/html; charset=not-a-charset"
        with patch.object(news, "_public_addresses", return_value=[PUBLIC]), patch.object(news, "_request_article", return_value=(200, value, "<article>正文</article>".encode())):
            self.assertEqual(news.fetch_article({"url": URL})["content"], "正文")

    def test_aggregation_selects_relevant_items_but_displays_stable_ascending(self):
        now = int(news.time.time() * 1000)
        items = [
            {"url": URL + "/new", "title": "Bitcoin newest", "ts": now - 1},
            {"url": URL + "/old", "title": "Bitcoin oldest", "ts": now - 1000},
            {"url": URL + "/tie-a", "title": "Bitcoin tie A", "ts": now - 100},
            {"url": URL + "/tie-b", "title": "Bitcoin tie B", "ts": now - 100},
            {"url": URL + "/unrelated", "title": "Weather", "ts": now},
        ]
        with patch.object(news, "fetch_binance", return_value=items), patch.object(news, "fetch_okx", return_value=[]), patch.object(news, "fetch_rss", return_value=[]):
            result = news.fetch_all(shown_symbols=["btcusdt"], limit_total=4)
        self.assertEqual([it["title"] for it in result], ["Bitcoin oldest", "Bitcoin tie A", "Bitcoin tie B", "Bitcoin newest"])


if __name__ == "__main__":
    unittest.main()
