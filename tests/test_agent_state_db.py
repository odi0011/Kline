"""Agent 状态双存储：MySQL（主存储） + 本地 JSON（兜底/首次迁移来源）。

storage.Storage 的四个冻结方法用 FakeStateStore 内存实现顶替，本文件只按冻结
签名调用，不依赖 storage.py 的实现（它由另一位 teammate 并行开发）：

  load_agent_state() -> dict|None                # {"tasks", "memory", "chatSummary"}
  save_agent_state(tasks, memory, chat_summary) -> None
  load_agent_chat(limit: int = 40) -> list[dict] # 时间升序
  save_agent_chat(messages: list[dict]) -> int
"""
from __future__ import annotations

import copy
import json
import logging

import pytest

from ai.agent import AgentManager, PersistenceError

CONFIG = {"base": "https://example.invalid/v1", "key": "fake-test-key",
          "models": [{"id": "test-model", "active": True}]}


class FakeStateStore:
    """内存版 storage.Storage：实现四个冻结方法，可注入读写异常并记录写入内容。"""

    def __init__(self, state: dict | None = None, chat: list[dict] | None = None):
        self.state = copy.deepcopy(state)
        self.chat = copy.deepcopy(list(chat or []))
        self.saved_tasks = None
        self.saved_memory = None
        self.saved_summary = None
        self.saved_chat = None
        self.state_save_calls = 0
        self.chat_save_calls = 0
        self.load_error: Exception | None = None
        self.load_chat_error: Exception | None = None
        self.save_error: Exception | None = None
        self.save_chat_error: Exception | None = None

    def load_agent_state(self):
        if self.load_error is not None:
            raise self.load_error
        return copy.deepcopy(self.state)

    def save_agent_state(self, tasks, memory, chat_summary):
        if self.save_error is not None:
            raise self.save_error
        self.state_save_calls += 1
        self.saved_tasks = copy.deepcopy(tasks)
        self.saved_memory = copy.deepcopy(memory)
        self.saved_summary = chat_summary
        self.state = dict(self.state or {})
        self.state.update({"tasks": copy.deepcopy(tasks),
                           "memory": copy.deepcopy(memory),
                           "chatSummary": chat_summary})

    def load_agent_chat(self, limit: int = 40):
        if self.load_chat_error is not None:
            raise self.load_chat_error
        return copy.deepcopy(self.chat[-limit:])

    def save_agent_chat(self, messages):
        if self.save_chat_error is not None:
            raise self.save_chat_error
        self.chat_save_calls += 1
        self.saved_chat = copy.deepcopy(list(messages))
        self.chat = copy.deepcopy(list(messages))
        return len(self.saved_chat)


def make_task(task_id="task-1", title="巡检", status="scheduled"):
    """符合 _apply_state 校验的最小任务。"""
    return {"id": task_id, "title": title, "prompt": "检查风险",
            "model": "", "kind": "once", "intervalSec": 0, "status": status}


def make_chat(count, start=0):
    return [{"id": f"m{i}", "role": "user" if i % 2 == 0 else "assistant",
             "content": f"消息{i}", "ts": i} for i in range(start, start + count)]


def write_json(tmp_path, tasks=None, chat=None, memory=None, summary="",
               filename="agent-state.json"):
    path = tmp_path / filename
    payload = {"version": 2, "tasks": list(tasks or []), "chat": list(chat or []),
               "memory": dict(memory or {}), "chatSummary": summary}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def make_manager(tmp_path, state_store=None, filename="agent-state.json"):
    events = []

    async def broadcast(message):
        events.append(message)

    manager = AgentManager(lambda: copy.deepcopy(CONFIG), lambda: "", lambda: {},
                           broadcast, str(tmp_path / filename), state_store=state_store)
    return manager, events


def test_store_none_keeps_legacy_json_behaviour(tmp_path):
    """不传 state_store：行为与改造前一致（纯 JSON 文件）。"""
    manager, _ = make_manager(tmp_path, state_store=None)
    manager.tasks = [make_task()]
    manager.memory = {"response_style": "concise"}
    manager.chat_summary = "旧摘要"
    manager.chat = make_chat(3)
    manager._save()

    raw = json.loads((tmp_path / "agent-state.json").read_text(encoding="utf-8"))
    assert raw["version"] == 2
    assert [task["id"] for task in raw["tasks"]] == ["task-1"]
    assert raw["memory"] == {"response_style": "concise"}
    assert raw["chatSummary"] == "旧摘要"
    assert [item["id"] for item in raw["chat"]] == ["m0", "m1", "m2"]

    restarted, _ = make_manager(tmp_path, state_store=None)
    assert restarted.persistence_error == "" and not restarted._load_blocked
    assert [task["id"] for task in restarted.tasks] == ["task-1"]
    assert restarted.memory == {"response_style": "concise"}
    assert restarted.chat_summary == "旧摘要"
    assert [item["id"] for item in restarted.chat] == ["m0", "m1", "m2"]


def test_save_pushes_state_and_last_40_chat_into_store(tmp_path):
    store = FakeStateStore()
    manager, _ = make_manager(tmp_path, store)
    # 首次启动时 JSON 兜底路径会做一次迁移写入，这里只看下一次显式保存
    store.state_save_calls = store.chat_save_calls = 0
    manager.tasks = [make_task("task-a"), make_task("task-b", "复盘")]
    manager.memory = {"language": "en"}
    manager.chat_summary = "压缩后的摘要"
    manager.chat = make_chat(45)

    manager._save()

    assert store.state_save_calls == 1 and store.chat_save_calls == 1
    assert [task["id"] for task in store.saved_tasks] == ["task-a", "task-b"]
    assert store.saved_memory == {"language": "en"}
    assert store.saved_summary == "压缩后的摘要"
    assert len(store.saved_chat) == 40
    assert [item["id"] for item in store.saved_chat] == [f"m{i}" for i in range(5, 45)]
    # JSON 兜底也照旧写，只留最近 40 条
    raw = json.loads((tmp_path / "agent-state.json").read_text(encoding="utf-8"))
    assert [item["id"] for item in raw["chat"]] == [f"m{i}" for i in range(5, 45)]


def test_restart_restores_complete_state_from_store(tmp_path):
    store = FakeStateStore()
    manager, _ = make_manager(tmp_path, store)
    manager.tasks = [make_task("task-x", "盯 BTC")]
    manager.memory = {"watch_symbols": "BTCUSDT,ETHUSDT"}
    manager.chat_summary = "要点摘要"
    manager.chat = make_chat(45)
    manager._save()

    restarted, _ = make_manager(tmp_path, store)
    assert restarted.persistence_error == "" and not restarted._load_blocked
    assert not restarted.state()["readOnly"]
    assert [task["id"] for task in restarted.tasks] == ["task-x"]
    assert restarted.tasks[0]["title"] == "盯 BTC"
    assert restarted.memory == {"watch_symbols": "BTCUSDT,ETHUSDT"}
    assert restarted.chat_summary == "要点摘要"
    assert [item["id"] for item in restarted.chat] == [f"m{i}" for i in range(5, 45)]


def test_db_state_wins_over_stale_json(tmp_path):
    write_json(tmp_path, tasks=[make_task("task-json")], memory={"language": "en"},
               summary="JSON 摘要")
    store = FakeStateStore(
        state={"tasks": [make_task("task-db")],
               "memory": {"response_style": "detailed"},
               "chatSummary": "DB 摘要"},
        chat=make_chat(2))
    manager, _ = make_manager(tmp_path, store)

    assert [task["id"] for task in manager.tasks] == ["task-db"]
    assert manager.memory == {"response_style": "detailed"}
    assert manager.chat_summary == "DB 摘要"
    assert [item["id"] for item in manager.chat] == ["m0", "m1"]
    # DB 命中后不再读 JSON：本地文件保持原样
    raw = json.loads((tmp_path / "agent-state.json").read_text(encoding="utf-8"))
    assert [task["id"] for task in raw["tasks"]] == ["task-json"]


def test_empty_db_loads_json_and_migrates_into_db(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="ai.agent")
    write_json(tmp_path, tasks=[make_task("task-json", "迁移任务")],
               memory={"language": "zh-CN"}, summary="JSON 摘要", chat=make_chat(3))
    store = FakeStateStore()  # DB 里还没有状态

    manager, _ = make_manager(tmp_path, store)

    assert [task["id"] for task in manager.tasks] == ["task-json"]
    assert manager.chat_summary == "JSON 摘要"
    # 首次启动把 JSON 状态镜像进 DB
    assert store.state_save_calls == 1 and store.chat_save_calls == 1
    assert [task["id"] for task in store.saved_tasks] == ["task-json"]
    assert store.saved_memory == {"language": "zh-CN"}
    assert store.saved_summary == "JSON 摘要"
    assert [item["id"] for item in store.saved_chat] == ["m0", "m1", "m2"]
    assert "migrated" in caplog.text


def test_db_load_error_falls_back_to_json(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="ai.agent")
    write_json(tmp_path, tasks=[make_task("task-json")], memory={"language": "en"},
               summary="JSON 摘要", chat=make_chat(2))
    store = FakeStateStore()
    store.load_error = RuntimeError("mysql unavailable")

    manager, _ = make_manager(tmp_path, store)

    assert manager.persistence_error == "" and not manager._load_blocked
    assert [task["id"] for task in manager.tasks] == ["task-json"]
    assert [item["id"] for item in manager.chat] == ["m0", "m1"]
    assert "DB load failed" in caplog.text


def test_db_invalid_data_falls_back_to_json_without_lock(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="ai.agent")
    write_json(tmp_path, tasks=[make_task("task-json")], summary="JSON 摘要")
    store = FakeStateStore(state={"tasks": [{"id": 1}], "memory": {}},
                           chat=[{"role": "system"}])

    manager, _ = make_manager(tmp_path, store)

    assert not manager.state()["readOnly"]
    assert [task["id"] for task in manager.tasks] == ["task-json"]
    assert "DB load failed" in caplog.text
    # 坏数据被 JSON 状态覆盖，迁移回路顺手把 DB 修好
    assert [task["id"] for task in store.state["tasks"]] == ["task-json"]


def test_db_save_error_keeps_json_success_quiet(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="ai.agent")
    store = FakeStateStore()
    store.save_error = RuntimeError("mysql unavailable")
    manager, _ = make_manager(tmp_path, store)
    manager.tasks = [make_task("task-json")]
    manager.chat = make_chat(2)

    manager._save()  # 不抛：JSON 已成功落盘

    assert manager.persistence_error == ""
    assert not manager.state()["readOnly"]
    raw = json.loads((tmp_path / "agent-state.json").read_text(encoding="utf-8"))
    assert [task["id"] for task in raw["tasks"]] == ["task-json"]
    assert "DB save failed" in caplog.text


def test_both_stores_fail_raises_persistence_error(tmp_path, monkeypatch):
    store = FakeStateStore()
    store.save_error = RuntimeError("mysql unavailable")
    manager, _ = make_manager(tmp_path, store)
    manager.tasks = [make_task()]
    manager.chat = make_chat(1)

    def fail_replace(*args, **kwargs):
        raise PermissionError("disk unavailable")

    monkeypatch.setattr("ai.agent.os.replace", fail_replace)
    with pytest.raises(PersistenceError):
        manager._save()
    assert manager.persistence_error
    assert manager.state()["readOnly"]


def test_corrupt_json_allows_db_write_but_never_overwrites(tmp_path):
    path = tmp_path / "agent-state.json"
    path.write_bytes(b"{bad json original")
    store = FakeStateStore()
    manager, _ = make_manager(tmp_path, store)
    assert manager.state()["readOnly"] and manager._load_blocked

    manager.tasks = [make_task("task-db")]
    manager.memory = {"language": "en"}
    manager.chat = make_chat(1)
    manager._save()  # DB 可用：降级模式继续工作，不抛

    assert path.read_bytes() == b"{bad json original"  # 损坏原件必须保留
    assert [task["id"] for task in store.saved_tasks] == ["task-db"]
    assert store.saved_memory == {"language": "en"}


def test_corrupt_json_with_db_state_recovers_from_db(tmp_path):
    path = tmp_path / "agent-state.json"
    path.write_bytes(b"{bad json original")
    store = FakeStateStore(
        state={"tasks": [make_task("task-db")], "memory": {}, "chatSummary": "DB 摘要"},
        chat=make_chat(2))

    manager, _ = make_manager(tmp_path, store)

    assert not manager.state()["readOnly"]
    assert [task["id"] for task in manager.tasks] == ["task-db"]
    assert manager.chat_summary == "DB 摘要"
    assert path.read_bytes() == b"{bad json original"  # DB 命中时不碰坏文件


def test_apply_state_validates_then_assigns_atomically(tmp_path):
    manager, _ = make_manager(tmp_path, state_store=None)
    manager.tasks = [make_task("keep")]

    with pytest.raises(ValueError):
        manager._apply_state({"version": 2, "tasks": [{"id": ""}], "chat": []})
    assert [task["id"] for task in manager.tasks] == ["keep"]  # 失败绝不部分赋值

    with pytest.raises(ValueError):
        manager._apply_state({"version": 99, "tasks": [], "chat": []})

    assert manager._apply_state({"version": 2, "tasks": [make_task("new")],
                                 "chat": make_chat(1), "memory": {"language": "en"},
                                 "chatSummary": "ok"}) is True
    assert [task["id"] for task in manager.tasks] == ["new"]
    assert manager.chat_summary == "ok"
    assert manager.memory == {"language": "en"}
