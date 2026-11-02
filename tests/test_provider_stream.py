"""流式解析：TTFT / 解码速度 / usage 汇总，以及失败时退回非流式。

这里直接喂一段假的 SSE 字节流，不联网——测的是解析与计时逻辑本身。
"""
import json
import unittest

from ai import provider


def sse(*events):
    out = b""
    for event in events:
        out += b"data: " + json.dumps(event, ensure_ascii=False).encode() + b"\n\n"
    return out + b"data: [DONE]\n\n"


class FakeResponse:
    """按块吐字节，模拟真实的网络分片（一行可能被切在两个 chunk 里）。"""

    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.status = 200

    def read(self, size=-1):
        # 每次返回预置的一段，读完返回空字节表示结束
        return self.chunks.pop(0) if self.chunks else b""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class StreamParseTest(unittest.TestCase):
    def setUp(self):
        self._open = provider.urllib.request.urlopen
        self._build = provider.urllib.request.build_opener

    def tearDown(self):
        provider.urllib.request.urlopen = self._open
        provider.urllib.request.build_opener = self._build

    def _install(self, chunks):
        provider.urllib.request.urlopen = lambda *a, **k: FakeResponse(chunks)

    def _stream(self, chunks):
        self._install(chunks)
        return provider.chat_completion(
            {"base": "https://example.invalid", "key": "k"}, "test-model",
            [{"role": "user", "content": "hi"}], stream=True)

    def test_text_is_joined_and_timed(self):
        body = sse(
            {"model": "test-model", "choices": [{"delta": {"role": "assistant"}}]},
            {"choices": [{"delta": {"content": "你好"}}]},
            {"choices": [{"delta": {"content": "，世界"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                       "prompt_cache_hit_tokens": 80}},
        )
        # 把一帧拆成两段，验证跨 chunk 拼接（中文不转义，按字节定位）
        split = body.index("你好".encode("utf-8"))
        chunks = [body[:split + 5], body[split + 5:]]
        result = self._stream(chunks)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reply"], "你好，世界")
        self.assertTrue(result["streamed"])
        self.assertEqual(result["usage"]["prompt_cache_hit_tokens"], 80)
        self.assertEqual(result["chars"], 5)
        self.assertGreaterEqual(result["ttftMs"], 0)
        self.assertIsInstance(result["decodeMs"], int)

    def test_tool_calls_are_assembled_from_fragments(self):
        body = sse(
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call-1", "type": "function",
                 "function": {"name": "read_", "arguments": "{\"symbol\":"}}]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"name": "market", "arguments": "\"btcusdt\"}"}}]}}]},
        )
        result = self._stream([body])

        self.assertTrue(result["ok"], result)
        calls = result["tool_calls"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["id"], "call-1")
        self.assertEqual(calls[0]["function"]["name"], "read_market")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"symbol": "btcusdt"})

    def test_reasoning_content_is_kept_for_next_round(self):
        """思考型服务商要求把 reasoning_content 原样回传，丢了会 400。"""
        body = sse(
            {"choices": [{"delta": {"reasoning_content": "我先想想…"}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "c1", "type": "function",
                 "function": {"name": "read_advice", "arguments": "{}"}}]}}]},
        )
        result = self._stream([body])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["message"]["reasoning_content"], "我先想想…")

    def test_no_reasoning_field_when_absent(self):
        """普通服务商不吐思维链时，不能凭空造一个空字段。"""
        body = sse({"choices": [{"delta": {"content": "hi"}}]})
        result = self._stream([body])
        self.assertNotIn("reasoning_content", result["message"])

    def test_required_tool_choice_still_enforced(self):
        """服务商在流式下忽略 tool_choice 时，必须和以前一样报错，不能放行。"""
        body = sse({"choices": [{"delta": {"content": "我直接回答"}}]})
        provider.urllib.request.urlopen = lambda *a, **k: FakeResponse([body])
        result = provider.chat_completion(
            {"base": "https://example.invalid", "key": "k"}, "test-model",
            [{"role": "user", "content": "hi"}], stream=True,
            tools=[{"type": "function", "function": {"name": "x"}}],
            tool_choice="required")
        self.assertFalse(result["ok"])
        self.assertIn("工具", result["error"])

    def test_no_events_falls_back_to_non_stream(self):
        """服务商忽略 stream 参数、回了普通 JSON：要退回非流式，对话不能失败。"""
        self._install([b""])                      # 一个事件都没有
        calls = []

        def fake_request(url, key="", payload=None, proxy=None, timeout=20):
            calls.append(payload)
            return 200, {"choices": [{"message": {"role": "assistant", "content": "普通回复"},
                                      "finish_reason": "stop"}],
                         "usage": {"total_tokens": 7}}

        original = provider._request
        provider._request = fake_request
        try:
            result = provider.chat_completion(
                {"base": "https://example.invalid", "key": "k"}, "test-model",
                [{"role": "user", "content": "hi"}], stream=True)
        finally:
            provider._request = original

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reply"], "普通回复")
        self.assertNotIn("streamed", result)
        self.assertEqual(calls[0]["stream"], False, "退回非流式时要显式关掉 stream")

    def test_http_error_is_not_retried(self):
        """4xx 是服务商的真实答复，不能悄悄重试一次（会打两次模型）。"""
        import urllib.error

        def boom(*a, **k):
            raise urllib.error.HTTPError("https://example.invalid", 401, "Unauthorized",
                                         {}, None)

        provider.urllib.request.urlopen = boom
        result = provider.chat_completion(
            {"base": "https://example.invalid", "key": "k"}, "test-model",
            [{"role": "user", "content": "hi"}], stream=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 401)


if __name__ == "__main__":
    unittest.main()
