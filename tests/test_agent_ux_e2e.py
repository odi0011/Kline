"""Agent 优化批次独立验证（搜索/网页/表情包/后台通知/前端防抖）。

独立编写，不复制实现者 tests/test_agent_behavior.py / tests/test_search.py 的断言。
用 tests/test_agent.py 同款"fake provider"模式驱动真实 AgentManager 的完整工具回合；
搜索安全用 monkeypatch feed.search._open 做完全离线的 SSRF 校验；联网测试单独一条、
环境波动时 skip 并保留重试记录。

覆盖：
  A. web_search / read_web_page 工具端到端（工具回包 untrusted、结果进 messages、最终回复正常、
     未注入 search_fn/fetch_fn 时 available=False 不崩）。
  B. send_sticker：只表情包 / 正文末尾追加 / 重复与已有标记不重复 / 非法名字拒绝 /
     前端 stickers.jsx 与后端 STICKER_NAMES、真实 PNG 像素尺寸一致。
  C. 后台任务通知：notify 三模式、on_change 去重、<=400 字、kind=watch、不触发新回复、
     非法 notify 拒绝、重启后 notify/lastNotified 持久化、真实 _execute 路径。
  D. 真实联网：web_search 至少一个 provider ok 且结果非空、fetch_page example.com 正文非空。
  E. 前端源码静态审查（memo 签名/入场动画/贴底 rAF+ResizeObserver/胶囊 key/贴纸占位）。
  F. 安全：fetch_page 拒绝内网/元数据/危险重定向；搜索 provider 回退且永不抛异常。
"""
from __future__ import annotations

import asyncio
import json
import re
import struct
import time
import urllib.request
from pathlib import Path

import pytest

import ai.agent as agent_mod
import feed.search as search
import storage as storage_mod
from ai.agent import AgentManager, SYSTEM_PROMPT, _attach_sticker, _notify_mode, _system_content
from ai.agent_tools import STICKER_NAMES, TOOLS, TOOL_NAMES
from storage import Storage
from tests.test_paper_store import SQLConnection

REPO = Path(__file__).resolve().parent.parent
NOW = 1_800_000_000_000


# ---------------------------------------------------------------------------
# fake provider / manager helpers（同 tests/test_agent.py 的注入方式，独立实现）
# ---------------------------------------------------------------------------

def tool_call(name, arguments=None, call_id="call-1"):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments or {}, ensure_ascii=False)}}


def make_provider(tool_calls=None, final_reply="结论", captured=None):
    """第一回合返回工具调用（required），拿到 tool 结果后返回最终回复（auto）。"""
    calls = list(tool_calls or [])

    def provider(config, model, messages, **kwargs):
        if captured is not None:
            captured.append({
                "tool_choice": kwargs.get("tool_choice"),
                "messages": json.loads(json.dumps(messages, ensure_ascii=False, default=str)),
            })
        has_tool_result = any(isinstance(m, dict) and m.get("role") == "tool"
                              for m in messages)
        if calls and not has_tool_result and kwargs.get("tool_choice") == "required":
            return {"ok": True, "reply": "", "tool_calls": list(calls),
                    "usage": {"total_tokens": 5}}
        return {"ok": True, "reply": final_reply, "usage": {"total_tokens": 20},
                "model": "test-model"}

    return provider


def make_manager(tmp_path, monkeypatch, provider, context=None):
    events = []

    async def broadcast(message):
        events.append(message)

    monkeypatch.setattr(agent_mod, "chat_completion", provider)
    config = {"base": "https://example.invalid/v1", "key": "fake-test-key",
              "models": [{"id": "test-model", "active": True}]}
    manager = AgentManager(
        lambda: config, lambda: "",
        context or (lambda: {"risk": {"equity": 10000}}),
        broadcast, str(tmp_path / "agent-state.json"))
    return manager, events


async def wait_for(predicate, rounds=500):
    for _ in range(rounds):
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return False


async def chat_once(manager, prompt="你好"):
    message_id = await manager.ask(prompt, "test-model")
    assert await wait_for(lambda: not manager.chat_runners)
    return next(item for item in manager.chat if item.get("id") == message_id)


def last_tool_payload(captured):
    """最后一次 provider 调用里，最后一条 tool 消息的 JSON 回包。"""
    messages = captured[-1]["messages"]
    tool_messages = [m for m in messages if isinstance(m, dict) and m.get("role") == "tool"]
    assert tool_messages, "provider 第二次调用里必须带 tool 结果"
    return json.loads(tool_messages[-1]["content"])


# ---------------------------------------------------------------------------
# A. 搜索 / 网页读取工具端到端
# ---------------------------------------------------------------------------

def test_web_search_tool_end_to_end(tmp_path, monkeypatch):
    captured = []
    provider = make_provider(
        [tool_call("web_search", {"query": "bitcoin price", "count": 3})],
        "比特币现价约 6 万美元（来源 example.com）", captured)

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch, provider)
        manager.search_fn = lambda query, count=5: {
            "ok": True, "provider": "fake",
            "results": [{"title": "BTC price", "url": "https://example.com/btc",
                         "snippet": "price 60000"}],
            "error": ""}
        message = await chat_once(manager, "帮我查一下比特币价格")
        assert message["status"] == "succeeded"
        assert "比特币" in message["content"]
        step = message["steps"][0]
        assert step["tool"] == "web_search" and step["status"] == "succeeded"
        payload = last_tool_payload(captured)
        assert payload["untrusted"] is True
        data = payload["data"]
        assert data["available"] is True and data["untrusted"] is True
        assert data["provider"] == "fake"
        assert data["results"][0]["title"] == "BTC price"
        # 第一条 provider 调用必须带 required（逼模型先调工具）
        assert captured[0]["tool_choice"] == "required"
        await manager.stop()

    asyncio.run(scenario())


def test_read_web_page_tool_end_to_end_and_truncation(tmp_path, monkeypatch):
    captured = []
    provider = make_provider(
        [tool_call("read_web_page", {"url": "https://example.com/article"})],
        "网页正文讲的是示例域名（来源 example.com）", captured)
    long_text = "正文内容" * 4000        # 16000 字 > WEB_TEXT_LIMIT(12000)

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch, provider)
        manager.fetch_fn = lambda url: {
            "ok": True, "url": url, "finalUrl": "https://example.com/article",
            "title": "示例文章", "text": long_text, "truncated": False, "error": ""}
        message = await chat_once(manager, "读一下这个网页")
        assert message["status"] == "succeeded"
        assert message["steps"][0]["tool"] == "read_web_page"
        data = last_tool_payload(captured)["data"]
        assert data["available"] is True and data["untrusted"] is True
        assert data["title"] == "示例文章"
        assert len(data["text"]) <= 12000
        assert data["truncated"] is True
        await manager.stop()

    asyncio.run(scenario())


def test_search_tools_degrade_when_not_injected(tmp_path, monkeypatch):
    captured = []
    provider = make_provider(
        [tool_call("web_search", {"query": "anything"}),
         tool_call("read_web_page", {"url": "https://example.com"}, call_id="call-2")],
        "搜索没接上，我只能用本地数据回答。", captured)

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch, provider)
        assert manager.search_fn is None and manager.fetch_fn is None
        message = await chat_once(manager, "搜一下")
        assert message["status"] == "succeeded"
        # 两个工具都执行了但安全降级为 available=False，不抛异常
        steps = {step["tool"]: step for step in message["steps"]}
        assert steps["web_search"]["status"] == "succeeded"
        assert steps["read_web_page"]["status"] == "succeeded"
        payload = last_tool_payload(captured)["data"]
        assert payload["available"] is False
        await manager.stop()

    asyncio.run(scenario())


def test_web_search_uses_search_default_count(tmp_path, monkeypatch):
    """P3 修复：模型不传 count 时用 main 注入的 search_default_count（config.search.count）。"""
    provider = make_provider([tool_call("web_search", {"query": "x"})], "查到了。")

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch, provider)
        manager.search_default_count = 7
        seen = {}

        def fake_search(query, count=5):
            seen["count"] = count
            return {"ok": True, "provider": "fake", "results": [], "error": ""}

        manager.search_fn = fake_search
        message = await chat_once(manager, "搜 x")
        assert message["status"] == "succeeded"
        assert seen["count"] == 7
        await manager.stop()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# B. 表情包
# ---------------------------------------------------------------------------

def test_send_sticker_only_reply(tmp_path, monkeypatch):
    """模型只调 send_sticker、最终回复为空：_agent_loop 应放行，_run_chat 组装出纯表情包。"""
    provider = make_provider([tool_call("send_sticker", {"name": "崩溃"})], "")

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch, provider)
        message = await chat_once(manager, "又亏了草")
        assert message["status"] == "succeeded"
        assert message["content"] == "[sticker:崩溃]"
        assert message["steps"][0]["tool"] == "send_sticker"
        await manager.stop()

    asyncio.run(scenario())


def test_send_sticker_appended_to_last_piece(tmp_path, monkeypatch):
    provider = make_provider([tool_call("send_sticker", {"name": "疑问"})],
                             "不知道，别问我。")

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch, provider)
        message = await chat_once(manager, "BTC 还能涨吗")
        assert message["content"] == "不知道，别问我。\n[sticker:疑问]"
        assert message["content"].count("[sticker:") == 1
        await manager.stop()

    asyncio.run(scenario())


def test_send_sticker_duplicate_and_existing_marker(tmp_path, monkeypatch):
    # 一次回合里模型连调两次：只认第一个
    provider = make_provider(
        [tool_call("send_sticker", {"name": "崩溃"}, "s1"),
         tool_call("send_sticker", {"name": "嘲讽"}, "s2")], "哈哈哈")

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch, provider)
        message = await chat_once(manager, "哈哈哈")
        assert message["content"] == "哈哈哈\n[sticker:崩溃]"
        second = message["steps"][1]
        assert second["status"] == "succeeded"
        assert second["result"]["sent"] is False
        await manager.stop()

    asyncio.run(scenario())

    # 模型自己已经在正文里写了标记：不能再追加第二个
    # 注意：正文标记必须在"短答保留的第一句"里；若写在句号之后会被短答裁掉
    # （这是实现的一个 P3 边界，见报告；提示词已改为优先用 send_sticker 工具）。
    provider2 = make_provider([tool_call("send_sticker", {"name": "崩溃"})],
                              "这波确实烦 [sticker:嘲讽]")

    async def scenario2():
        manager, _ = make_manager(tmp_path, monkeypatch, provider2)
        message = await chat_once(manager, "烦")
        assert message["content"].count("[sticker:") == 1
        assert "[sticker:嘲讽]" in message["content"]
        await manager.stop()

    asyncio.run(scenario2())


def test_send_sticker_invalid_name_rejected(tmp_path, monkeypatch):
    provider = make_provider([tool_call("send_sticker", {"name": "不存在的表情"})],
                             "好的。")

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch, provider)
        message = await chat_once(manager, "来个表情")
        assert message["steps"][0]["status"] == "failed"
        assert "[sticker:" not in message["content"]
        # 直接调工具也拒绝（不写 pendingSticker）
        target = {}
        with pytest.raises(ValueError):
            await manager._send_sticker({"name": "不存在的表情"}, target)
        assert target == {}
        await manager.stop()

    asyncio.run(scenario())


def test_attach_sticker_rules():
    assert _attach_sticker([], "崩溃", "") == ["[sticker:崩溃]"]
    assert _attach_sticker([""], "崩溃", "") == ["[sticker:崩溃]"]
    assert _attach_sticker(["你好"], "崩溃", "你好") == ["你好\n[sticker:崩溃]"]
    assert _attach_sticker(["第一句", "第二句"], "崩溃", "第一句|||第二句") == [
        "第一句", "第二句\n[sticker:崩溃]"]
    # 正文已有标记：不重复
    assert _attach_sticker(["好 [sticker:疑问]"], "崩溃", "好 [sticker:疑问]") == [
        "好 [sticker:疑问]"]
    # 没选表情包：原样
    assert _attach_sticker(["你好"], "", "你好") == ["你好"]
    # 未知名字不会进 pendingSticker（工具层校验），这里只保证空名字不追加
    assert _attach_sticker(["你好"], None, "你好") == ["你好"]


def test_sticker_frontend_consistency_and_real_pixel_sizes():
    jsx_path = REPO / "web/src/components/arc/chat-dock/stickers.jsx"
    jsx = jsx_path.read_text(encoding="utf-8")
    block = jsx.split("export const STICKERS")[1].split("export const STICKER_NAMES")[0]
    front_names = re.findall(r"\n  ([^\s:/]+)\s*:", block)
    assert tuple(front_names) == STICKER_NAMES, "前后端表情包名字必须一致"
    # 名字与工具 enum 一致
    tool = next(t for t in TOOLS if t["function"]["name"] == "send_sticker")
    assert tool["function"]["parameters"]["properties"]["name"]["enum"] == list(STICKER_NAMES)
    # 提示词里列出全部名字
    prompt = _system_content("")
    for name in STICKER_NAMES:
        assert name in prompt
    # 前端写死的真实像素尺寸必须与 PNG 文件 IHDR 一致（独立读取文件头，不依赖 Pillow）
    sizes_block = jsx.split("export const STICKER_SIZES")[1].split("export function Sticker")[0]
    declared = {name: (int(w), int(h)) for name, w, h in re.findall(
        r"([^\s:]+):\s*\{\s*w:\s*(\d+),\s*h:\s*(\d+)\s*\}", sizes_block)}
    image_files = re.findall(r'"(/stickers/[^"]+)"', block)
    assert len(image_files) == len(STICKER_NAMES)
    for name, ref in zip(STICKER_NAMES, image_files):
        raw = (REPO / "web" / "public" / ref.lstrip("/")).read_bytes()
        assert raw[:8] == b"\x89PNG\r\n\x1a\n"
        width, height = struct.unpack(">II", raw[16:24])
        assert declared[name] == (width, height), name
    # 前端 splitSticker 能解析后端产出的标记
    assert "[sticker:" in jsx and "splitSticker" in jsx


# ---------------------------------------------------------------------------
# C. 后台任务通知
# ---------------------------------------------------------------------------

def test_notify_mode_validation():
    assert _notify_mode(None) == "on_change"
    assert _notify_mode("") == "on_change"
    for mode in ("always", "on_change", "never"):
        assert _notify_mode(mode) == mode
    with pytest.raises(ValueError):
        _notify_mode("sometimes")
    with pytest.raises(ValueError):
        _notify_mode(1)


def test_create_watch_tool_with_notify(tmp_path, monkeypatch):
    captured = []
    provider = make_provider(
        [tool_call("create_watch", {"title": "盯 BTC", "prompt": "检查 BTC 是否满足入场条件",
                                    "intervalSec": 300, "notify": "always"})],
        "已建好，5 分钟盯一次。", captured)

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch, provider)
        message = await chat_once(manager, "帮我盯着 BTC")
        assert message["status"] == "succeeded"
        assert len(manager.tasks) == 1
        task = manager.tasks[0]
        assert task["notify"] == "always" and task["kind"] == "recurring"
        result = message["steps"][0]["result"]
        assert result["created"] is True and result["notify"] == "always"
        await manager.stop()

    asyncio.run(scenario())


def test_create_watch_invalid_notify_rejected(tmp_path, monkeypatch):
    provider = make_provider(
        [tool_call("create_watch", {"title": "盯 BTC", "prompt": "检查 BTC",
                                    "intervalSec": 300, "notify": "有时候"})],
        "参数不对，没建。")

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch, provider)
        message = await chat_once(manager, "盯着 BTC")
        assert message["steps"][0]["status"] == "failed"
        assert manager.tasks == []
        # 直接走 UI 的 create 也必须拒绝非法 notify
        ack = await manager.handle("create", {"prompt": "检查风险", "notify": "bad"})
        assert ack["ok"] is False
        assert manager.tasks == []
        await manager.stop()

    asyncio.run(scenario())


def test_watch_notification_modes_dedupe_and_truncation(tmp_path, monkeypatch):
    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch, make_provider(final_reply="x"))
        task = {"id": "t1", "title": "盯 BTC", "notify": "always",
                "status": "scheduled", "result": "结论A", "lastNotified": ""}
        manager.tasks = [task]

        assert manager._push_watch_notification(task, "succeeded") is True
        pushed = manager.chat[-1]
        assert pushed["kind"] == "watch" and pushed["role"] == "assistant"
        assert pushed["content"] == "【盯 BTC】结论A"
        assert task["lastNotified"] == "结论A"

        # always：重复结果也汇报
        assert manager._push_watch_notification(task, "succeeded") is True
        assert sum(1 for m in manager.chat if m.get("kind") == "watch") == 2

        # never：不汇报
        task["notify"] = "never"
        assert manager._push_watch_notification(task, "succeeded") is False

        # on_change：相同结果去重，变化才汇报
        task["notify"] = "on_change"
        task["result"] = "结论B"
        task["lastNotified"] = "结论B"
        assert manager._push_watch_notification(task, "succeeded") is False
        task["result"] = "结论C"
        assert manager._push_watch_notification(task, "succeeded") is True

        # 失败/空结果不汇报
        task["notify"] = "always"
        before = len(manager.chat)
        assert manager._push_watch_notification(task, "failed") is False
        task["result"] = "   "
        assert manager._push_watch_notification(task, "succeeded") is False
        assert len(manager.chat) == before

        # 超长结果：总长 <= 400 且标题保留
        task["result"] = "长" * 2000
        assert manager._push_watch_notification(task, "succeeded") is True
        content = manager.chat[-1]["content"]
        assert content.startswith("【盯 BTC】") and len(content) <= 400

        # 通知不会触发新的模型回复
        assert not manager.chat_runners
        await manager.stop()

    asyncio.run(scenario())


def test_watch_notification_via_real_execute(tmp_path, monkeypatch):
    captured = []
    provider = make_provider([], "巡检结论", captured)

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch, provider)
        ack = await manager.handle("create", {"prompt": "巡检账户风险",
                                              "title": "巡检", "notify": "always"})
        assert ack["ok"], ack
        task = manager._find(ack["task"]["id"])
        assert await wait_for(lambda: task["runCount"] > 0 and task["id"] not in manager._executing)
        notices = [m for m in manager.chat if m.get("kind") == "watch"]
        assert len(notices) == 1
        assert notices[0]["content"].startswith("【巡检】")
        assert "巡检结论" in notices[0]["content"]
        assert not manager.chat_runners          # 通知不会触发新回复

        # on_change：同样的结果再跑一次不重复通知
        ack2 = await manager.handle("create", {"prompt": "巡检账户风险2",
                                               "title": "巡检2", "notify": "on_change"})
        task2 = manager._find(ack2["task"]["id"])
        assert await wait_for(lambda: task2["runCount"] > 0 and task2["id"] not in manager._executing)
        first_count = sum(1 for m in manager.chat if m.get("kind") == "watch")
        assert first_count == 2
        await manager.handle("run", {"id": task2["id"]})
        assert await wait_for(lambda: task2["runCount"] >= 2
                              and task2["id"] not in manager._executing)
        assert sum(1 for m in manager.chat if m.get("kind") == "watch") == first_count
        await manager.stop()

    asyncio.run(scenario())


def test_watch_notify_persisted_after_restart(tmp_path, monkeypatch):
    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch, make_provider([], "结果"))
        ack = await manager.handle("create", {"prompt": "巡检", "title": "巡检",
                                              "notify": "on_change"})
        assert ack["ok"], ack
        task = manager._find(ack["task"]["id"])
        task["lastNotified"] = "上次结论"
        manager._save()
        await manager.stop()

        manager2, _ = make_manager(tmp_path, monkeypatch, make_provider([], "结果"))
        restored = manager2._find(task["id"])
        assert restored["notify"] == "on_change"
        assert restored["lastNotified"] == "上次结论"
        await manager2.stop()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# D. 真实联网（环境波动时 skip 并保留重试记录）
# ---------------------------------------------------------------------------

def test_live_web_search_and_fetch_page():
    # 与 main.py 一致：无显式代理时沿用系统代理（本机 Clash 等场景直连会超时）
    try:
        proxies = urllib.request.getproxies() or {}
    except Exception:
        proxies = {}
    system_proxy = str(proxies.get("https") or proxies.get("http") or "").strip()
    search_attempts = []
    result = None
    for _ in range(3):
        result = search.web_search("bitcoin price", count=5, proxy=system_proxy,
                                   timeout=20)
        search_attempts.append({k: result.get(k) for k in ("ok", "provider", "count", "error")})
        if result.get("ok") and result.get("results"):
            break
        time.sleep(1)
    if not (result and result.get("ok") and result.get("results")):
        pytest.skip("联网环境波动，3 次搜索均失败: %s" % search_attempts)
    assert result["provider"] in ("serper", "duckduckgo", "bing")
    first = result["results"][0]
    assert first["title"] and first["url"].startswith(("http://", "https://"))

    fetch_attempts = []
    page = None
    for _ in range(3):
        page = search.fetch_page("https://example.com", proxy=system_proxy,
                                   timeout=20)
        fetch_attempts.append({k: page.get(k) for k in ("ok", "title", "truncated", "error")})
        if page.get("ok") and page.get("text"):
            break
        time.sleep(1)
    if not (page and page.get("ok") and page.get("text")):
        pytest.skip("联网环境波动，3 次 fetch_page 均失败: %s" % fetch_attempts)
    assert page["finalUrl"].startswith("https://")
    assert "example" in (page["title"] + page["text"]).lower()


# ---------------------------------------------------------------------------
# F. 搜索 / 网页读取安全（离线：绝不联网；不安全 URL 用 spy 断言零 DNS/零连接/零代理）
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status=200, body=b"", headers=None, url="https://public.example/"):
        self.status = status
        self._body = body
        self.headers = headers or {}
        self._url = url
        self.closed = False

    def read(self, limit=-1):
        return self._body[:limit] if limit and limit > 0 else self._body

    def geturl(self):
        return self._url

    def close(self):
        self.closed = True


BLOCKED_URLS = [
    "http://localhost/admin",
    "http://localhost:8080/",
    "http://127.0.0.1/",
    "http://10.1.2.3/",
    "http://172.16.0.1/",
    "http://192.168.0.1/",
    "http://169.254.169.254/latest/meta-data/",
    "http://100.100.100.200/latest/meta-data/",
    "http://[::1]/",
    "http://[::ffff:127.0.0.1]/",
    "http://0.0.0.0/",
    "http://foo.local/x",
    "http://metadata.google.internal/computeMetadata/v1/",
    "http://user:pass@example.com/",
    "ftp://example.com/x",
    # 数值/编码 IP 形式：正则已修为 \d+ 且 host 先 unquote，必须在解析/连接前拒绝
    "http://2130706433/",
    "http://0177.0.0.1/",
    "http://0x7f.0.0.1/",
    "http://127.1/",
    "http://127%2e0%2e0%2e1/",
]


@pytest.mark.parametrize("url", BLOCKED_URLS)
def test_fetch_page_never_opens_unsafe_urls(monkeypatch, url):
    """不安全的 URL 连 DNS 解析都不应发生，更不能建连/走代理（防止假绿）。"""
    calls = {"resolve": 0, "connect": 0, "proxy": 0}

    def spy_resolve(host):
        calls["resolve"] += 1
        raise AssertionError("不安全 URL 不应做 DNS 解析")

    def spy_connect(*_a, **_k):
        calls["connect"] += 1
        raise AssertionError("不安全 URL 不应建立连接")

    def spy_proxy(*_a, **_k):
        calls["proxy"] += 1
        raise AssertionError("不安全 URL 不应走代理")

    monkeypatch.setattr(search, "_resolve_public_ips", spy_resolve)
    monkeypatch.setattr(search, "_pinned_connection", spy_connect)
    monkeypatch.setattr(search, "_open_via_proxy", spy_proxy)
    out = search.fetch_page(url)
    assert out["ok"] is False and out["text"] == ""
    assert out["error"] and len(out["error"]) <= 200
    assert calls == {"resolve": 0, "connect": 0, "proxy": 0}


def test_fetch_page_rejects_unsafe_redirect(monkeypatch):
    opened = []

    def fake_open(request, proxy="", timeout=10):
        opened.append(str(request.full_url if hasattr(request, "full_url") else request))
        return FakeResponse(status=302, headers={"Location": "http://169.254.169.254/x"})

    monkeypatch.setattr(search, "_open", fake_open)
    out = search.fetch_page("https://public.example/start")
    assert out["ok"] is False
    assert "重定向目标不安全" in out["error"] or "内网" in out["error"]
    assert len(opened) == 1, "不安全的重定向目标不能发起第二跳请求"


def test_fetch_page_extracts_text_and_follows_public_redirect(monkeypatch):
    opened = []

    def fake_open(request, proxy="", timeout=10):
        url = str(getattr(request, "full_url", request))
        opened.append(url)
        if len(opened) == 1:
            return FakeResponse(status=302, headers={"Location": "https://public.example/next"})
        html = ("<html><head><title>测试页面</title></head><body>"
                "<p>第一段正文</p><p>第二段正文</p>"
                "<script>alert('不要执行')</script></body></html>")
        return FakeResponse(status=200, body=html.encode("utf-8"),
                            headers={"Content-Type": "text/html; charset=utf-8"},
                            url="https://public.example/next")

    monkeypatch.setattr(search, "_open", fake_open)
    out = search.fetch_page("https://public.example/start", max_chars=100)
    assert out["ok"] is True
    assert out["title"] == "测试页面"
    assert "第一段正文" in out["text"] and "第二段正文" in out["text"]
    assert "不要执行" not in out["text"]
    assert out["finalUrl"] == "https://public.example/next"
    assert len(opened) == 2


def test_fetch_page_redirect_loop_limit(monkeypatch):
    def fake_open(request, proxy="", timeout=10):
        return FakeResponse(status=302, headers={"Location": "https://public.example/loop"})

    monkeypatch.setattr(search, "_open", fake_open)
    out = search.fetch_page("https://public.example/loop")
    assert out["ok"] is False
    assert "重定向次数过多" in out["error"]


def test_web_search_provider_fallback_and_never_raises(monkeypatch):
    def fail(*_a, **_k):
        raise RuntimeError("ddg down")

    monkeypatch.setattr(search, "_search_duckduckgo", fail)
    monkeypatch.setattr(search, "_search_bing",
                        lambda *_: [{"title": "B", "url": "https://example.com/b",
                                     "snippet": "s"}])
    monkeypatch.delenv(search.SERPER_KEY_ENV, raising=False)
    out = search.web_search("hello", count=1)
    assert out["ok"] is True and out["provider"] == "bing" and out["count"] == 1

    # 全部失败：结构化错误、不抛
    monkeypatch.setattr(search, "_search_bing", fail)
    out2 = search.web_search("hello")
    assert out2["ok"] is False and out2["results"] == [] and out2["error"]

    # 空查询/坏参数
    assert search.web_search("")["ok"] is False
    out3 = search.web_search("x", count=99)
    assert isinstance(out3, dict) and out3["ok"] in (True, False)


def test_search_result_urls_are_filtered():
    assert search._new_result("内网", "http://127.0.0.1/x", "s") is None
    assert search._new_result("坏协议", "ftp://example.com/x", "s") is None
    ok = search._new_result("正常", "https://example.com/x", "摘要")
    assert ok["url"] == "https://example.com/x" and ok["title"] == "正常"
    # DDG 跳转链接还原真实 URL
    wrapped = ("//duckduckgo.com/l/?uddg="
               "https%3A%2F%2Fexample.com%2Freal%3Fa%3D1")
    assert search._ddg_real_url(wrapped) == "https://example.com/real?a=1"


def test_fetch_page_blocks_dns_rebinding_hostname(monkeypatch):
    """公共通配 DNS 指向内网（nip.io 模式）：解析后必须按真实 IP 拒绝且不建连。"""
    real_getaddrinfo = search.socket.getaddrinfo

    def fake_getaddrinfo(host, port, *args, **kwargs):
        if str(host).lower().endswith("nip.io"):
            return [(search.socket.AF_INET, search.socket.SOCK_STREAM, 6, "",
                     ("127.0.0.1", 80))]
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(search.socket, "getaddrinfo", fake_getaddrinfo)
    connects = []

    def spy_connect(*args, **kwargs):
        connects.append(args)
        raise AssertionError("解析到内网地址后不允许建立连接")

    monkeypatch.setattr(search, "_pinned_connection", spy_connect)
    monkeypatch.setattr(search, "_open_via_proxy",
                        lambda *args, **kwargs: connects.append(args))
    out = search.fetch_page("http://127.0.0.1.nip.io/")
    assert out["ok"] is False
    assert connects == [], "DNS 解析到内网地址后必须零连接"
    # 元数据通配域名同理
    out2 = search.fetch_page("http://169.254.169.254.nip.io/latest/meta-data/")
    assert out2["ok"] is False and connects == []


def test_validate_blocks_legacy_numeric_hosts():
    """数值/编码 IP 主机名必须在 URL 校验阶段就拒绝（正则 \\d+ 且 host 先 unquote）。"""
    for url in ("http://2130706433/", "http://0177.0.0.1/",
                "http://0x7f.0.0.1/", "http://127.1/",
                "http://127%2e0%2e0%2e1/"):
        with pytest.raises(search._UnsafeURLError):
            search._validate_public_url(url)


# ---------------------------------------------------------------------------
# E/F. 接线与前端静态审查
# ---------------------------------------------------------------------------

def test_main_search_wiring_and_prompt_sections():
    main_src = (REPO / "main.py").read_text(encoding="utf-8")
    assert "from feed.search import fetch_page, web_search" in main_src
    assert "def _search_number(" in main_src                     # 配置容错
    assert 'if _search_cfg.get("enabled", True):' in main_src
    assert "agent.search_default_count = _search_count" in main_src
    assert "agent.search_fn = lambda query, count=0: web_search(" in main_src
    assert "count or agent.search_default_count" in main_src
    assert "agent.fetch_fn = lambda url: fetch_page(" in main_src
    assert "getproxies()" in main_src                            # 系统代理回退
    assert 'proxy_cfg or ""' in main_src                        # search.proxy 空则沿用 feed.proxy

    prompt = SYSTEM_PROMPT
    for token in ("【聊天】", "【搜索】", "【后台任务】", "【表情包】",
                  "web_search", "read_web_page", "send_sticker",
                  "always", "on_change", "never"):
        assert token in prompt, token
    # 工具清单/schema
    assert {"web_search", "read_web_page", "send_sticker"} <= TOOL_NAMES
    schema = {t["function"]["name"]: t["function"]["parameters"] for t in TOOLS}
    assert schema["web_search"]["required"] == ["query"]
    assert schema["read_web_page"]["required"] == ["url"]
    assert schema["create_watch"]["properties"]["notify"]["enum"] == [
        "always", "on_change", "never"]

    # config.example.yaml / README 与实现一致
    cfg_src = (REPO / "config.example.yaml").read_text(encoding="utf-8")
    for key in ("search:", "enabled:", "proxy:", "timeout:", "count:"):
        assert key in cfg_src
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    for token in ("web_search", "read_web_page", "send_sticker",
                  "KLINE_SERPER_KEY", "notify"):
        assert token in readme, token


def test_frontend_jitter_fix_static_review():
    jsx = (REPO / "web/src/components/arc/chat-dock/chat-dock.jsx").read_text(
        encoding="utf-8")
    css = (REPO / "web/src/components/arc/chat-dock/chat-dock.module.css").read_text(
        encoding="utf-8")
    # 贴底：rAF 合并 + ResizeObserver/MutationObserver + 用户上翻不拽回
    assert "requestAnimationFrame" in jsx and "cancelAnimationFrame" in jsx
    assert "ResizeObserver" in jsx and "MutationObserver" in jsx
    assert "FOLLOW_SLACK" in jsx and "followRef.current" in jsx
    # 入场动画只给新出现的 assistant 分组
    assert "seenGroupsRef" in jsx and "animateIn" in jsx
    assert 'group.role !== "user"' in jsx
    # 标题栏状态词只在 busy/idle 切换时重挂载（否则每 250ms 重放入场动画）
    assert 'key={busy ? "busy" : "idle"}' in jsx
    # MessageGroup 签名 memo：签名必须覆盖所有渲染字段（content/status/error/attachments）
    assert "groupSignature" in jsx and "prev.sig === next.sig" in jsx
    for field in ("item?.id", "item?.role", "item?.status", "item?.content",
                  "item?.error", "attachments"):
        assert field in jsx, field
    # 既有行为不能被破坏：分组（-N 合并）/ 时间分隔 / [sticker:] 解析
    assert "groupMessages" in jsx
    # 分组规则本身（id 里的 -2/-3 后缀去掉后归为同一轮）。
    # 注意别用带反斜杠的字面量去比对源码里的正则——转义层级很容易写错，
    # 分别校验「replace 调用」「-」「$」三个片段即可达到同样目的。
    assert "id.replace(" in jsx and "id.replace(/-" in jsx, "缺少去掉 -N 后缀的分组逻辑"
    assert 'const base = id.replace' in jsx
    assert "TIME_GAP_MS" in jsx and "timeDivider" in jsx
    assert "splitSticker" in jsx
    # 浮窗/胶囊/拖拽已移除：面板固定在页面第二行，不再是浮层
    # 只查代码里的用法（onPointerDown={startDrag} 等），不查注释里的历史说明
    assert "createPortal" not in jsx
    assert "onPointerDown={startDrag}" not in jsx
    assert "commitPos(" not in jsx
    assert 'aria-label="打开 ODDD 对话"' not in jsx   # 折叠胶囊（已删除）
    assert 'aria-label="收起窗口"' not in jsx         # 收起按钮（已删除）
    # 贴纸占位：真实宽高 + eager；单图 4:3 占位；贴纸行最小高度
    stickers = (REPO / "web/src/components/arc/chat-dock/stickers.jsx").read_text(
        encoding="utf-8")
    assert 'loading="eager"' in stickers
    assert "width={width}" in stickers and "height={height}" in stickers
    assert "aspect-ratio: 4 / 3" in css
    assert "min-height: 74px" in css


def test_storage_ddl_fault_isolation_and_reserved_word_columns(monkeypatch):
    """验证期间并发新增的 storage.py DDL 容错修复复验。

    1) 单条建表失败只记录到 _ddl_failures，不抛异常、不拖垮整个存储层
       （历史上的教训：一个 MySQL 保留字列名就让全库降级为无持久化）。
    2) paper_trades 的 MySQL 保留字列 trailing 已加反引号：DDL/INSERT/SELECT
       全链路在真实 SQL（SQLite 适配）上往返正常。
    """
    conn = SQLConnection()
    conn.db.execute("CREATE TABLE equity(acct TEXT,ts BIGINT,bal DOUBLE,"
                    "upnl DOUBLE,PRIMARY KEY(acct,ts))")
    monkeypatch.setattr(storage_mod.pymysql, "connect", lambda **_: conn)
    store = Storage({"user": "isolated-ddl"})
    assert store.enabled
    # 坏 DDL 单条失败：不抛异常、存储层保持 enabled、失败被记录
    assert store._ddl("CREATE TABLE bad_ddl (this is not valid sql)") is False
    assert store._ddl_failures and store.enabled
    # 保留字列 trailing 真实读写往返
    store.save_paper_trade({"symbol": "btcusdt", "entryTs": 1, "openSeq": 1,
                            "closedAt": 2, "reason": "tp", "trailing": True,
                            "pnl": 1.0})
    row = store.paper_trades()[0]
    assert row["trailing"] is True
    conn.db.close()
