"""Telegram Bot：白名单、分段、token 处理。"""
import asyncio
import unittest

from feed.telegram import MAX_TEXT, TelegramBot


class TelegramBotTest(unittest.TestCase):
    def _bot(self, allowed=None, handler=None):
        bot = TelegramBot("test-token", allowed or [], handler or (lambda c, t: None))
        bot.sent = []
        bot._request = lambda method, params=None: (bot.sent.append((method, params)) or {"ok": True})
        return bot

    def test_authorized_chat_reaches_handler(self):
        seen = []

        async def handler(chat_id, text):
            seen.append((chat_id, text))
            return "回复内容"

        bot = self._bot(["123"], handler)
        asyncio.run(bot._handle_update({"message": {"chat": {"id": 123}, "text": "你好"}}))
        self.assertEqual(seen, [(123, "你好")])
        self.assertTrue(any(m == "sendMessage" and p["text"] == "回复内容" for m, p in bot.sent))

    def test_unauthorized_chat_is_rejected_with_its_id(self):
        seen = []

        async def handler(chat_id, text):
            seen.append(chat_id)
            return "不该走到这里"

        bot = self._bot(["123"], handler)
        asyncio.run(bot._handle_update({"message": {"chat": {"id": 999}, "text": "hi"}}))
        self.assertEqual(seen, [], "未授权会话不能进入 Agent")
        texts = [p["text"] for m, p in bot.sent if m == "sendMessage"]
        self.assertTrue(any("999" in t and "未授权" in t for t in texts),
                        "应把会话 ID 告知用户，方便加白名单")

    def test_empty_allowlist_permits_any_chat(self):
        seen = []

        async def handler(chat_id, text):
            seen.append(chat_id)
            return "ok"

        bot = self._bot([], handler)
        asyncio.run(bot._handle_update({"message": {"chat": {"id": 7}, "text": "hi"}}))
        self.assertEqual(seen, [7])

    def test_message_without_text_is_ignored(self):
        seen = []

        async def handler(chat_id, text):
            seen.append(text)
            return "x"

        bot = self._bot([], handler)
        asyncio.run(bot._handle_update({"message": {"chat": {"id": 1}}}))
        asyncio.run(bot._handle_update({"edited_message": {"text": "x"}}))
        self.assertEqual(seen, [])

    def test_long_reply_is_split_under_limit(self):
        bot = self._bot()
        bot.send(1, "x" * (MAX_TEXT * 2 + 50))
        texts = [p["text"] for m, p in bot.sent if m == "sendMessage"]
        self.assertEqual(len(texts), 3)
        self.assertTrue(all(len(t) <= MAX_TEXT for t in texts))
        self.assertEqual("".join(texts), "x" * (MAX_TEXT * 2 + 50))

    def test_handler_error_is_reported_not_raised(self):
        async def handler(chat_id, text):
            raise RuntimeError("boom")

        bot = self._bot([], handler)
        asyncio.run(bot._handle_update({"message": {"chat": {"id": 1}, "text": "hi"}}))
        texts = [p["text"] for m, p in bot.sent if m == "sendMessage"]
        self.assertTrue(any("处理失败" in t for t in texts))

    def test_missing_token_does_not_start(self):
        bot = TelegramBot("", [], None)
        bot.start()
        self.assertIsNone(bot._task)


if __name__ == "__main__":
    unittest.main()
