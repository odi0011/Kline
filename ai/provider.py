"""AI 模型接入层：配置持久化 + 模型列表探测 + 连通性测试。

支持 OpenAI 兼容协议（/chat/completions、/models），覆盖绝大多数服务商：
  OpenAI / DeepSeek / Moonshot / 智谱 / 阿里通义 / 硅基流动 / OpenRouter /
  其他 OpenAI 兼容端点。

职责边界（刻意保持很窄）：
  · 只做「读配置 / 写配置 / 列模型 / 测连通」，不在这里跑推理。
    推理放在将来的 AI 复盘模块里，按需读取这里的配置。
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
             proxy: str | None = None) -> tuple[int, dict | str | None]:
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
    try:
        if proxy:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler(
                {"http": proxy, "https": proxy}))
            with opener.open(req, timeout=TIMEOUT) as r:
                raw = r.read().decode("utf-8", "replace")
                return getattr(r, "status", 200), _maybe_json(raw)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read().decode("utf-8", "replace")
            return getattr(r, "status", 200), _maybe_json(raw)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:500]
        except Exception:
            pass
        return e.code, _maybe_json(body) if body else None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
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
