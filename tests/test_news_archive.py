"""新闻正文必须来自存档，不能在点击时实时抓源站（那会被限流）。"""
import asyncio
import threading
import unittest

from server.hub import BroadcastHub


class NewsArticleFromStoreTest(unittest.TestCase):
    def run_case(self, lookup, news=None, enqueue=None):
        hub = BroadcastHub({}, [], [])
        hub.news = news if news is not None else [{"url": "u1", "title": "A", "desc": "摘要A"}]
        hub.news_lookup_fn = lookup
        if enqueue is not None:
            hub.news_enqueue_fn = enqueue
        return asyncio.run(hub._news_article("u1"))

    def test_full_content_comes_from_store(self):
        out = self.run_case(lambda url: {"content": "完整正文", "desc": "摘要A", "partial": False})
        self.assertTrue(out["ok"])
        self.assertEqual(out["content"], "完整正文")
        self.assertFalse(out["partial"])

    def test_summary_only_queues_background_fetch(self):
        queued = []
        out = self.run_case(lambda url: {"content": None, "desc": "摘要A", "partial": True},
                            enqueue=queued.append)
        self.assertEqual(out["content"], "摘要A")
        self.assertTrue(out["partial"])
        self.assertTrue(out.get("pending"))
        self.assertEqual(queued, ["u1"])

    def test_unknown_url_is_an_error_not_a_crawl(self):
        out = self.run_case(lambda url: None, news=[])
        self.assertFalse(out["ok"])

    def test_never_calls_network_inline(self):
        """点开新闻只允许读库 + 入队；任何同步抓取都意味着又会被 429。"""
        calls = []

        def lookup(url):
            calls.append(("lookup", url))
            return {"content": None, "desc": "摘要", "partial": True}

        queued = []
        self.run_case(lookup, enqueue=queued.append)
        self.assertEqual([c[0] for c in calls], ["lookup"])
        self.assertEqual(queued, ["u1"])


class SaveNewsStateTest(unittest.TestCase):
    """state 机：只有拿到全文才算 done，否则保持 pending 以便重试。"""

    def setUp(self):
        import storage as st
        self.log = []

        outer = self

        class Cur:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, sql, args=()): outer.log.append(("exec", " ".join(sql.split()), args))
            def executemany(self, sql, rows): outer.log.append(("many", " ".join(sql.split()), rows))
            def fetchall(self): return []

        class Conn:
            def ping(self, **k): pass
            def cursor(self): return Cur()

        self.storage = st.Storage.__new__(st.Storage)
        self.storage._lock = threading.Lock()
        self.storage.enabled = True
        self.storage._conn = Conn()

    def test_row_state_follows_content(self):
        rows = [
            {"url": "u1", "source": "S", "title": "T1", "ts": 1, "desc": "摘要"},
            {"url": "u2", "source": "S", "title": "T2", "ts": 2, "desc": "摘要",
             "content": "全文", "contentPartial": False},
        ]
        self.assertEqual(self.storage.save_news(rows), 2)
        payload = self.log[-1][2]
        self.assertEqual(payload[0][8], "pending")   # 无正文
        self.assertEqual(payload[1][8], "done")      # 有全文

    def test_failed_fetch_stays_pending(self):
        self.storage.save_news_content("u1", "", True)
        args = self.log[-1][2]
        self.assertEqual(args[2], "pending")

    def test_successful_fetch_marks_done(self):
        self.storage.save_news_content("u1", "正文", False)
        args = self.log[-1][2]
        self.assertEqual(args[2], "done")


if __name__ == "__main__":
    unittest.main()
