"""AI 模型接入层：配置持久化 + 模型列表探测 + 连通性测试 + 对话请求。

支持 OpenAI 兼容协议（/chat/completions、/models），覆盖绝大多数服务商：
  OpenAI / DeepSeek / Moonshot / 智谱 / 阿里通义 / 硅基流动 / OpenRouter /
  其他 OpenAI 兼容端点。

职责边界（刻意保持很窄）：
  · 只做「读配置 / 写配置 / 列模型 / 测连通 / 发对话请求」，不在这里执行工具。
  · API Key 不回传明文到浏览器：前端拿到的是掩码，保存时留空即沿用原值。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
TIMEOUT = 20
MAX_RESPONSE = 8 * 1024 * 1024
MASK = "••••••••"

# 常见服务商预设：前端下拉直接选，省得手填端点
PROVIDER_PRESETS = [
    {"id": "openai", "label": "OpenAI", "base": "https://api.openai.com/v1"},
    {"id": "deepseek", "label": "DeepSeek", "base": "https://api.deepseek.com/v1"},
    {"id": "moonshot", "label": "Moonshot 月之暗面", "base": "https://api.moonshot.cn/v1"},
    {"id": "zhipu", "label": "智谱 GLM", "base": "https://open.bigmodel.cn/api/paas/v4"},
    {"id": "qwen", "label": "阿里通义千问", "base": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
    {"id": "siliconflow", "label": "硅基流动", "base": "https://api.siliconflow.cn/v1"},
    {"id": "openrouter", "label": "OpenRouter", "base": "https://openrouter.ai/api/v1"},
    {"id": "custom", "label": "自定义", "base": ""},
]

# 模型能力标记
CAPABILITIES = [
    {"id": "text", "label": "文本"},
    {"id": "vision", "label": "识图"},
    {"id": "pdf", "label": "PDF"},
    {"id": "video", "label": "视频"},
]


def _request(url: str, key: str = "", payload: dict | None = None,
             proxy: str | None = None, timeout: int = TIMEOUT) -> tuple[int, dict | str | None]:
    """统一请求：返回 (status, body)。body 为解析后的 JSON 或错误文本。"""
    headers = {"User-Agent": UA, "Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data else "GET")

    def read_body(response):
        raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise ValueError("模型响应超过 8MB 限制")
        return getattr(response, "status", 200), _maybe_json(raw.decode("utf-8", "replace"))

    try:
        if proxy:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler(
                {"http": proxy, "https": proxy}))
            with opener.open(req, timeout=timeout) as r:
                return read_body(r)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return read_body(r)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read(2000).decode("utf-8", "replace")[:500]
        except Exception:
            pass
        return e.code, _maybe_json(body) if body else None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        return 0, str(e)[:200]


def _maybe_json(raw: str):
    try:
        return json.loads(raw)
    except Exception:
        return raw[:500]


def _join(base: str, path: str) -> str:
    b = (base or "").strip().rstrip("/")
    return f"{b}{path}"


def list_models(base: str, key: str = "", proxy: str | None = None) -> dict:
    """GET {base}/models → 归一化为 [{id, owned_by}]。

    个别服务商路径不同（如智谱没有 /models），失败时返回可读错误，
    前端提示用户手动填模型名。
    """
    if not base:
        return {"ok": False, "error": "未填写 API 端点", "models": []}
    status, body = _request(_join(base, "/models"), key, proxy=proxy)
    if status == 0:
        return {"ok": False, "error": f"连接失败：{body}", "models": []}
    if status >= 400:
        msg = ""
        if isinstance(body, dict):
            msg = (body.get("error") or {}).get("message") if isinstance(body.get("error"), dict) else body.get("error")
        return {"ok": False, "status": status,
                "error": str(msg or body or f"HTTP {status}")[:300], "models": []}
    arr = []
    if isinstance(body, dict):
        arr = body.get("data") or body.get("models") or []
    out = []
    for m in arr if isinstance(arr, list) else []:
        if isinstance(m, str):
            out.append({"id": m, "owned_by": ""})
        elif isinstance(m, dict):
            mid = m.get("id") or m.get("name") or m.get("model")
            if mid:
                out.append({"id": str(mid), "owned_by": str(m.get("owned_by") or m.get("provider") or "")})
    out.sort(key=lambda x: x["id"])
    return {"ok": True, "models": out, "count": len(out)}


# 常见模型的默认能力/上下文（用户可覆盖；未知模型给保守默认）
MODEL_DEFAULTS = [
    (("gpt-4o", "gpt-4.1", "gpt-5"), {"caps": ["text", "vision", "pdf"], "ctx": 128000}),
    (("o1", "o3", "o4"), {"caps": ["text", "vision"], "ctx": 200000}),
    (("deepseek",), {"caps": ["text"], "ctx": 65536}),
    (("claude",), {"caps": ["text", "vision", "pdf"], "ctx": 200000}),
    (("gemini",), {"caps": ["text", "vision", "pdf", "video"], "ctx": 1000000}),
    (("qwen", "qvq"), {"caps": ["text", "vision"], "ctx": 131072}),
    (("glm-4v",), {"caps": ["text", "vision"], "ctx": 8192}),
    (("glm",), {"caps": ["text"], "ctx": 131072}),
    (("moonshot", "kimi"), {"caps": ["text"], "ctx": 131072}),
    (("llava", "minicpm-v", "internvl"), {"caps": ["text", "vision"], "ctx": 32768}),
]


def guess_model_meta(model_id: str) -> dict:
    """按模型名猜默认能力与上下文长度（仅作初值，前端可改）。"""
    mid = (model_id or "").lower()
    for keys, meta in MODEL_DEFAULTS:
        if any(k in mid for k in keys):
            return {"caps": list(meta["caps"]), "ctx": meta["ctx"]}
    return {"caps": ["text"], "ctx": 8192}


def test_model(cfg: dict, model_id: str, proxy: str | None = None) -> dict:
    """发一条最小 chat 请求验证：端点/密钥/模型名是否可用。

    用 max_tokens=1 把成本压到最低；记录耗时便于判断网络质量。
    """
    base = (cfg.get("base") or "").strip()
    key = (cfg.get("key") or "").strip()
    if not base or not model_id:
        return {"ok": False, "error": "端点或模型未填写"}
    t0 = time.time()
    status, body = _request(
        _join(base, "/chat/completions"), key,
        {"model": model_id, "messages": [{"role": "user", "content": "ping"}],
         "max_tokens": 1, "stream": False},
        proxy=proxy,
    )
    ms = int((time.time() - t0) * 1000)
    if status == 0:
        return {"ok": False, "error": f"连接失败：{body}", "ms": ms}
    if status >= 400:
        msg = ""
        if isinstance(body, dict):
            err = body.get("error")
            msg = err.get("message") if isinstance(err, dict) else err or body.get("message")
        return {"ok": False, "status": status, "ms": ms,
                "error": str(msg or body or f"HTTP {status}")[:300]}
    reply = ""
    if isinstance(body, dict):
        choices = body.get("choices") or []
        if choices:
            reply = (choices[0].get("message") or {}).get("content") or ""
    return {"ok": True, "ms": ms, "reply": str(reply)[:100]}


def _chat_completion_stream(base: str, key: str, model_id: str, payload: dict,
                            proxy: str | None, timeout: float, on_progress):
    """SSE 流式请求。返回 None 表示流式这条路走不通，调用方应退回非流式。

    只有真正拿到事件才算走通；服务商忽略 stream 参数、返回 JSON、瞬间断流
    都按「走不通」处理，交给非流式重来，不让统计影响对话本身。

    on_progress(chars, ttft_ms, deltas, elapsed_ms, text) 在每次收到可见内容时
    回调（跑在请求线程里）：
      ttft_ms   从请求发出算起、到第一个 token（含思维链）为止，和最终 ttftMs 同一定义；
      deltas    已收到的内容增量帧数，OpenAI 协议下通常一帧一个 token，用来估实时 TPS；
      elapsed_ms 从请求发出到现在，用于算解码窗口；
      text      目前累计的**全文**，前端据此做增量渲染（真正的流式）。
    """
    import urllib.request

    body_payload = dict(payload)
    body_payload["stream"] = True
    # 多数服务商默认不在流里带 usage；显式要求，拿不到就只算得出耗时
    body_payload["stream_options"] = {"include_usage": True}
    headers = {"User-Agent": UA, "Accept": "text/event-stream",
               "Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    url = _join(base, "/chat/completions")
    data = json.dumps(body_payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")

    started = time.monotonic()
    try:
        if proxy:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler(
                {"http": proxy, "https": proxy}))
            response = opener.open(request, timeout=min(180, max(1, timeout)))
        else:
            response = urllib.request.urlopen(request, timeout=min(180, max(1, timeout)))
    except urllib.error.HTTPError as e:
        # 服务商明确拒绝：这是真实答复，不能重试（否则会打两次）
        raw = ""
        try:
            raw = e.read(2000).decode("utf-8", "replace")[:500]
        except Exception:
            pass
        response = None
        ms = int((time.monotonic() - started) * 1000)
        body = _maybe_json(raw) if raw else None
        return _interpret(e.code, body, ms, key, model_id, payload.get("tools"),
                          payload.get("tool_choice"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None

    parts: list[str] = []
    reasoning_parts: list[str] = []
    calls: dict[int, dict] = {}
    usage: dict = {}
    model_out = ""
    finish = None
    first_at = None
    last_at = None
    events = 0
    chars = 0
    deltas = 0
    try:
        with response:
            buffer = b""
            while True:
                chunk = response.read(8192)
                if not chunk:
                    break
                buffer += chunk
                # 按换行切帧：一次 read 可能带回多帧，readline 的 framing 不可依赖
                while b"\n" in buffer:
                    raw_line, buffer = buffer.split(b"\n", 1)
                    line = raw_line.strip()
                    if not line.startswith(b"data:"):
                        continue
                    text = line[5:].strip().decode("utf-8", "replace")
                    if text == "[DONE]":
                        continue
                    try:
                        event = json.loads(text)
                    except Exception:
                        continue
                    if not isinstance(event, dict):
                        continue
                    events += 1
                    if isinstance(event.get("usage"), dict):
                        usage = event["usage"]
                    if not model_out and isinstance(event.get("model"), str):
                        model_out = event["model"]
                    for choice in event.get("choices") or []:
                        if not isinstance(choice, dict):
                            continue
                        if choice.get("finish_reason"):
                            finish = choice["finish_reason"]
                        delta = choice.get("delta") or {}
                        if not isinstance(delta, dict):
                            continue
                        # reasoning_content 也是模型在吐 token（思考型模型的思维链），
                        # 首字延迟和解码窗口都得把它算进去，否则 TPS 会虚高一截。
                        # 同时要收集起来：DeepSeek 思考模式要求下一轮把思维链原样回传，
                        # 丢掉它会让多轮工具调用直接 400。
                        reasoning = delta.get("reasoning_content")
                        if isinstance(reasoning, str) and reasoning:
                            now = time.monotonic()
                            if first_at is None:
                                first_at = now
                            last_at = now
                            reasoning_parts.append(reasoning)
                        piece = delta.get("content")
                        if isinstance(piece, str) and piece:
                            now = time.monotonic()
                            if first_at is None:
                                first_at = now
                            last_at = now
                            parts.append(piece)
                            chars += len(piece)
                            deltas += 1
                            if on_progress:
                                # 首字按 first_at 算（含思维链），与最终 ttftMs 一致；
                                # 用 now 会让实时值偏大，结束后数字突然变小。
                                # 第 5 个参数是**目前累计的全文**，前端据此增量渲染。
                                ttft = int(((first_at or now) - started) * 1000)
                                on_progress(chars, ttft, deltas,
                                            int((now - started) * 1000), "".join(parts))
                        for delta_call in delta.get("tool_calls") or []:
                            if not isinstance(delta_call, dict):
                                continue
                            index = delta_call.get("index", 0)
                            slot = calls.setdefault(index, {"id": "", "type": "function",
                                                            "function": {"name": "", "arguments": ""}})
                            if delta_call.get("id"):
                                slot["id"] = delta_call["id"]
                            fn = delta_call.get("function") or {}
                            if isinstance(fn.get("name"), str):
                                slot["function"]["name"] += fn["name"]
                            if isinstance(fn.get("arguments"), str):
                                slot["function"]["arguments"] += fn["arguments"]
    except Exception:
        # 读到一半断开：已经拿到的内容仍然可用，交给下面的统一校验判断
        pass

    if events == 0:
        return None                      # 服务商没按流式回，改走非流式

    ms = int((time.monotonic() - started) * 1000)
    timing = {
        "streamed": True,
        "ttftMs": int((first_at - started) * 1000) if first_at else 0,
        "decodeMs": int((last_at - first_at) * 1000) if first_at and last_at else 0,
        "chars": chars,
    }
    body = {
        "model": model_out,
        "usage": usage,
        "choices": [{
            "finish_reason": finish,
            "message": {"role": "assistant",
                        "content": "".join(parts) or None,
                        # 思考型服务商要求下一轮把思维链原样带回，缺了会 400
                        **({"reasoning_content": "".join(reasoning_parts)}
                           if reasoning_parts else {}),
                        **({"tool_calls": [calls[k] for k in sorted(calls)]} if calls else {})},
        }],
    }
    return _interpret(200, body, ms, key, model_id, payload.get("tools"),
                      payload.get("tool_choice"), timing)


def chat_completion(cfg: dict, model_id: str, messages: list[dict],
                    proxy: str | None = None, max_tokens: int = 1800,
                    timeout: float = 150, tools: list[dict] | None = None,
                    tool_choice: str | dict | None = None,
                    stream: bool = False, on_progress=None) -> dict:
    """OpenAI 兼容对话请求；保留 tool_calls，绝不降级重试为纯文本执行。

    stream=True 时走 SSE：同一条链路上能拿到真实的「首 token 延迟」和
    「解码速度」，非流式只能算端到端耗时（含提示词预填充，偏低）。
    流式不可用（服务商不支持 / 代理异常 / 没吐出任何事件）时自动退回非流式，
    保证对话本身绝不因为统计而失败。
    """
    base = (cfg.get("base") or "").strip()
    key = (cfg.get("key") or "").strip()
    if not base or not model_id:
        return {"ok": False, "error": "未配置 AI 端点或模型"}
    payload = {"model": model_id, "messages": messages,
               "max_tokens": max_tokens, "stream": False}
    if tools is not None:
        payload.update(tools=tools, tool_choice=tool_choice or "auto")
    if stream:
        result = _chat_completion_stream(base, key, model_id, payload, proxy,
                                         timeout, on_progress)
        # 只有「完全没拿到响应」才退回非流式；4xx/5xx 是服务商的真实答复，不能重试。
        if result is not None:
            return result
    started = time.monotonic()
    status, body = _request(_join(base, "/chat/completions"), key, payload,
                            proxy=proxy, timeout=min(180, max(1, timeout)))
    ms = int((time.monotonic() - started) * 1000)
    return _interpret(status, body, ms, key, model_id, tools, tool_choice)


def _interpret(status: int, body, ms: int, key: str, model_id: str,
               tools: list[dict] | None, tool_choice, timing: dict | None = None) -> dict:
    """把服务商返回归一化成统一结果，流式与非流式共用同一套校验。"""
    def failure(detail):
        # 服务商可能回显认证数据；不能进入聊天/持久化状态。
        text = str(detail)
        if key:
            text = text.replace(key, "[REDACTED]")
        out = {"ok": False, "error": text[:600], "ms": ms, "status": status}
        if timing:
            out.update(timing)
        return out

    if status == 0:
        return failure(f"连接失败：{body}")
    if status >= 400:
        detail = ""
        if isinstance(body, dict):
            err = body.get("error")
            detail = err.get("message") if isinstance(err, dict) else err or body.get("message")
        prefix = "工具请求失败（服务商可能不支持 tools/tool_choice），未降级执行：" if tools is not None else ""
        return failure(prefix + str(detail or body or f"HTTP {status}"))
    choices = body.get("choices") if isinstance(body, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return failure("模型返回 choices 格式无效或为空")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        return failure("模型返回 message 格式无效")
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    if content is not None and not isinstance(content, str):
        return failure("模型返回 content 格式无效")
    reply = (content or "").strip()
    calls = message.get("tool_calls") or []
    if not isinstance(calls, list) or len(calls) > 8:
        return failure("模型 tool_calls 格式无效或超过每轮 8 次限制")
    seen = set()
    for call in calls:
        if not isinstance(call, dict) or call.get("type") != "function":
            return failure("模型工具调用必须为 function")
        function = call.get("function")
        call_id = call.get("id")
        if (not isinstance(call_id, str) or not call_id or len(call_id) > 200
                or call_id in seen or not isinstance(function, dict)
                or not isinstance(function.get("name"), str)
                or not isinstance(function.get("arguments"), str)
                or len(function["arguments"]) > 4096):
            return failure("模型工具调用参数或 ID 无效")
        seen.add(call_id)
    if tool_choice == "required" and not calls:
        return failure("服务商未返回必需的工具调用，不支持 tools 或忽略 tool_choice；未执行")
    if choice.get("finish_reason") in ("length", "content_filter"):
        return failure("模型输出被截断或过滤，不能作为已完成结果")
    if not reply and not calls:
        return failure("模型未返回文本或工具调用")
    out = {"ok": True, "reply": reply, "tool_calls": calls,
           # 非流式同样要保留思维链：下一轮要原样回传（思考型服务商的要求）
           "message": {"role": "assistant", "content": reply or None,
                       **({"reasoning_content": message["reasoning_content"]}
                          if isinstance(message.get("reasoning_content"), str)
                          and message["reasoning_content"] else {}),
                       **({"tool_calls": calls} if calls else {})},
           "finish_reason": choice.get("finish_reason"), "ms": ms,
           "usage": body.get("usage") or {}, "model": body.get("model") or model_id}
    if timing:
        out.update(timing)
    return out


def mask_cfg(ai: dict) -> dict:
    """给前端的副本：密钥只显示掩码，其余原样。"""
    out = dict(ai or {})
    key = str(out.get("key") or "")
    out["key"] = MASK if key else ""
    out["hasKey"] = bool(key)
    return out


def merge_cfg(old: dict, new: dict) -> dict:
    """保存时合并：key 为掩码/空 → 沿用旧密钥（掩码绝不能写回配置）。"""
    merged = dict(old or {})
    for k, v in (new or {}).items():
        if k in ("providers", "models", "capabilities", "key", "hasKey"):
            continue
        merged[k] = v
    key = str((new or {}).get("key") or "")
    if key and MASK not in key:
        merged["key"] = key[:400]                      # 用户填了新密钥
    else:
        merged["key"] = str((old or {}).get("key") or "")   # 掩码/留空 → 沿用旧值
    return merged
