"""OpenAI 兼容接口：把 ODDD 的 Agent 暴露成一个「模型」给 OpenClaw 用。

## 为什么是这条路

OpenClaw 是个自托管的 agent 网关，靠 `models.providers` 指向某个
OpenAI 兼容端点（你截图里它在用 ZCode 当后端，说明通道就是这么接的）。
所以让微信里出现 ODDD 的人设，最稳的做法不是去逆向它的 Gateway WS 协议，
而是**我们按标准 OpenAI 形状提供服务**，把它配成一个 provider：

    models.providers.oddd = {
      baseUrl: "https://od1.site/v1",
      apiKey:  "<设置页生成的令牌>",
      models:  [{ id: "oddd", name: "ODDD" }],
    }

好处：协议是公开标准，OpenClaw 升级换版本也不影响；失败时能拿 curl 复现。

## 只喂最后一条用户消息

OpenClaw 是 agent，它发过来的 `messages` 里带着**它自己的** system prompt
和完整历史。整段塞给我们的 Agent，它就会去回答 OpenClaw 的系统提示词
（"你是一个..."），而不是微信里那个人问的那句话。所以这里只取最后一条
user 消息的文本——人真正打出来的那句。多轮记忆由我们自己的会话负责，
和网页端/Telegram 共用同一份上下文。

## 安全

这个端点跑在公网端口上、后面连着能真实下单的 Agent，所以：
  · **默认关闭**，没在设置里启用就不挂载（404）；
  · 启用后**必须**有访问令牌，没配令牌时直接 401——绝不允许匿名访问，
    否则任何人扫到这个路径就能替你开仓；
  · 令牌比对用 hmac.compare_digest（定时安全）；
  · 有每分钟请求上限，防止对方陷入重试循环时把 Agent 队列打满。
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import re
import threading
import time
import uuid

DEFAULT_MODEL = "oddd"
DEFAULT_TIMEOUT = 180.0
DEFAULT_MAX_PER_MIN = 40
# 单条消息上限：OpenClaw 偶尔会把长上下文一起塞进来，超过就直接拒绝，
# 不要把几 MB 文本喂进模型（既慢又贵）。
MAX_PROMPT_CHARS = 12000
MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_IMAGE_BYTES = 2_000_000


def _now() -> int:
    return int(time.time())


def _new_id() -> str:
    return "chatcmpl-" + uuid.uuid4().hex[:24]


def _num_tokens(text: str) -> int:
    """粗略 token 估算（中文约 1 字 1 token，英文约 4 字符 1 token）。

    只用于响应里的 usage 字段——部分客户端在没有 usage 时会报错，
    但没人会拿它做计费，所以不值得为此引入分词器依赖。
    """
    text = str(text or "")
    han = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return max(1, han + (len(text) - han) // 4)


class AuthError(Exception):
    pass


class _AgentFailed(Exception):
    """Agent 执行本身失败。由 _reply 抛出并已记过账，handle 不再重复统计。"""


class OpenAICompat:
    """把 (prompt, attachments) -> reply 的调用包成 OpenAI 兼容 HTTP 接口。

    ask_fn 是**同步**可调用（main 里用 run_coroutine_threadsafe 桥到事件循环），
    因为 HTTP handler 本身跑在线程池里。
    """

    def __init__(self, token: str = "", model: str = DEFAULT_MODEL,
                 ask_fn=None, timeout: float = DEFAULT_TIMEOUT,
                 max_per_min: int = DEFAULT_MAX_PER_MIN,
                 sticker_dir: str = "", attach_dir: str = "",
                 public_dir: str = "", base_url: str = "https://od1.site"):
        self.token = str(token or "").strip()
        self.model = str(model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        self.ask_fn = ask_fn
        self.timeout = float(timeout or DEFAULT_TIMEOUT)
        self.max_per_min = int(max_per_min or DEFAULT_MAX_PER_MIN)
        # 表情包：外部渠道（微信）不认 `[sticker:xxx]` 标记，要换成真正的图片。
        self.sticker_dir = sticker_dir
        self.attach_dir = attach_dir
        self.public_dir = public_dir
        self.base_url = base_url
        self._lock = threading.Lock()
        self._calls: list[float] = []      # 最近一分钟的请求时刻
        self._hits = 0
        self._errors = 0
        self._last_error = ""
        self._last_reply_at = 0

    # ---------- 表情包落地 ----------

    def render_reply(self, text: str) -> str:
        """把 `[sticker:xxx]` 换成 Markdown 图片。

        微信/OpenClaw 收到的是纯文本，原样转发标记的话用户看到的就是
        `[sticker:崩溃]` 这串代码（Telegram 那边线上已经这么翻过车了）。
        这里换成 `![](https://.../stickers/x.gif)`，客户端能渲染就渲染，
        不能也会给个可点的链接。没有表情标记时原样返回。
        """
        body = str(text or "")
        if "[sticker:" not in body and "[sticker：" not in body:
            return body
        try:
            from ai.stickers_media import to_markdown
            return to_markdown(body, self.base_url, self.public_dir,
                               self.attach_dir, self.sticker_dir)
        except Exception:
            # 渲染失败绝不能把整条回复搞丢：退回原始文本，至少话还在
            return body

    # ---------- 状态（设置页展示用） ----------

    def describe(self) -> dict:
        with self._lock:
            recent = len([t for t in self._calls if _now() - t < 60])
        return {
            "ready": bool(self.token and self.ask_fn),
            "hasToken": bool(self.token),
            "model": self.model,
            "hits": self._hits,
            "errors": self._errors,
            "lastError": self._last_error,
            "lastReplyAt": self._last_reply_at,
            "callsLastMinute": recent,
            "maxPerMinute": self.max_per_min,
        }

    # ---------- 鉴权与限流 ----------

    def check_auth(self, headers: dict) -> None:
        """没有令牌就不放行——这个端点后面是能下单的 Agent。"""
        if not self.token:
            raise AuthError("服务端尚未配置访问令牌，接口未开放")
        supplied = ""
        raw = ""
        for key, value in (headers or {}).items():
            if str(key).lower() == "authorization":
                raw = str(value or "")
                break
        if raw.lower().startswith("bearer "):
            supplied = raw[7:].strip()
        # 也接受 x-api-key：部分客户端/网关走这个头
        if not supplied:
            for key, value in (headers or {}).items():
                if str(key).lower() in ("x-api-key", "api-key"):
                    supplied = str(value or "").strip()
                    break
        if not supplied or not hmac.compare_digest(supplied, self.token):
            raise AuthError("访问令牌无效")

    def _rate_limit(self) -> None:
        now = _now()
        with self._lock:
            self._calls = [t for t in self._calls if now - t < 60]
            if len(self._calls) >= self.max_per_min:
                raise AuthError(
                    f"请求过于频繁（每分钟上限 {self.max_per_min} 次），请稍后再试")
            self._calls.append(now)

    # ---------- 请求解析 ----------

    @staticmethod
    def extract_prompt(body: dict) -> tuple[str, list[dict]]:
        """从 messages 里取「人真正说的最后一句」+ 图片附件。

        只认最后一条 role=user 的消息：
          · 前面的 user 消息是 OpenClaw 拼的历史，我们已经有自己的上下文，
            重复喂会让同一件事出现两遍，模型开始"我记得你刚才说过"；
          · system 消息是 OpenClaw 自己的人设，不能当用户问题。
        content 既可能是字符串，也可能是多模态数组
        （[{"type":"text","text":"..."},{"type":"image_url",...}]）。
        """
        messages = body.get("messages")
        if not isinstance(messages, list):
            return "", []
        text, images = OpenAICompat._last_user_text(messages)
        return text[:MAX_PROMPT_CHARS], images

    @staticmethod
    def _last_user_text(messages: list) -> tuple[str, list[dict]]:
        """最后一条 user 消息里的文字与图片。"""
        prompt = ""
        images: list[dict] = []
        for msg in messages:
            if not isinstance(msg, dict) or msg.get("role") != "user":
                continue
            content = msg.get("content")
            texts: list[str] = []
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    kind = str(part.get("type") or "")
                    if kind in ("text", "input_text"):
                        texts.append(str(part.get("text") or ""))
                    elif kind in ("image_url", "input_image"):
                        url = part.get("image_url")
                        if isinstance(url, dict):
                            url = url.get("url")
                        got = OpenAICompat._decode_image(url)
                        if got:
                            images.append(got)
            # 只保留最后一条 user 的内容
            prompt = "\n".join(t for t in texts if t).strip() or prompt
            images = images[-4:] or []
        return prompt, images

    @staticmethod
    def _decode_image(url) -> dict | None:
        """data URL → 附件结构（与网页端/Telegram 完全一致的形状）。

        只接受 data: 内联图片，不接受 http 地址：服务端去拉任意 URL 等于
        给了一个开放代理（还能打到内网），风险和收益完全不成比例。
        """
        text = str(url or "")
        m = re.match(r"^data:([^;,]+);base64,(.+)$", text, re.S)
        if not m:
            return None
        mime = m.group(1).strip().lower()
        if not mime.startswith("image/"):
            return None
        try:
            raw = base64.b64decode(m.group(2), validate=True)
        except Exception:
            return None
        if not raw or len(raw) > MAX_IMAGE_BYTES:
            return None
        return {"kind": "image", "name": "openclaw.png", "mime": mime,
                "data": "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode("ascii"))}

    # ---------- 响应构造 ----------

    def _dump_request(self, payload: dict, prompt: str) -> None:
        """把收到的请求摘要落到文件，用于排查"用户的话到底放在哪个字段"。

        只在 OPENCLAW_DUMP 指向一个路径时启用——默认关闭，不写盘、不占空间。
        存的是**结构摘要**（每条消息的角色 + 前 400 字），不是完整上下文：
        排查看的是"OpenClaw 把微信消息塞在哪"，不需要整份历史。
        """
        path = str(os.environ.get("OPENCLAW_DUMP") or "").strip()
        if not path:
            return
        try:
            messages = payload.get("messages") or []
            summary = []
            for msg in messages if isinstance(messages, list) else []:
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                if isinstance(content, list):
                    content = " | ".join(
                        f"{p.get('type')}:{str(p.get('text') or p.get('image_url') or '')[:120]}"
                        for p in content if isinstance(p, dict))
                summary.append({
                    "role": msg.get("role"),
                    "len": len(str(content or "")),
                    "text": str(content or "")[:400],
                })
            row = {"ts": _now(), "extracted": prompt[:200],
                   "model": payload.get("model"), "messages": summary}
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            pass                                    # 排查工具本身不能影响回复

    def _reply(self, prompt: str, images: list[dict]) -> str:
        if not self.ask_fn:
            raise RuntimeError("Agent 未接入")
        with self._lock:
            self._hits += 1
        try:
            text = self.ask_fn(prompt, images) if images else self.ask_fn(prompt)
        except Exception as exc:
            with self._lock:
                self._errors += 1
                self._last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            raise _AgentFailed(str(exc)) from exc
        with self._lock:
            self._last_reply_at = _now()
            self._last_error = ""
        return self.render_reply(str(text or "").strip())

    def completion(self, prompt: str, images: list[dict]) -> dict:
        text = self._reply(prompt, images)
        return {
            "id": _new_id(), "object": "chat.completion", "created": _now(),
            "model": self.model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": _num_tokens(prompt),
                      "completion_tokens": _num_tokens(text),
                      "total_tokens": _num_tokens(prompt) + _num_tokens(text)},
        }

    def stream_chunks(self, prompt: str, images: list[dict]) -> list[str]:
        """SSE 分片。回复是一次性算出来的，所以只发一个内容增量 + 结束帧。

        不假装逐字流式：伪造打字效果只会让客户端多等一轮，
        而且用户看到的停顿位置是假的。
        """
        text = self._reply(prompt, images)
        cid = _new_id()
        created = _now()
        head = {
            "id": cid, "object": "chat.completion.chunk", "created": created,
            "model": self.model,
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": text},
                         "finish_reason": None}],
        }
        tail = {
            "id": cid, "object": "chat.completion.chunk", "created": created,
            "model": self.model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": _num_tokens(prompt),
                      "completion_tokens": _num_tokens(text),
                      "total_tokens": _num_tokens(prompt) + _num_tokens(text)},
        }
        return [f"data: {json.dumps(head, ensure_ascii=False)}\n\n",
                f"data: {json.dumps(tail, ensure_ascii=False)}\n\n",
                "data: [DONE]\n\n"]

    def models(self) -> dict:
        return {
            "object": "list",
            "data": [{"id": self.model, "object": "model", "created": 0,
                      "owned_by": "oddd"}],
        }

    # ---------- 入口 ----------

    def handle(self, method: str, path: str, headers: dict,
               body: bytes) -> tuple[int, dict, bytes]:
        """返回 (status, response_headers, body)。纯函数式，便于单测。"""
        path = str(path or "").split("?", 1)[0].rstrip("/") or "/"
        method = str(method or "GET").upper()
        try:
            if path == "/v1/models":
                if method != "GET":
                    return self._json(405, {"error": "只支持 GET"})
                self.check_auth(headers)
                return self._json(200, self.models())
            if path in ("/v1/chat/completions", "/chat/completions"):
                if method != "POST":
                    return self._json(405, {"error": "只支持 POST"})
                self.check_auth(headers)
                self._rate_limit()
                if len(body or b"") > MAX_BODY_BYTES:
                    return self._json(413, {"error": "请求体过大"})
                try:
                    payload = json.loads((body or b"{}").decode("utf-8", "replace"))
                except ValueError:
                    return self._json(400, {"error": "请求体不是合法 JSON"})
                if not isinstance(payload, dict):
                    return self._json(400, {"error": "请求体必须是 JSON 对象"})
                prompt, images = self.extract_prompt(payload)
                self._dump_request(payload, prompt)
                if not prompt and not images:
                    return self._json(400, {"error": "messages 里没有用户内容"})
                if payload.get("stream"):
                    chunks = self.stream_chunks(prompt, images)
                    return 200, {"Content-Type": "text/event-stream; charset=utf-8",
                                 "Cache-Control": "no-cache"}, "".join(chunks).encode()
                return self._json(200, self.completion(prompt, images))
            return self._json(404, {"error": f"未知路径 {path}"})
        except AuthError as exc:
            return self._json(401, {"error": {"message": str(exc), "type": "auth_error"}})
        except _AgentFailed as exc:
            # _reply 已经记过错误、写过 lastError，这里只负责转成 HTTP 响应。
            # 早先两边都记一次，界面上"错误数"是真实次数的两倍。
            return self._json(500, {"error": {"message": f"Agent 执行失败: {str(exc)[:200]}",
                                              "type": "agent_error"}})
        except Exception as exc:
            with self._lock:
                self._errors += 1
                self._last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            return self._json(500, {"error": {"message": f"服务异常: {str(exc)[:200]}",
                                              "type": "server_error"}})

    @staticmethod
    def _json(status: int, payload: dict) -> tuple[int, dict, bytes]:
        return status, {"Content-Type": "application/json; charset=utf-8"}, \
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
