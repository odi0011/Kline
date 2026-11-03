"""流式解析：TTFT / 解码速度 / usage 汇总，以及失败时退回非流式。

这里直接喂一段假的 SSE 字节流，不联网——测的是解析与计时逻辑本身。
"""
import json
import unittest
import urllib.error

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
        # 真实响应带 headers；流式路径会读它，缺了会和真实行为不一致
        self.headers = {"Content-Type": "text/event-stream"}

    def read(self, size=-1):
        # 每次返回预置的一段，读完返回空字节表示结束。
        # 有意忽略 size：模拟"一次 read 带回多帧"的真实情况。
        return self.chunks.pop(0) if self.chunks else b""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def close(self):
        pass


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


class EmptyDeltaRetryTest(unittest.TestCase):
    """流式返回"有响应但既无文本也无工具调用"时重试一次。

    线上表现：用户发"发个表情包"，偶尔收到红色气泡
    "模型未返回文本或工具调用"。图其实没发出去，但再请求一次通常就有内容。
    """

    def setUp(self):
        self._open = provider.urllib.request.urlopen
        self._build = provider.urllib.request.build_opener
        self.calls = []

    def tearDown(self):
        provider.urllib.request.urlopen = self._open
        provider.urllib.request.build_opener = self._build

    def _install_sequence(self, bodies):
        """每次**请求**依次返回一个独立的响应对象。

        注意：FakeResponse 内部会把 chunks 逐个 pop 掉，所以必须每次请求
        都新建一个实例并传入**未消耗**的拷贝，否则第二次请求读到空流，
        会被 _chat_completion_stream 判成"没按流式回"而返回 None。
        """
        pending = [list(b) for b in bodies]
        state = {"n": 0}

        def opener(*a, **k):
            idx = state["n"]
            state["n"] += 1
            chunks = pending[idx] if idx < len(pending) else []
            return FakeResponse(list(chunks))

        provider.urllib.request.urlopen = opener
        provider.urllib.request.build_opener = lambda *a, **k: _NoopOpener(opener)

    def test_retries_once_on_empty_and_returns_content(self):
        empty = [b"data: " + json.dumps(
            {"choices": [{"delta": {}, "finish_reason": "stop"}]}).encode() + b"\n\n",
            b"data: [DONE]\n\n"]
        # sse() 返回的是**整段字节**，要放进 list 才算一个 chunk；
        # 直接传 bytes 会被 list() 拆成单字节，流解析就认不出 data: 行。
        good = [sse({"choices": [{"delta": {"content": "好"}}]})]
        self._install_sequence([empty, good])
        result = provider.chat_completion(
            {"base": "https://example.invalid", "key": "k"}, "test-model",
            [{"role": "user", "content": "hi"}], stream=True)
        assert result.get("ok"), result
        assert result.get("reply") == "好"

    def test_falls_back_to_non_stream_after_two_empties(self):
        """两次空响应后退回非流式，而不是把"未返回文本"直接甩给用户。"""
        empty = [b"data: " + json.dumps(
            {"choices": [{"delta": {}, "finish_reason": "stop"}]}).encode() + b"\n\n",
            b"data: [DONE]\n\n"]
        self._install_sequence([empty, empty])
        # 非流式兜底：测试里让它直接失败，只断言"确实尝试过"
        nonstream = []

        def opener(*a, **k):
            nonstream.append(1)
            raise urllib.error.URLError("blocked in test")

        provider.urllib.request.urlopen = opener
        result = provider.chat_completion(
            {"base": "https://example.invalid", "key": "k"}, "test-model",
            [{"role": "user", "content": "hi"}], stream=True)
        assert nonstream, "空响应后应该尝试非流式兜底"
        assert "未返回文本" not in str(result.get("error") or ""), result


class _NoopOpener:
    def __init__(self, open_fn):
        self._open = open_fn

    def open(self, *a, **k):
        return self._open(*a, **k)


class BusyRetryTest(unittest.TestCase):
    """服务商过载（503 Service is too busy）必须重试，且报错不能说成"不支持 tools"。"""

    def setUp(self):
        self._request = provider._request
        self._sleep = provider.time.sleep
        provider.time.sleep = lambda s: None      # 测试不等退避

    def tearDown(self):
        provider._request = self._request
        provider.time.sleep = self._sleep

    def _install(self, responses):
        queue = list(responses)

        def fake(url, key="", payload=None, proxy=None, timeout=0):
            return queue.pop(0) if queue else (500, {"error": "no more"})

        provider._request = fake

    def test_retries_on_503_then_succeeds(self):
        ok_body = {"choices": [{"finish_reason": "stop",
                                "message": {"role": "assistant", "content": "好"}}]}
        self._install([
            (503, {"error": {"message": "Service is too busy"}}),
            (200, ok_body),
        ])
        out = provider.chat_completion(
            {"base": "https://x.invalid", "key": "k"}, "m",
            [{"role": "user", "content": "hi"}], stream=False)
        assert out.get("ok"), out
        assert out.get("reply") == "好"

    def test_busy_error_does_not_blame_tools(self):
        """重试耗尽后，报错不能出现"服务商可能不支持 tools"这种误导。"""
        self._install([(503, {"error": {"message": "Service is too busy"}})] * 5)
        out = provider.chat_completion(
            {"base": "https://x.invalid", "key": "k"}, "m",
            [{"role": "user", "content": "hi"}], tools=[{"x": 1}], stream=False)
        assert not out.get("ok")
        err = str(out.get("error") or "")
        assert "不支持 tools" not in err, err
        assert out.get("busy") is True
        assert "过载" in err or "繁忙" in err, err

    def test_400_still_mentions_tools(self):
        """真正的 400（请求不被接受）才提 tools。"""
        self._install([(400, {"error": {"message": "tools not supported"}})])
        out = provider.chat_completion(
            {"base": "https://x.invalid", "key": "k"}, "m",
            [{"role": "user", "content": "hi"}], tools=[{"x": 1}], stream=False)
        assert not out.get("ok")
        assert "tools" in str(out.get("error") or "").lower()

    def test_auth_error_is_not_retried(self):
        """401 是配置问题，重试没意义。"""
        calls = []

        def fake(url, key="", payload=None, proxy=None, timeout=0):
            calls.append(1)
            return 401, {"error": {"message": "bad key"}}

        provider._request = fake
        out = provider.chat_completion(
            {"base": "https://x.invalid", "key": "k"}, "m",
            [{"role": "user", "content": "hi"}], stream=False)
        assert not out.get("ok")
        assert len(calls) == 1, f"401 不该重试，实际请求 {len(calls)} 次"
