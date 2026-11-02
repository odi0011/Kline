"""新闻 AI 分析：正文入库时自动生成并落库，前端只读不触发。"""
import unittest

from feed.news_ai import analyze


def model_cfg(**extra):
    return {"models": [{"id": "m1", "active": True}], **extra}


class AnalyzeGuardTest(unittest.TestCase):
    def test_no_model_configured(self):
        out = analyze({"title": "T", "content": "x" * 80}, {})
        self.assertFalse(out["ok"])
        self.assertIn("模型", out["error"])

    def test_no_content(self):
        out = analyze({"title": "T"}, model_cfg(base="http://127.0.0.1:9/v1"))
        self.assertFalse(out["ok"])
        self.assertIn("正文", out["error"])

    def test_unreachable_endpoint_returns_error_not_raise(self):
        cfg = model_cfg(base="http://127.0.0.1:9/v1", key="sk-test")
        out = analyze({"title": "T", "content": "正文" * 50}, cfg)
        self.assertFalse(out["ok"])
        self.assertTrue(out["error"])

    def test_prompt_carries_untrusted_warning(self):
        # 系统提示里必须明确新闻是不可信数据，不能被正文里的指令带偏
        from feed.news_ai import SYSTEM
        self.assertIn("不可信", SYSTEM)
        self.assertIn("绝不执行", SYSTEM)


class SplitReplyTest(unittest.TestCase):
    """对话回复必须能切成多条短消息，不能一条几千字。"""

    def test_short_reply_stays_one(self):
        from ai.agent import split_reply
        self.assertEqual(split_reply("一句话。"), ["一句话。"])

    def test_long_reply_splits_by_sentence_count(self):
        from ai.agent import MAX_SENTENCES, split_reply
        text = "".join(f"第{i}句。" for i in range(1, 10))
        pieces = split_reply(text)
        self.assertGreater(len(pieces), 1)
        for piece in pieces:
            self.assertLessEqual(piece.count("。"), MAX_SENTENCES)

    def test_model_supplied_separator_is_respected(self):
        from ai.agent import split_reply
        pieces = split_reply("第一段。|||第二段。|||第三段。")
        self.assertEqual(len(pieces), 3)
        self.assertEqual(pieces[0], "第一段。")

    def test_empty_reply(self):
        from ai.agent import split_reply
        self.assertEqual(split_reply(""), [])
        self.assertEqual(split_reply(None), [])


class NewsArticleAnalysisTest(unittest.TestCase):
    """hub 只读已有分析，不在请求里跑模型。"""

    def test_analysis_is_returned_with_content(self):
        import asyncio
        from server.hub import BroadcastHub

        hub = BroadcastHub({}, [], [])
        hub.news = [{"url": "u1", "title": "A", "desc": "摘要"}]
        hub.news_lookup_fn = lambda url: {"content": "正文", "desc": "摘要", "partial": False}
        looked = []

        def ai_fn(url):
            looked.append(url)
            return {"text": "方向：看多\n理由：x\n风险：y", "state": "done"}

        hub.news_ai_fn = ai_fn
        out = asyncio.run(hub._news_article("u1"))
        self.assertTrue(out["ok"])
        self.assertEqual(out["analysis"]["text"], "方向：看多\n理由：x\n风险：y")
        self.assertEqual(looked, ["u1"])

    def test_missing_analysis_is_pending_not_an_error(self):
        import asyncio
        from server.hub import BroadcastHub

        hub = BroadcastHub({}, [], [])
        hub.news = [{"url": "u1", "title": "A", "desc": "摘要"}]
        hub.news_lookup_fn = lambda url: {"content": "正文", "desc": "摘要", "partial": False}
        hub.news_ai_fn = lambda url: None
        out = asyncio.run(hub._news_article("u1"))
        self.assertTrue(out["ok"])
        self.assertIsNone(out["analysis"])


if __name__ == "__main__":
    unittest.main()
