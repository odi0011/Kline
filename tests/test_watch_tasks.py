"""后台常驻任务：模型必须能真的创建/列出/停止监控。

曾经的缺陷：create_task 框架完整（定时、循环、暂停、取消都有），
但模型没有任何工具能碰到它，用户说"帮我盯着 BTC"只能回"做不到"。
"""
import asyncio
import copy

from tests.test_agent import make_manager, tool_call, tool_result, wait_for


class WatchHTTP:
    """每次调用返回一句固定回复，用于验证任务能连续跑。"""

    def __init__(self, reply="盯着的结论：暂不入场"):
        self.reply = reply
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append(copy.deepcopy(args[2] if len(args) >= 3 else kwargs.get("messages")))
        return {"ok": True, "reply": self.reply, "usage": {"total_tokens": 5},
                "model": "test-model"}


def test_create_watch_creates_a_real_recurring_task(tmp_path, monkeypatch):
    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        out = await manager._create_watch({
            "title": "盯 BTC 入场",
            "prompt": "检查 BTC 是否满足入场条件",
            "intervalSec": 300,
        })
        assert out["created"] is True
        assert out["intervalSec"] == 300
        assert len(manager.tasks) == 1
        task = manager.tasks[0]
        assert task["kind"] == "recurring"
        assert task["intervalSec"] == 300
        assert task["title"] == "盯 BTC 入场"
        await manager.stop()
    asyncio.run(scenario())


def test_interval_below_one_minute_is_rejected(tmp_path, monkeypatch):
    """高频轮询既烧钱又没意义，必须拦住。"""
    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        for bad in (0, 30, 59, -60, "300", 60.5, True):
            try:
                await manager._create_watch({"title": "x", "prompt": "y", "intervalSec": bad})
            except ValueError as exc:
                assert "intervalSec" in str(exc) or "整数" in str(exc)
            else:
                raise AssertionError(f"intervalSec={bad!r} 应被拒绝")
        assert manager.tasks == []
        await manager.stop()
    asyncio.run(scenario())


def test_list_watches_reports_status(tmp_path, monkeypatch):
    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        await manager._create_watch({"title": "盯 A", "prompt": "查 A", "intervalSec": 120})
        out = manager._list_watches({})
        assert out["count"] == 1
        item = out["tasks"][0]
        assert item["title"] == "盯 A"
        assert item["intervalSec"] == 120
        assert "status" in item
        await manager.stop()
    asyncio.run(scenario())


def test_stop_watch_removes_it(tmp_path, monkeypatch):
    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        created = await manager._create_watch({"title": "临时", "prompt": "查", "intervalSec": 60})
        task_id = created["id"]
        out = await manager._stop_watch({"id": task_id})
        assert out["stopped"] is True
        assert manager.tasks == []
        # 再停一次应报错并提示先列出
        try:
            await manager._stop_watch({"id": task_id})
        except ValueError as exc:
            assert "list_watches" in str(exc)
        else:
            raise AssertionError("重复停止应报错")
        await manager.stop()
    asyncio.run(scenario())


def test_tools_are_exposed_to_the_model():
    from ai.agent_tools import TOOL_NAMES
    for name in ("create_watch", "list_watches", "stop_watch"):
        assert name in TOOL_NAMES


def test_prompt_tells_the_model_it_can_watch(tmp_path, monkeypatch):
    """提示词必须明确说"你能建后台任务"，否则模型会答做不到。"""
    from ai.agent import SYSTEM_PROMPT
    assert "create_watch" in SYSTEM_PROMPT
    assert "后台常驻" in SYSTEM_PROMPT
    assert "做不到" in SYSTEM_PROMPT      # 明确禁止这个回答


def test_duplicate_create_is_idempotent(tmp_path, monkeypatch):
    """同名同间隔重复创建时只保留一个。

    模型在工具轮里可能被重试（provider 降级），没有幂等闸就会
    给用户堆出一串重复的监控任务。
    """
    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        a = await manager._create_watch({"title": "盯 BTC", "prompt": "查",
                                         "intervalSec": 300})
        b = await manager._create_watch({"title": "盯 BTC", "prompt": "查",
                                         "intervalSec": 300})
        assert a["created"] is True
        assert b["created"] is False and b["existing"] is True
        assert len(manager.tasks) == 1
        await manager.stop()
    asyncio.run(scenario())


def test_model_can_create_watch_end_to_end(tmp_path, monkeypatch):
    """端到端：模型调用 create_watch，任务真的被建出来。"""
    seen = {"required": 0}

    def provider(*args, **kwargs):
        if kwargs.get("tool_choice") == "required":
            seen["required"] += 1
            # 只在第一轮要求工具；后续轮直接给结论，
            # 否则每次 required 都会再建一个任务（真 bug 就是这样暴露的）
            if seen["required"] == 1:
                return tool_result(tool_call("create_watch", {
                    "title": "盯 BTC",
                    "prompt": "检查 BTC 入场条件",
                    "intervalSec": 300,
                }, "w1"))
        return {"ok": True, "reply": "好，已盯着 BTC，每 5 分钟看一次。",
                "usage": {"total_tokens": 10}, "model": "test-model"}

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch, provider=provider)
        await manager.ask("帮我设一个定时任务，盯着BTC，可以入场的时候直接告诉我", "test-model")
        assert await wait_for(lambda: not manager.chat_runners)
        assert len(manager.tasks) == 1, f"任务被创建了 {len(manager.tasks)} 次"
        assert manager.tasks[0]["intervalSec"] == 300
        await manager.stop()
    asyncio.run(scenario())


def test_task_actually_runs_and_records_result(tmp_path, monkeypatch):
    """建完之后任务要真的跑一轮并留下结果，不是只存个壳。"""
    http = WatchHTTP("结论：现在是空头")

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch, provider=http)
        manager.interval_floor = 60
        await manager._create_watch({"title": "跑一次", "prompt": "查一下 BTC",
                                     "intervalSec": 60})
        # 把下次执行时间拉到当前，触发立刻跑
        manager.tasks[0]["nextRunAt"] = 0
        manager.tasks[0]["startAt"] = None
        manager.tasks[0]["status"] = "scheduled"
        runner = manager._start_runner(manager.tasks[0]["id"])
        assert await wait_for(lambda: manager.tasks and manager.tasks[0]["runCount"] >= 1, rounds=600)
        assert "空头" in (manager.tasks[0]["result"] or "")
        await manager.stop()
    asyncio.run(scenario())


def test_watch_survives_page_close(tmp_path, monkeypatch):
    """任务由服务端跑，网页关掉不影响——这里验证状态能持久化。"""
    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        await manager._create_watch({"title": "持久", "prompt": "查", "intervalSec": 300})
        await manager.stop()
        resumed, _ = make_manager(tmp_path, monkeypatch)
        assert len(resumed.tasks) == 1
        assert resumed.tasks[0]["title"] == "持久"
        await resumed.stop()
    asyncio.run(scenario())
