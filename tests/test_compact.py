"""清空（全清 + 二次确认）与手动压缩。"""
import asyncio

from tests.test_agent import completion, make_manager, wait_for


def _seed(manager, n=12):
    """塞入 n 轮对话，用于验证压缩。"""
    for i in range(n):
        manager.chat.append({"id": f"u{i}", "role": "user", "content": f"问题{i}", "ts": i})
        manager.chat.append({"id": f"a{i}", "role": "assistant", "content": f"回答{i}",
                             "status": "succeeded", "ts": i, "model": "test-model"})


def test_clear_wipes_everything(tmp_path, monkeypatch):
    """清空 = 对话 + 摘要 + 记忆 + 后台任务，一个都不留。"""
    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        _seed(manager)
        manager.chat_summary = "旧摘要"
        manager.memory = {"language": "zh-CN"}
        await manager._create_watch({"title": "盯 BTC", "prompt": "查", "intervalSec": 300})

        result = await manager.handle("clear_chat", {})
        assert result["ok"]
        assert manager.chat == []
        assert manager.chat_summary == ""
        assert manager.memory == {}
        assert manager.tasks == []
        await manager.stop()
    asyncio.run(scenario())


def test_clear_persists(tmp_path, monkeypatch):
    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        _seed(manager)
        manager.memory = {"language": "en"}
        await manager.handle("clear_chat", {})
        await manager.stop()
        resumed, _ = make_manager(tmp_path, monkeypatch)
        assert resumed.chat == [] and resumed.memory == {} and resumed.tasks == []
        await resumed.stop()
    asyncio.run(scenario())


def test_compact_keeps_recent_and_summarizes_older(tmp_path, monkeypatch):
    seen = []

    def provider(*args, **kwargs):
        messages = args[2] if len(args) >= 3 else kwargs.get("messages")
        seen.append(messages)
        return {"ok": True, "reply": "摘要：用户在问 BTC，结论偏空。",
                "usage": {"total_tokens": 10}, "model": "test-model"}

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch, provider=provider)
        _seed(manager, 12)          # 24 条
        result = await manager.handle("compact_chat", {})
        assert result["ok"] and result.get("compacted") is True
        assert len(manager.chat) <= 6, f"压缩后应只留几条，实际 {len(manager.chat)}"
        assert "摘要" in manager.chat_summary
        await manager.stop()
    asyncio.run(scenario())


def test_compact_skips_short_history(tmp_path, monkeypatch):
    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        _seed(manager, 1)           # 2 条，太短
        result = await manager.handle("compact_chat", {})
        assert result["ok"] and result.get("skipped") is True
        assert len(manager.chat) == 2
        await manager.stop()
    asyncio.run(scenario())


def test_compact_falls_back_when_model_fails(tmp_path, monkeypatch):
    """模型不可用时退回机械压缩，不能让上下文原样留着。"""
    def provider(*args, **kwargs):
        return {"ok": False, "error": "boom"}

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch, provider=provider)
        _seed(manager, 12)
        result = await manager.handle("compact_chat", {})
        assert result["ok"]
        assert len(manager.chat) <= 6, "模型失败也要裁掉历史"
        await manager.stop()
    asyncio.run(scenario())


def test_compact_is_a_known_action(tmp_path, monkeypatch):
    """compact_chat 必须在允许的动作列表里，且不接受参数。"""
    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        # handle() 把 ValueError 转成 {ok: False, error}，不向外抛
        result = await manager.handle("compact_chat", {"oops": 1})
        assert result["ok"] is False
        assert "不支持" in result["error"]
        await manager.stop()
    asyncio.run(scenario())


def test_stats_reads_the_running_message(tmp_path, monkeypatch):
    """状态栏要读**进行中**那条消息的实时 perf，而不是等它结束。

    真实流式下 perf 每 250ms 由 progress 回调写入；这里直接构造
    一条 running + perf 的消息，验证 stats() 会优先选它。
    """
    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        # 一条已完成的旧消息
        manager.chat.append({"id": "old", "role": "assistant", "content": "旧",
                             "status": "succeeded", "ts": 1, "model": "test-model",
                             "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                             "perf": {"ttftMs": 500, "decodeMs": 1000}})
        # 一条正在跑的，带实时 perf（chars/deltas 是流式过程中才有的）
        manager.chat.append({"id": "run", "role": "assistant", "content": "正在输出",
                             "status": "running", "ts": 2, "model": "test-model",
                             "perf": {"ttftMs": 300, "decodeMs": 900, "chars": 160,
                                      "deltas": 40, "streamed": True, "live": True}})
        s = manager.stats()
        assert s["ttftMs"] == 300, "应该读进行中那条，而不是上一条已完成的"
        assert s["live"] is True, "进行中应标记 live"
        assert s["tps"] is not None, "进行中也要给出估算 TPS，状态栏才会跳"
        await manager.stop()
    asyncio.run(scenario())
