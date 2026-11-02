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


class TelegramMediaTest(unittest.TestCase):
    def _bot(self, handler):
        bot = TelegramBot("test-token", [], handler)
        bot.sent = []
        bot._request = lambda method, params=None: (bot.sent.append((method, params)) or {"ok": True})
        return bot

    def test_photo_is_downloaded_and_passed_as_attachment(self):
        seen = []

        async def handler(chat_id, text, attachments=None):
            seen.append((text, attachments))
            return "看到了"

        bot = self._bot(handler)
        raw = b"fake-png-bytes"
        bot.download = lambda file_id: (raw, "image/jpeg")
        update = {"message": {"chat": {"id": 1}, "caption": "这是什么",
                              "photo": [{"file_id": "small", "file_size": 10, "file_unique_id": "a"},
                                        {"file_id": "big", "file_size": 900, "file_unique_id": "b"}]}}
        asyncio.run(bot._handle_update(update))

        text, attachments = seen[0]
        self.assertEqual(text, "这是什么")
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]["kind"], "image")
        self.assertEqual(attachments[0]["mime"], "image/jpeg")
        self.assertTrue(attachments[0]["data"].startswith("data:image/jpeg;base64,"))

    def test_text_document_becomes_text_attachment(self):
        seen = []

        async def handler(chat_id, text, attachments=None):
            seen.append(attachments)
            return "ok"

        bot = self._bot(handler)
        bot.download = lambda file_id: (b"hello", "text/plain")
        update = {"message": {"chat": {"id": 1}, "document": {
            "file_id": "d1", "file_name": "notes.txt", "mime_type": "text/plain"}}}
        asyncio.run(bot._handle_update(update))
        self.assertEqual(seen[0][0]["kind"], "text")

    def test_unsupported_media_gets_an_explicit_reply(self):
        """发来不支持的媒体不能被静默吞掉。"""
        async def handler(chat_id, text, attachments=None):
            return "不该走到这里"

        bot = self._bot(handler)
        bot.download = lambda file_id: (b"xx", "video/mp4")
        update = {"message": {"chat": {"id": 1}, "video": {"file_id": "v1"}}}
        asyncio.run(bot._handle_update(update))
        texts = [p["text"] for m, p in bot.sent if m == "sendMessage"]
        self.assertTrue(any("没法读" in t or "只支持图片" in t for t in texts), texts)

    def test_two_arg_handler_still_works(self):
        """旧的两参数回调不能被当成三参数去调，也不能重复处理。"""
        seen = []

        async def handler(chat_id, text):
            seen.append(text)
            return "ok"

        bot = self._bot(handler)
        asyncio.run(bot._handle_update({"message": {"chat": {"id": 1}, "text": "hi"}}))
        self.assertEqual(seen, ["hi"])

    def test_handler_internal_typeerror_is_not_mistaken_for_signature(self):
        """回调自己抛 TypeError 时，不能重试一次（否则同一条消息处理两遍）。"""
        calls = []

        async def handler(chat_id, text, attachments=None):
            calls.append(text)
            raise TypeError("boom inside handler")

        bot = self._bot(handler)
        asyncio.run(bot._handle_update({"message": {"chat": {"id": 1}, "text": "hi"}}))
        self.assertEqual(len(calls), 1)
        texts = [p["text"] for m, p in bot.sent if m == "sendMessage"]
        self.assertTrue(any("处理失败" in t for t in texts))


if __name__ == "__main__":
    unittest.main()
