"""OpenAI 兼容接口（OpenClaw / 微信接入）。

它把 ODDD 的 Agent 包成一个「模型」，OpenClaw 在 models.providers 里
指过来就能用。这个端点后面连着能真实下单的 Agent，所以安全边界要硬：
默认关闭、必须有令牌、限流、只取最后一条 user 消息。
"""
import json
import pathlib

import pytest

from server.openai_compat import AuthError, OpenAICompat

ROOT = pathlib.Path(__file__).resolve().parents[1]
PUB = str(ROOT / "web" / "public" / "stickers")
ATT = str(ROOT / "agent-attachments")


def _api(reply="改好了。", **kw):
    calls = []

    def ask(prompt, attachments=None):
        calls.append((prompt, attachments))
        return reply

    api = OpenAICompat(token="secret-token-123456", ask_fn=ask,
                       public_dir=PUB, attach_dir=ATT,
                       base_url="https://od1.site", **kw)
    return api, calls


def _post(api, payload, token="secret-token-123456"):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    body = json.dumps(payload).encode()
    return api.handle("POST", "/v1/chat/completions", headers, body)


# ---------- 鉴权 ----------

def test_no_token_configured_rejects_everything():
    """没配令牌时连"接口存在"都不透露——这是 401，不是 404。"""
    api = OpenAICompat(token="", ask_fn=lambda p: "x")
    status, _, _ = _post(api, {"messages": [{"role": "user", "content": "hi"}]})
    assert status == 401


def test_wrong_token_rejected():
    api, calls = _api()
    status, _, body = _post(api, {"messages": [{"role": "user", "content": "hi"}]},
                            token="wrong")
    assert status == 401
    assert calls == []


def test_missing_token_rejected():
    api, calls = _api()
    status, _, _ = _post(api, {"messages": [{"role": "user", "content": "hi"}]},
                         token=None)
    assert status == 401
    assert calls == []


def test_x_api_key_header_also_accepted():
    api, _ = _api()
    body = json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode()
    status, _, _ = api.handle("POST", "/v1/chat/completions",
                              {"x-api-key": "secret-token-123456"}, body)
    assert status == 200


# ---------- 请求解析 ----------

def test_only_last_user_message_is_used():
    """前面的 user 消息是 OpenClaw 拼的历史，重复喂会让同一件事出现两遍。"""
    api, calls = _api()
    _post(api, {"messages": [
        {"role": "system", "content": "你是 ZCode，一个编程助手"},
        {"role": "user", "content": "旧问题"},
        {"role": "assistant", "content": "旧回答"},
        {"role": "user", "content": "现在能开仓吗"},
    ]})
    assert len(calls) == 1
    assert calls[0][0] == "现在能开仓吗"


def test_system_prompt_never_becomes_the_question():
    """OpenClaw 自己的 system prompt 不能当用户问题转给 Agent。"""
    api, calls = _api()
    _post(api, {"messages": [
        {"role": "system", "content": "You are a helpful coding assistant."},
        {"role": "user", "content": "你在吗"},
    ]})
    assert "coding assistant" not in calls[0][0]


def test_bad_json_returns_400():
    api, _ = _api()
    status, _, _ = api.handle("POST", "/v1/chat/completions",
                              {"Authorization": "Bearer secret-token-123456"},
                              b"{not json")
    assert status == 400


def test_empty_messages_returns_400():
    api, calls = _api()
    status, _, _ = _post(api, {"messages": []})
    assert status == 400
    assert calls == []


# ---------- 响应形状 ----------

def test_completion_shape_is_openai_compatible():
    api, _ = _api("行，收工。")
    status, _, raw = _post(api, {"messages": [{"role": "user", "content": "收工"}]})
    assert status == 200
    data = json.loads(raw)
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["role"] == "assistant"
    assert data["choices"][0]["message"]["content"] == "行，收工。"
    assert data["choices"][0]["finish_reason"] == "stop"
    assert "usage" in data and data["usage"]["total_tokens"] > 0


def test_models_endpoint():
    api, _ = _api()
    status, _, raw = api.handle("GET", "/v1/models",
                                {"Authorization": "Bearer secret-token-123456"}, b"")
    assert status == 200
    data = json.loads(raw)
    assert data["data"][0]["id"] == "oddd"


def test_unknown_path_404():
    api, _ = _api()
    status, _, _ = api.handle("GET", "/v1/nope",
                              {"Authorization": "Bearer secret-token-123456"}, b"")
    assert status == 404


# ---------- 表情包：微信不认 [sticker:xxx] ----------

def test_sticker_marker_becomes_image_link():
    """外部渠道收到标记会原样显示成代码（Telegram 线上就是这样）。"""
    api, _ = _api("行，收工。\n[sticker:点赞]")
    status, _, raw = _post(api, {"messages": [{"role": "user", "content": "收工"}]})
    assert status == 200
    text = json.loads(raw)["choices"][0]["message"]["content"]
    assert "[sticker:" not in text
    assert "https://od1.site/stickers/thumbsup.gif" in text


def test_sticker_only_reply_still_returns_content():
    """只发表情时 content 不能是空串（有些客户端会当成失败）。"""
    api, _ = _api("[sticker:无语]")
    _, _, raw = _post(api, {"messages": [{"role": "user", "content": "嗯"}]})
    text = json.loads(raw)["choices"][0]["message"]["content"]
    assert text and "speechless.gif" in text


# ---------- 限流 ----------

def test_rate_limit_blocks_burst():
    api, calls = _api(max_per_min=2)
    for _ in range(2):
        assert _post(api, {"messages": [{"role": "user", "content": "hi"}]})[0] == 200
    status, _, _ = _post(api, {"messages": [{"role": "user", "content": "hi"}]})
    assert status == 401
    assert len(calls) == 2


# ---------- 状态 ----------

def test_describe_tracks_hits_and_errors():
    api, _ = _api()
    _post(api, {"messages": [{"role": "user", "content": "hi"}]})
    st = api.describe()
    assert st["hits"] == 1 and st["ready"] is True

    def boom(prompt):
        raise RuntimeError("模型挂了")

    bad = OpenAICompat(token="k" * 20, ask_fn=boom)
    bad.handle("POST", "/v1/chat/completions", {"Authorization": "Bearer " + "k" * 20},
               json.dumps({"messages": [{"role": "user", "content": "x"}]}).encode())
    assert bad.describe()["errors"] == 1
    assert "模型挂了" in bad.describe()["lastError"]


def test_agent_failure_returns_500_not_crash():
    def boom(prompt):
        raise RuntimeError("队列已满")

    api = OpenAICompat(token="t" * 20, ask_fn=boom)
    status, _, raw = api.handle(
        "POST", "/v1/chat/completions", {"Authorization": "Bearer " + "t" * 20},
        json.dumps({"messages": [{"role": "user", "content": "x"}]}).encode())
    assert status == 500
    assert "Agent 执行失败" in json.loads(raw)["error"]["message"]


def test_get_on_completions_is_405():
    api, _ = _api()
    status, _, _ = api.handle("GET", "/v1/chat/completions",
                              {"Authorization": "Bearer secret-token-123456"}, b"")
    assert status == 405
