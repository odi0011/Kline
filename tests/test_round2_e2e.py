"""第二批修复的独立端到端验证（verifier 独立编写，不照抄实现者测试）。

覆盖：
  A. Agent 状态进 MySQL：真实 storage.Storage（SQLite 适配器）+ 真实 AgentManager，
     DB 优先/JSON 兜底/首次迁移/DB 读失败回落/JSON 损坏由 DB 恢复/聊天 append-only。
  B. 配置密钥加密：真实 CLI（--check/加密/--decrypt）、enc:v1 落盘、备份、权限、
     环境变量覆盖、惰性 cryptography、仓库无 secrets.key/备份误提交。
  C. K线长期归档：archive 覆盖写/乱序/范围/limit/坏数据、export_klines CSV、
     main.py 四处归档调用的静态确认。
  D. 模拟仓风险修复：open_seq 同毫秒不撞 trade_key、settleBars/settleApprox 往返、
     close_missing 在监控范围收窄时按 mark 平仓且不误删范围内仓位、quant.manage 接线。
  E. 其他：mysql read/write timeout 归一化与 connect 传参、新增保留期参数。

隔离：storage 用 tests/test_paper_store.py 的 SQLite 适配器（pymysql.connect 被
monkeypatch 到内存库）；不连生产 MySQL、不启动 main.py 服务、不读/改 config.yaml
（仅测试自己写到临时目录的配置文件）。
"""
from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import decision.paper as paper_mod
import secrets_store
import storage as storage_mod
from ai.agent import AgentManager
from core.candle import Candle
from decision.paper import PaperBook
from quant import QuantEngine
from storage import Storage, _timeout_seconds
from tests.test_paper_store import SQLConnection

REPO = Path(__file__).resolve().parent.parent
NOW = 1_800_000_000_000
MINUTE = 60_000
DAY = 86_400_000

KLINE_ENV_KEYS = (
    secrets_store.ENV_KEY, "KLINE_MYSQL_PASSWORD", "KLINE_AI_KEY",
    "KLINE_TELEGRAM_TOKEN", "KLINE_WEBHOOK_URL",
    "KLINE_ACCOUNT_API_KEY", "KLINE_ACCOUNT_API_SECRET",
)

_TOOL_CACHE: dict = {}


# ---------------------------------------------------------------------------
# 公共夹具 / 工具
# ---------------------------------------------------------------------------

class FakeClock:
    def __init__(self, now_ms: int = NOW):
        self.now = int(now_ms)

    def time(self) -> float:
        return self.now / 1000.0

    def advance(self, ms: int) -> int:
        self.now += int(ms)
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(storage_mod, "time", fake)
    monkeypatch.setattr(paper_mod, "time", fake)
    return fake


@pytest.fixture
def make_store(monkeypatch):
    """真实 Storage：pymysql.connect -> 内存 SQLite（隔离，不连生产库）。"""
    connections = []

    def create(**kwargs):
        conn = SQLConnection()
        connections.append(conn)
        # _init() 的 _backfill_equity_daily() 会读现存 raw 权益表；先建空表。
        conn.db.execute(
            "CREATE TABLE equity(acct TEXT,ts BIGINT,bal DOUBLE,upnl DOUBLE,"
            "PRIMARY KEY(acct,ts))")
        monkeypatch.setattr(storage_mod.pymysql, "connect", lambda **_: conn)
        store = Storage({"user": "isolated-round2"}, **kwargs)
        assert store.enabled, "真实 DDL 必须走通"
        return store

    yield create
    for conn in connections:
        conn.db.close()


def sql(store, query, args=()):
    return store._qall(query, args)


def _ctx(price, atr=1.0, direction="bullish", bars=None, symbol_filter=None):
    data = {"price": price, "atr": atr, "score": 70.0, "gap": 20.0,
            "grade": "medium", "direction": direction, "reason": "round2",
            "regime": "trend", "pattern": "hammer", "patternStats": {},
            "maxLeverage": 20}
    if bars is not None:
        data["bars"] = bars
    return data


def _candle(open_time, o, h, l, c, v=1.0, trades=1, interval="1m"):
    return Candle(open_time=int(open_time), open=o, high=h, low=l, close=c,
                  volume=v, trades=trades, interval=interval, closed=True)


def load_tool(name: str):
    """按文件路径加载 tools/ 下的脚本模块（tools 不是包）。"""
    if name in _TOOL_CACHE:
        return _TOOL_CACHE[name]
    path = REPO / "tools" / (name + ".py")
    spec = importlib.util.spec_from_file_location("round2_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _TOOL_CACHE[name] = module
    return module


def clean_kline_env(monkeypatch):
    for key in KLINE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def run_cli(*args):
    env = {k: v for k, v in os.environ.items() if k not in KLINE_ENV_KEYS}
    return subprocess.run(
        [sys.executable, str(REPO / "tools" / "encrypt_config.py"),
         *[str(a) for a in args]],
        cwd=str(REPO), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=180, env=env)


def _sample_config():
    """6 个密钥 slot（4 固定 + 账户第 0 个）都有明文，其余是普通配置。"""
    return {
        "mysql": {"host": "127.0.0.1", "user": "kline",
                  "password": "pw-mysql-01", "database": "kline"},
        "ai": {"key": "ai-key-01"},
        "telegram": {"token": "tg-token-01"},
        "notify": {"webhook": {"url": "https://example.invalid/hook-01"}},
        "accounts": [{"name": "acc", "api_key": "acct-key-01",
                      "api_secret": "acct-secret-01"}],
        "storage": {"minutes": 30},
    }


def _write_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")


# ---------------------------------------------------------------------------
# A. Agent 状态进 MySQL（真实 Storage + 真实 AgentManager）
# ---------------------------------------------------------------------------

def _agent(state_path, store=None):
    return AgentManager(lambda: {}, lambda: None, lambda: {},
                        lambda _message: None, str(state_path),
                        state_store=store)


def _task(task_id="t1", status="scheduled"):
    return {"id": task_id, "status": status, "prompt": "分析 BTC 的测试任务",
            "title": "测试任务", "model": "test-model", "intervalSec": 0,
            "kind": "once", "runCount": 0, "createdAt": NOW,
            "startAt": NOW + MINUTE}


def _chat(message_id, role, content, ts):
    return {"id": message_id, "role": role, "content": content, "ts": int(ts)}


def test_agent_state_saved_to_db_and_restored_on_restart(tmp_path, make_store):
    store = make_store()
    state_path = tmp_path / "agent-state.json"
    agent = _agent(state_path, store)
    assert agent.tasks == [] and agent.chat == [] and agent.persistence_error == ""

    agent.tasks = [_task("t1"), _task("t2", status="paused")]
    agent.chat = [_chat("c1", "user", "你好", NOW),
                  _chat("c2", "assistant", "收到", NOW + 1)]
    agent.memory = {"language": "zh-CN"}
    agent.chat_summary = "前情提要：讨论 K线"
    agent._save()

    # 真实 SQL：三行状态 + 两条聊天都落库
    assert sql(store, "SELECT COUNT(*) FROM agent_state") == [(3,)]
    assert sql(store, "SELECT COUNT(*) FROM agent_chat") == [(2,)]
    data = store.load_agent_state()
    assert data is not None
    assert [t["id"] for t in data["tasks"]] == ["t1", "t2"]
    assert data["memory"] == {"language": "zh-CN"}
    assert data["chatSummary"] == "前情提要：讨论 K线"

    # 重启（同 store、同 state_path，JSON 也在，但必须 DB 优先）
    agent2 = _agent(state_path, store)
    assert [t["id"] for t in agent2.tasks] == ["t1", "t2"]
    assert [t["status"] for t in agent2.tasks] == ["scheduled", "paused"]
    assert [m["content"] for m in agent2.chat] == ["你好", "收到"]
    assert agent2.memory == {"language": "zh-CN"}
    assert agent2.chat_summary == "前情提要：讨论 K线"
    assert agent2.persistence_error == "" and agent2._load_blocked is False


def test_agent_state_first_start_migrates_json_into_db(tmp_path, make_store):
    store = make_store()
    state_path = tmp_path / "agent-state.json"
    state_path.write_text(json.dumps({
        "version": 2, "tasks": [_task("mig1")],
        "chat": [_chat("m1", "user", "迁移前消息", NOW),
                 _chat("m2", "assistant", "迁移前回复", NOW + 1)],
        "memory": {"response_style": "concise"},
        "chatSummary": "迁移摘要",
    }, ensure_ascii=False), encoding="utf-8")

    agent = _agent(state_path, store)
    assert [t["id"] for t in agent.tasks] == ["mig1"]
    # 首次启动：JSON 状态被镜像进 DB
    data = store.load_agent_state()
    assert data is not None and data["chatSummary"] == "迁移摘要"
    assert [m["content"] for m in store.load_agent_chat(40)] == ["迁移前消息", "迁移前回复"]

    # 删掉 JSON 再重启：只能从 DB 恢复
    state_path.unlink()
    agent2 = _agent(state_path, store)
    assert [t["id"] for t in agent2.tasks] == ["mig1"]
    assert [m["content"] for m in agent2.chat] == ["迁移前消息", "迁移前回复"]
    assert agent2.memory == {"response_style": "concise"}
    assert agent2.chat_summary == "迁移摘要"


def test_agent_state_db_read_failure_falls_back_to_json(tmp_path, make_store, monkeypatch):
    store = make_store()
    state_path = tmp_path / "agent-state.json"
    state_path.write_text(json.dumps({
        "version": 2, "tasks": [_task("json-only")],
        "chat": [_chat("j1", "user", "JSON 兜底消息", NOW)],
        "memory": {"language": "en"}, "chatSummary": "JSON摘要",
    }, ensure_ascii=False), encoding="utf-8")

    def boom():
        raise RuntimeError("模拟 MySQL 读失败")

    monkeypatch.setattr(store, "load_agent_state", boom)
    agent = _agent(state_path, store)
    assert [t["id"] for t in agent.tasks] == ["json-only"]
    assert [m["content"] for m in agent.chat] == ["JSON 兜底消息"]
    assert agent.memory == {"language": "en"}
    assert agent.chat_summary == "JSON摘要"
    assert agent.persistence_error == ""


def test_agent_state_corrupt_json_recovered_from_db(tmp_path, make_store):
    store = make_store()
    state_path = tmp_path / "agent-state.json"
    agent = _agent(state_path, store)
    agent.tasks = [_task("db-good")]
    agent.chat = [_chat("d1", "user", "DB 里的消息", NOW)]
    agent.chat_summary = "DB摘要"
    agent._save()

    state_path.write_text("{ 这不是合法 JSON", encoding="utf-8")
    # DB 有完整状态：_load 走 DB 分支，损坏的 JSON 根本不读，可正常恢复
    agent2 = _agent(state_path, store)
    assert [t["id"] for t in agent2.tasks] == ["db-good"]
    assert [m["content"] for m in agent2.chat] == ["DB 里的消息"]
    assert agent2.chat_summary == "DB摘要"
    assert agent2.persistence_error == "" and agent2._load_blocked is False
    # 之后的正常保存会把损坏的 JSON 覆盖成合法镜像
    agent2._save()
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert [t["id"] for t in saved["tasks"]] == ["db-good"]


def test_agent_state_corrupt_json_without_db_is_readonly_then_db_rescues(tmp_path, make_store):
    store = make_store()
    state_path = tmp_path / "agent-state.json"
    state_path.write_text("{ broken", encoding="utf-8")
    agent = _agent(state_path, store)     # DB 空 -> JSON 损坏 -> 只读保护
    assert agent.persistence_error != ""
    assert agent._load_blocked is True
    assert agent.tasks == []

    # DB 可写：有效状态保存成功后不再 read-only，且损坏的 JSON 原件不被覆盖
    agent.tasks = [_task("rescued")]
    agent.chat = [_chat("r1", "user", "恢复后的消息", NOW)]
    agent._save()
    assert agent.persistence_error == ""
    assert state_path.read_text(encoding="utf-8") == "{ broken"
    assert [t["id"] for t in store.load_agent_state()["tasks"]] == ["rescued"]

    # 再重启：DB 优先，损坏 JSON 不影响
    agent2 = _agent(state_path, store)
    assert [t["id"] for t in agent2.tasks] == ["rescued"]


def test_agent_state_db_write_failure_keeps_json_fallback(tmp_path, make_store, monkeypatch):
    store = make_store()
    real_save = store.save_agent_state

    def boom(*_args, **_kwargs):
        raise RuntimeError("模拟 MySQL 写失败")

    monkeypatch.setattr(store, "save_agent_state", boom)
    state_path = tmp_path / "agent-state.json"
    agent = _agent(state_path, store)      # 启动迁移写 DB 失败只告警
    agent.tasks = [_task("json-survives")]
    agent.chat = [_chat("k1", "user", "JSON 兜底", NOW)]
    agent._save()                          # 不抛，JSON 照常写
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert [t["id"] for t in saved["tasks"]] == ["json-survives"]

    monkeypatch.setattr(store, "save_agent_state", real_save)
    agent2 = _agent(state_path, store)     # DB 里没有状态行 -> JSON 兜底
    assert [t["id"] for t in agent2.tasks] == ["json-survives"]


def test_agent_chat_is_append_only_while_memory_window_is_40(tmp_path, make_store):
    store = make_store()
    agent = _agent(tmp_path / "agent-state.json", store)
    first = [_chat("m%02d" % index, "user" if index % 2 == 0 else "assistant",
                   "消息%d" % index, NOW + index) for index in range(40)]
    agent.chat = list(first)
    agent._save()
    assert sql(store, "SELECT COUNT(*) FROM agent_chat") == [(40,)]

    # 内存窗口滑出最早一条（仍保持 40 条），库里应保留旧消息（append-only）
    agent.chat = first[1:] + [_chat("m40", "user", "新消息40", NOW + 40)]
    agent._save()
    assert sql(store, "SELECT COUNT(*) FROM agent_chat") == [(41,)]
    loaded = store.load_agent_chat(40)
    assert [m["id"] for m in loaded] == ["m%02d" % index for index in range(1, 41)]
    # 重启只恢复窗口内 40 条（更早的仍在库里可查）
    agent2 = _agent(tmp_path / "agent-state.json", store)
    assert len(agent2.chat) == 40
    assert agent2.chat[-1]["content"] == "新消息40"


def test_agent_chat_same_ms_order_after_restart(tmp_path, make_store):
    """同一毫秒的 user/assistant 消息必须保持写入顺序（agent_chat.seq 列）。"""
    store = make_store()
    agent = _agent(tmp_path / "agent-state.json", store)
    # 真实代码里 user/assistant 两条消息的 ts 常常是同一毫秒（两次 now_ms 紧挨着）
    agent.chat = [
        {"id": "zzzzzz-user", "role": "user", "content": "先问", "ts": NOW},
        {"id": "aaaaaa-assistant", "role": "assistant", "content": "后答", "ts": NOW},
    ]
    agent._save()
    restored = _agent(tmp_path / "agent-state.json", store)
    assert [m["role"] for m in restored.chat] == ["user", "assistant"]


def test_agent_chat_old_table_migration_adds_seq(monkeypatch):
    """老库 agent_chat 没有 seq 列：_init 必须检测并 ALTER 补列，之后同毫秒顺序可恢复。"""
    conn = SQLConnection()
    conn.db.execute("CREATE TABLE equity(acct TEXT,ts BIGINT,bal DOUBLE,"
                    "upnl DOUBLE,PRIMARY KEY(acct,ts))")
    # 老版本表结构（无 seq）
    conn.db.execute(
        "CREATE TABLE agent_chat(msg_id TEXT NOT NULL PRIMARY KEY, ts BIGINT, "
        "role VARCHAR(16), content MEDIUMTEXT, data MEDIUMTEXT)")
    monkeypatch.setattr(storage_mod.pymysql, "connect", lambda **_: conn)
    store = Storage({"user": "isolated-migrate"})
    assert store.enabled
    columns = [row[1] for row in conn.db.execute("PRAGMA table_info('agent_chat')")]
    # ALTER TABLE ADD COLUMN 只能追加到末尾（MySQL/SQLite 同），列顺序与新建表不同，
    # 但 save/load 都用显式列名，功能不受影响；关键是有 seq 列且能正确排序。
    assert "seq" in columns
    assert {"msg_id", "ts", "role", "content", "data"} <= set(columns)
    # 迁移后同毫秒顺序仍然正确
    store.save_agent_chat([
        {"id": "zzzzzz-user", "role": "user", "content": "先问", "ts": NOW},
        {"id": "aaaaaa-assistant", "role": "assistant", "content": "后答", "ts": NOW},
    ])
    assert [m["role"] for m in store.load_agent_chat()] == ["user", "assistant"]
    conn.db.close()


def test_clear_chat_does_not_reappear_after_restart(tmp_path, make_store):
    """清空聊天后重启不能复活：窗口<40 时 AgentManager 会 trim 掉库里孤儿消息。"""
    store = make_store()
    agent = _agent(tmp_path / "agent-state.json", store)
    agent.chat = [_chat("c%d" % index, "user" if index % 2 == 0 else "assistant",
                        "消息%d" % index, NOW + index) for index in range(6)]
    agent._save()
    # clear_chat 的真实收尾：清空内存窗口 + 摘要后 _emit -> _save
    agent.chat = []
    agent.chat_summary = ""
    agent._save()
    restored = _agent(tmp_path / "agent-state.json", store)
    assert restored.chat == []


def test_chat_sync_keeps_long_archive_when_window_full(tmp_path, make_store):
    """窗口=40 时 _sync_chat_window 不得动 DB：更早的长期存档不能被误删。"""
    store = make_store()
    agent = _agent(tmp_path / "agent-state.json", store)
    window = ["m%03d" % index for index in range(40)]
    agent.chat = [_chat(mid, "user" if index % 2 == 0 else "assistant",
                        "消息" + mid, NOW + index)
                  for index, mid in enumerate(window)]
    agent._save()
    assert sql(store, "SELECT COUNT(*) FROM agent_chat") == [(40,)]

    # 正常滑动窗口（仍 40 条，掉 10 条旧的、进 10 条新的）：库应累计到 50 条
    window = window[10:] + ["n%03d" % index for index in range(10)]
    agent.chat = [_chat(mid, "user" if index % 2 == 0 else "assistant",
                        "消息" + mid, NOW + 40 + index)
                  for index, mid in enumerate(window)]
    agent._save()
    assert sql(store, "SELECT COUNT(*) FROM agent_chat") == [(50,)]

    # 重启只加载最近 40 条；再保存一次（窗口仍 40）也不能删掉更早的 10 条存档
    agent2 = _agent(tmp_path / "agent-state.json", store)
    assert len(agent2.chat) == 40
    agent2._save()
    assert sql(store, "SELECT COUNT(*) FROM agent_chat") == [(50,)]
    assert [m["id"] for m in store.load_agent_chat(40)] == [
        m["id"] for m in agent2.chat]


def test_chat_sync_trims_only_after_successful_db_write(tmp_path, make_store, monkeypatch):
    """DB 写失败时不能收口删库；恢复写入后才按缩短后的窗口 trim。"""
    store = make_store()
    agent = _agent(tmp_path / "agent-state.json", store)
    agent.chat = [_chat("m%02d" % index, "user" if index % 2 == 0 else "assistant",
                        "消息%d" % index, NOW + index) for index in range(40)]
    agent._save()
    assert sql(store, "SELECT COUNT(*) FROM agent_chat") == [(40,)]

    # 模拟 compact：内存窗口缩到 10 条，但聊天写库失败
    agent.chat = agent.chat[:10]
    real_save_chat = store.save_agent_chat
    calls = {"n": 0}

    def failing(messages):
        calls["n"] += 1
        raise RuntimeError("模拟 MySQL 写失败")

    monkeypatch.setattr(store, "save_agent_chat", failing)
    agent._save()                      # 只告警，不抛
    assert calls["n"] == 1
    assert sql(store, "SELECT COUNT(*) FROM agent_chat") == [(40,)]   # 失败时不删库

    # 恢复写库后再保存：此时才按 10 条窗口收口，重启不复活
    monkeypatch.setattr(store, "save_agent_chat", real_save_chat)
    agent._save()
    assert sql(store, "SELECT COUNT(*) FROM agent_chat") == [(10,)]
    restored = _agent(tmp_path / "agent-state.json", store)
    assert len(restored.chat) == 10


# ---------------------------------------------------------------------------
# B. 配置密钥加密（真实 CLI + secrets_store）
# ---------------------------------------------------------------------------

def test_encrypt_config_cli_full_cycle(tmp_path, monkeypatch):
    clean_kline_env(monkeypatch)
    cfg_path = tmp_path / "config.yaml"
    original = _sample_config()
    _write_yaml(cfg_path, original)

    # 1) --check：明文、无密钥、全部 slot 都能识别
    check = run_cli("--config", cfg_path, "--check")
    assert check.returncode == 0, check.stderr
    assert "明文" in check.stdout and "mysql.password" in check.stdout

    # 2) 默认模式：加密写回（自动生成 secrets.key + 带时间戳备份）
    encrypted = run_cli("--config", cfg_path)
    assert encrypted.returncode == 0, encrypted.stderr
    key_file = tmp_path / "secrets.key"
    assert key_file.exists()
    backups = sorted(tmp_path.glob("config.yaml.bak.*"))
    assert len(backups) == 1

    enc_cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    slots = secrets_store.secret_slots(enc_cfg)
    assert len(slots) == 6
    for path, _env in slots:
        value = secrets_store.get_path(enc_cfg, path)
        assert secrets_store.is_encrypted(value), "slot 未加密: %s" % (path,)
    # 明文密钥不再出现在配置文件里；非密钥字段保持原样
    raw = cfg_path.read_text(encoding="utf-8")
    for path, _env in slots:
        assert str(secrets_store.get_path(original, path)) not in raw
    assert enc_cfg["storage"]["minutes"] == 30
    assert enc_cfg["accounts"][0]["name"] == "acc"

    # 主密钥格式：urlsafe base64 的 32 字节
    key_text = key_file.read_bytes().strip()
    assert len(base64.urlsafe_b64decode(key_text + b"=" * (-len(key_text) % 4))) == 32

    # 3) --check：全部已加密、主密钥存在
    check2 = run_cli("--config", cfg_path, "--check")
    assert check2.returncode == 0, check2.stderr
    assert "已加密" in check2.stdout
    status = load_tool("encrypt_config").run(str(cfg_path), check=True)
    assert status["key"] is True
    assert {s["state"] for s in status["slots"]} == {"encrypted"}

    # 4) --decrypt：还原为原文、再生成一个备份
    decrypted = run_cli("--config", cfg_path, "--decrypt")
    assert decrypted.returncode == 0, decrypted.stderr
    assert yaml.safe_load(cfg_path.read_text(encoding="utf-8")) == original
    assert len(sorted(tmp_path.glob("config.yaml.bak.*"))) == 2

    # 5) POSIX 权限（Windows chmod 语义有限，按任务约定跳过）
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(key_file).st_mode) & 0o077 == 0
        assert stat.S_IMODE(os.stat(cfg_path).st_mode) & 0o077 == 0
        for backup in backups:
            assert stat.S_IMODE(os.stat(backup).st_mode) & 0o077 == 0


def test_secrets_env_override_and_lazy_cryptography(tmp_path, monkeypatch):
    clean_kline_env(monkeypatch)
    cfg_path = tmp_path / "config.yaml"
    cfg = _sample_config()
    _write_yaml(cfg_path, cfg)

    # 主密钥来自环境变量时不需要落盘 secrets.key
    key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    monkeypatch.setenv(secrets_store.ENV_KEY, key)
    enc = secrets_store.encrypt_config(cfg, cfg_path, create_key=True)
    for path, _env in secrets_store.secret_slots(enc):
        assert secrets_store.is_encrypted(secrets_store.get_path(enc, path))
    assert not (tmp_path / "secrets.key").exists()

    # 环境变量覆盖优先级最高（服务器上不落盘密钥的部署方式）
    monkeypatch.setenv("KLINE_MYSQL_PASSWORD", "from-env-pw")
    dec = secrets_store.decrypt_config(enc, cfg_path)
    assert dec["mysql"]["password"] == "from-env-pw"
    assert dec["ai"]["key"] == cfg["ai"]["key"]
    assert dec["accounts"][0]["api_secret"] == cfg["accounts"][0]["api_secret"]

    # 惰性 cryptography：明文配置的解密路径完全不调用 cryptography
    monkeypatch.delenv(secrets_store.ENV_KEY)
    monkeypatch.delenv("KLINE_MYSQL_PASSWORD")

    def no_crypto():
        raise secrets_store.SecretsError("未安装 cryptography")

    monkeypatch.setattr(secrets_store, "_import_fernet", no_crypto)
    assert secrets_store.decrypt_config(cfg, cfg_path) == cfg
    assert len(secrets_store.audit_plaintext(cfg)) == 6
    # 但真正遇到密文时必须显式报错（不能静默返回密文/明文混用）
    with pytest.raises(secrets_store.SecretsError):
        secrets_store.decrypt_config(enc, cfg_path)

    # 错误密钥 / 坏密钥格式也必须报错
    monkeypatch.delenv(secrets_store.ENV_KEY, raising=False)
    monkeypatch.setenv(secrets_store.ENV_KEY,
                       base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"))
    monkeypatch.setattr(secrets_store, "_import_fernet", secrets_store._import_fernet)
    with pytest.raises(secrets_store.SecretsError):
        secrets_store.decrypt_config(enc, cfg_path)
    with pytest.raises(secrets_store.SecretsError):
        secrets_store._decode_key("not-a-valid-key")


def test_plain_config_startup_path_needs_no_cryptography(tmp_path, monkeypatch):
    clean_kline_env(monkeypatch)
    # 1) secrets_store 模块本身 import 时不拉 cryptography（子进程干净验证）
    probe = ("import sys; sys.path.insert(0, %r); import secrets_store; "
             "print('crypto=', 'cryptography' in sys.modules)" % str(REPO))
    out = subprocess.run([sys.executable, "-c", probe], cwd=str(REPO),
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=180)
    assert out.returncode == 0, out.stderr
    assert "crypto= False" in out.stdout

    # 2) 模拟"没装 cryptography"：明文配置的启动路径（decrypt_config + write_config_file）
    #    必须照常工作，只有真正需要加解密时才报错
    def no_crypto():
        raise secrets_store.SecretsError("未安装 cryptography")

    monkeypatch.setattr(secrets_store, "_import_fernet", no_crypto)
    cfg_path = tmp_path / "config.yaml"
    cfg = _sample_config()
    _write_yaml(cfg_path, cfg)
    loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert secrets_store.decrypt_config(loaded, cfg_path) == loaded
    assert secrets_store.audit_plaintext(loaded)      # 启动日志会提示 6 个明文 slot

    import main as main_mod
    target = tmp_path / "config2.yaml"
    _write_yaml(target, cfg)
    payload = main_mod.write_config_file(cfg, str(target))
    assert isinstance(payload, dict)
    assert payload["mysql"]["password"] == cfg["mysql"]["password"]
    assert yaml.safe_load(target.read_text(encoding="utf-8")) == cfg


def test_write_config_file_encrypts_when_key_available(tmp_path, monkeypatch):
    """main.write_config_file（运行时保存配置的唯一入口）在有用密钥时必须加密落盘。"""
    clean_kline_env(monkeypatch)
    import main as main_mod

    cfg = _sample_config()
    path = tmp_path / "config.yaml"
    _write_yaml(path, cfg)
    key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    monkeypatch.setenv(secrets_store.ENV_KEY, key)
    payload = main_mod.write_config_file(cfg, str(path))
    assert isinstance(payload, dict)
    reloaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    for slot, _env in secrets_store.secret_slots(reloaded):
        assert secrets_store.is_encrypted(secrets_store.get_path(reloaded, slot))
    decrypted = secrets_store.decrypt_config(reloaded, path)
    assert decrypted["mysql"]["password"] == cfg["mysql"]["password"]
    assert decrypted["accounts"][0]["api_secret"] == cfg["accounts"][0]["api_secret"]
    assert not (tmp_path / "config.yaml.tmp").exists()   # 原子写成功不留临时文件


def test_write_config_file_atomic_failure_keeps_old_config(tmp_path, monkeypatch):
    """原子写失败（os.replace 抛错）时：旧配置不被截断、.tmp 被清理。"""
    clean_kline_env(monkeypatch)
    import main as main_mod

    path = tmp_path / "config.yaml"
    old_cfg = _sample_config()
    _write_yaml(path, old_cfg)
    old_text = path.read_text(encoding="utf-8")

    def boom(_src, _dst):
        raise OSError("模拟 replace 失败")

    monkeypatch.setattr(main_mod.os, "replace", boom)
    with pytest.raises(OSError):
        main_mod.write_config_file(_sample_config(), str(path))
    assert path.read_text(encoding="utf-8") == old_text
    assert not (tmp_path / "config.yaml.tmp").exists()


def test_repo_does_not_track_secret_material():
    # config.yaml / secrets.key / 加密备份 都必须被 .gitignore 忽略
    for path in ("config.yaml", "secrets.key", "config.yaml.bak.20260101-000000"):
        result = subprocess.run(["git", "check-ignore", "-v", "--no-index", path],
                                cwd=str(REPO), capture_output=True, text=True)
        assert result.returncode == 0, path + " 必须被 ignore: " + result.stderr
    tracked = subprocess.run(
        ["git", "ls-files", "config.yaml", "secrets.key", "config.yaml.bak.x"],
        cwd=str(REPO), capture_output=True, text=True)
    assert tracked.returncode == 0 and tracked.stdout.strip() == ""
    ignore_text = (REPO / ".gitignore").read_text(encoding="utf-8")
    assert "config.yaml" in ignore_text and "secrets.key" in ignore_text
    assert "config.yaml.bak.*" in ignore_text


# ---------------------------------------------------------------------------
# C. K线长期归档 + CSV 导出
# ---------------------------------------------------------------------------

def test_candle_archive_roundtrip_overwrite_order_range_limit(make_store):
    store = make_store()
    assert (store.candle_archive_days, store.agent_chat_days) == (0, 0)
    base = NOW - 30 * MINUTE
    candles = [_candle(base + index * MINUTE, 100 + index, 101 + index,
                       99 + index, 100.5 + index, 10 + index, index + 1)
               for index in range(25)]
    # 乱序写入（奇偶交错）
    shuffled = candles[::2] + candles[1::2]
    assert store.archive_candles("btcusdt", "1m", shuffled) == 25

    # 同主键覆盖写：close/trades 以最后一次为准
    overwrite = _candle(base + 3 * MINUTE, 1, 2, 0.5, 999.0, 7.0, 42)
    store.archive_candle("btcusdt", "1m", overwrite)

    rows = store.archive_range("btcusdt", "1m", base, base + 24 * MINUTE)
    assert len(rows) == 25
    assert [r["t"] for r in rows] == [base + index * MINUTE for index in range(25)]
    assert rows[3]["c"] == 999.0 and rows[3]["trades"] == 42
    assert store.archive_range("btcusdt", "1m", base, base + 24 * MINUTE,
                               limit=5) == rows[:5]
    assert len(store.archive_range("btcusdt", "1m", base, base + 24 * MINUTE,
                                   limit=0)) == 1
    assert len(store.archive_range("btcusdt", "1m", base, base + 24 * MINUTE,
                                   limit=10 ** 9)) == 25
    # 范围含两端
    assert [r["t"] for r in store.archive_range("btcusdt", "1m", base, base)] == [base]
    assert store.archive_range("btcusdt", "1m", base + 6 * MINUTE,
                               base + 6 * MINUTE + 1)[0]["t"] == base + 6 * MINUTE
    # 不同 symbol / interval 互不串
    store.archive_candles("ethusdt", "1m", candles[:2])
    assert len(store.archive_range("ethusdt", "1m", base, base + MINUTE)) == 2
    assert store.archive_range("ethusdt", "5m", base, base + DAY) == []
    # 坏数据（open_time<=0 / 无属性对象）逐根跳过，不抛
    assert store.archive_candles("btcusdt", "1m", [
        _candle(0, 1, 1, 1, 1), object()]) == 0
    assert store.archive_candles("", "1m", candles[:1]) == 0
    assert store.archive_candle("btcusdt", "1m", None) is None
    # 未启用存储：安全 no-op
    disabled = Storage({})
    assert disabled.archive_candles("btcusdt", "1m", candles) == 0
    assert disabled.archive_range("btcusdt", "1m", base, base + DAY) == []


def test_new_retention_params_prune_archive_and_agent_chat(make_store):
    # 默认 0 = 永久：旧归档与旧聊天都不删
    store = make_store()
    old = NOW - 500 * DAY
    store.archive_candle("btcusdt", "1m", _candle(old, 1, 1, 1, 1))
    store.save_agent_chat([{"id": "old-msg", "role": "user",
                            "content": "旧消息", "ts": old}])
    store.prune(NOW)
    assert len(store.archive_range("btcusdt", "1m", old, old)) == 1
    assert [m["id"] for m in store.load_agent_chat(10)] == ["old-msg"]

    # 配置保留 1 天：旧行删、新行留
    store2 = make_store(candle_archive_days=1, agent_chat_days=1)
    store2.archive_candle("btcusdt", "1m", _candle(old, 1, 1, 1, 1))
    store2.archive_candle("btcusdt", "1m", _candle(NOW - MINUTE, 2, 2, 2, 2))
    store2.save_agent_chat([
        {"id": "old", "role": "user", "content": "旧", "ts": old},
        {"id": "new", "role": "user", "content": "新", "ts": NOW - MINUTE}])
    store2.prune(NOW)
    assert [r["t"] for r in store2.archive_range(
        "btcusdt", "1m", old, NOW)] == [NOW - MINUTE]
    assert [m["id"] for m in store2.load_agent_chat(10)] == ["new"]


def test_export_klines_csv(make_store, tmp_path):
    store = make_store()
    base = NOW - 25 * MINUTE
    candles = [_candle(base + index * MINUTE, 100 + index, 100.5 + index,
                       99.5 + index, 100.2 + index, 3.0 + index, index + 1)
               for index in range(22)]
    store.archive_candles("btcusdt", "1m", candles)
    export_tool = load_tool("export_klines")

    out = tmp_path / "out.csv"
    count = export_tool.export(store, "BTCUSDT", "1m", base,
                               base + 21 * MINUTE, str(out))
    assert count == 22
    with out.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == ["open_time", "datetime_utc", "open", "high", "low",
                       "close", "volume", "trades"]
    assert len(rows) == 23
    assert int(rows[1][0]) == base
    expected = dt.datetime.fromtimestamp(base / 1000, tz=dt.timezone.utc)
    assert rows[1][1] == expected.strftime("%Y-%m-%d %H:%M:%S")
    assert float(rows[1][2]) == 100.0 and int(rows[1][7]) == 1
    assert int(rows[-1][0]) == base + 21 * MINUTE

    # 空区间：只写表头、返回 0
    empty = tmp_path / "empty.csv"
    assert export_tool.export(store, "btcusdt", "1m", NOW + DAY,
                              NOW + 2 * DAY, str(empty)) == 0
    with empty.open(encoding="utf-8") as handle:
        assert len(list(csv.reader(handle))) == 1

    # 分块导出：step 修复（不再把 interval 毫秒再乘 1000）后，用小于一个周期的
    # chunk 能真正走多窗口分页；结果必须与单窗口一致、不重不漏。
    chunked = tmp_path / "chunked.csv"
    assert export_tool.export(store, "btcusdt", "1m", base,
                              base + 21 * MINUTE, str(chunked),
                              chunk_ms=3 * MINUTE) == 22
    with chunked.open(encoding="utf-8", newline="") as handle:
        chunked_rows = list(csv.reader(handle))
    assert chunked_rows[0] == rows[0]
    assert chunked_rows[1:] == rows[1:]

    # 参数校验与时间解析
    with pytest.raises(ValueError):
        export_tool.export(store, "btcusdt", "1m", NOW, NOW - 1, str(out))
    with pytest.raises(ValueError):
        export_tool.export(store, "", "1m", NOW, NOW + 1, str(out))
    assert export_tool.parse_time(str(base)) == base
    assert export_tool.parse_time("2026-01-02") == int(
        dt.datetime(2026, 1, 2).timestamp() * 1000)
    with pytest.raises(argparse.ArgumentTypeError):
        export_tool.parse_time("not-a-time")


def test_export_cli_reports_missing_key_without_traceback(tmp_path, monkeypatch):
    """配置含密文但缺密钥时 export CLI 应给中文提示+退出码2，不能抛 traceback。"""
    clean_kline_env(monkeypatch)
    export_tool = load_tool("export_klines")
    cfg = _sample_config()
    cfg_path = tmp_path / "config.yaml"
    key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    monkeypatch.setenv(secrets_store.ENV_KEY, key)
    enc = secrets_store.encrypt_config(cfg, cfg_path, create_key=True)
    enc_path = tmp_path / "enc.yaml"
    _write_yaml(enc_path, enc)
    monkeypatch.delenv(secrets_store.ENV_KEY, raising=False)
    # 期望：中文提示 + 退出码 2；当前实现应该抛未捕获的 SecretsError
    rc = export_tool.main(["-c", str(enc_path), "--symbol", "btcusdt",
                           "--interval", "1m", "--from", "0", "--to", "1",
                           "--out", str(tmp_path / "o.csv")])
    assert rc == 2


def test_main_archive_wiring_static():
    source = (REPO / "main.py").read_text(encoding="utf-8")
    # 收线单根归档 1 处 + 批量归档 3 处（按需订阅 / 启动回补 / 重试）
    assert source.count("storage.archive_candle(") == 1
    assert source.count("storage.archive_candles(") == 3

    closed_index = source.index("if closed:")
    closed_block = source[closed_index:closed_index + 900]
    assert "storage.save_candle(sym, iv, candle)" in closed_block
    assert "storage.archive_candle(sym, iv, candle)" in closed_block
    assert "storage.eval_outcomes" in closed_block

    batch_call = "storage.archive_candles(sym, iv, candles)"
    position = 0
    for _ in range(3):
        index = source.index(batch_call, position)
        position = index + 1
        before = source[max(0, index - 500):index]
        after = source[index:index + 300]
        assert "agg.seed(candles)" in before          # 都在 seed 后归档
        assert "except Exception" in after            # 归档失败不能打断主流程
    assert "[archive] 按需" in source and "[archive] 重试" in source


# ---------------------------------------------------------------------------
# D. 模拟仓风险修复（open_seq / settle 质量标记 / close_missing）
# ---------------------------------------------------------------------------

def test_open_seq_and_trade_key_no_same_ms_collision(make_store, clock):
    store = make_store()
    book = PaperBook(store=store)
    assert book.tick("btcusdt", _ctx(100.0))["open_seq"] == 1
    # 同一毫秒（clock 固定）内连续打止损平仓 + 立即重开：open_seq 不同 -> trade_key 不撞
    book.tick("btcusdt", _ctx(90.0))
    book.tick("btcusdt", _ctx(80.0))
    trades = store.paper_trades()
    assert len(trades) == 2
    assert sorted(t["openSeq"] for t in trades) == [1, 2]
    keys = [row[0] for row in sql(
        store, "SELECT trade_key FROM paper_trades ORDER BY open_seq")]
    assert keys == ["btcusdt:%d:1" % NOW, "btcusdt:%d:2" % NOW]
    assert book.positions["btcusdt"].open_seq == 3

    # 同一笔流水重放（网络重试）被 INSERT IGNORE 幂等去重
    duplicate = dict(trades[-1])
    duplicate["pnl"] = 0.0
    store.save_paper_trade(duplicate)
    assert sql(store, "SELECT COUNT(*) FROM paper_trades") == [(2,)]

    # 重启后序号从恢复的最大值继续，不归零
    book2 = PaperBook(store=store)
    assert book2.positions["btcusdt"].open_seq == 3
    book2.tick("btcusdt", _ctx(70.0))
    trades2 = store.paper_trades()
    assert len(trades2) == 3
    assert max(t["openSeq"] for t in trades2) == 3
    reopened = json.loads(sql(store, "SELECT data FROM paper_positions "
                              "WHERE symbol=%s", ("btcusdt",))[0][0])
    assert reopened["open_seq"] == 4


def test_settle_quality_flags_persist_and_recovered_settlement(make_store, clock):
    store = make_store()
    # 1) 列往返：settleBars / settleApprox / openSeq
    store.save_paper_trade({
        "symbol": "btcusdt", "entryTs": NOW - MINUTE, "openSeq": 7,
        "closedAt": NOW, "reason": "timeout", "entry": 100.0, "exit": 100.5,
        "pnl": 1.0, "settleBars": 15, "settleApprox": True, "recovered": True})
    row = store.paper_trades()[0]
    assert row["settleBars"] == 15 and int(row["settleApprox"]) == 1
    assert row["openSeq"] == 7 and row["recovered"] is True
    assert sql(store, "SELECT settle_bars,settle_approx,open_seq "
                      "FROM paper_trades")[0] == (15, 1, 7)

    # 2) 真实恢复补结算（bars 覆盖到期窗口 -> 非近似）
    book = PaperBook(store=store)
    book.tick("ethusdt", _ctx(100.0))
    position = book.positions["ethusdt"]
    position.entry_ts = clock.now - 30 * MINUTE
    position.expires_ts = clock.now - 15 * MINUTE
    book._persist(position)
    bars = []
    stamp = position.entry_ts + MINUTE
    while stamp <= position.expires_ts:
        bars.append(_candle(stamp, 100.0, 100.2, 99.8, 100.0))
        stamp += MINUTE
    book2 = PaperBook(store=store)
    assert "ethusdt" in book2._recovered
    book2.tick("ethusdt", _ctx(100.0, bars=bars))
    settled = [t for t in store.paper_trades() if t["symbol"] == "ethusdt"][0]
    assert settled["recovered"] is True and settled["reason"] == "timeout"
    assert settled["settleBars"] == len(bars)
    assert int(settled["settleApprox"]) == 0

    # 3) 没有 bars 的近似补结算：settleBars=0 / settleApprox=1
    reopened = book2.positions["ethusdt"]
    reopened.entry_ts = clock.now - 30 * MINUTE
    reopened.expires_ts = clock.now - 15 * MINUTE
    book2._persist(reopened)
    book3 = PaperBook(store=store)
    book3.tick("ethusdt", _ctx(101.0))
    approx = [t for t in store.paper_trades()
              if t["symbol"] == "ethusdt" and int(t["settleApprox"]) == 1]
    assert len(approx) == 1
    assert approx[0]["settleBars"] == 0 and approx[0]["recovered"] is True


def test_close_missing_on_scope_change(make_store, clock, tmp_path):
    store = make_store()
    book = PaperBook(store=store)
    book.tick("btcusdt", _ctx(100.0))
    book.tick("ethusdt", _ctx(100.0))
    book.tick("solusdt", _ctx(100.0))
    # 没有任何可用价格的仓位（mark=0）：只删未平仓行，不能编造成交价
    book.positions["solusdt"].mark = 0.0
    book._persist(book.positions["solusdt"])

    done = book.close_missing({"btcusdt"})
    assert sorted(done) == ["ethusdt", "solusdt"]
    assert set(book.positions) == {"btcusdt"}
    trades = store.paper_trades()
    eth = [t for t in trades if t["symbol"] == "ethusdt"]
    assert len(eth) == 1 and eth[0]["reason"] == "removed" and eth[0]["openSeq"] == 2
    assert not [t for t in trades if t["symbol"] == "solusdt"]
    assert [r[0] for r in sql(
        store, "SELECT symbol FROM paper_positions ORDER BY symbol")] == ["btcusdt"]
    # keep 全包含：不动账本
    assert book.close_missing({"btcusdt", "ethusdt"}) == []

    # quant.manage 接线：symbol_filter 收窄 -> 范围外的模拟仓被按 mark 平掉
    engine = QuantEngine({"list": []}, str(tmp_path / "q.json"), paper_store=store)
    assert set(engine.paper.positions) == {"btcusdt"}
    engine.paper.tick("ethusdt", _ctx(100.0))
    engine.symbol_filter = {"btcusdt"}
    assert engine._settle_paper_out_of_scope() == ["ethusdt"]
    removed = [t for t in store.paper_trades()
               if t["symbol"] == "ethusdt" and t["reason"] == "removed"]
    assert len(removed) == 2          # 手工 close_missing 一次 + 引擎接线一次
    # symbol_filter 未确定（None）时绝不动账本
    engine.symbol_filter = None
    assert engine._settle_paper_out_of_scope() == []

    # 静态确认 manage() 里确实接线了该清理
    quant_source = (REPO / "quant.py").read_text(encoding="utf-8")
    manage_start = quant_source.index("def manage(self)")
    manage_body = quant_source[manage_start:manage_start + 1500]
    assert "_settle_paper_out_of_scope()" in manage_body


# ---------------------------------------------------------------------------
# E. mysql 读写超时 + 静态接线
# ---------------------------------------------------------------------------

def test_storage_timeouts_normalized_and_passed_to_connect(monkeypatch):
    assert _timeout_seconds(30) == 30
    assert _timeout_seconds("45") == 45
    assert _timeout_seconds(0) is None
    assert _timeout_seconds(None) is None
    assert _timeout_seconds("bad") == 30
    # 负数不是合法 socket 超时：必须回退默认 30（本轮修复项）
    assert _timeout_seconds(-5) == 30
    assert _timeout_seconds("-1") == 30

    captured = {}
    conn = SQLConnection()
    conn.db.execute("CREATE TABLE equity(acct TEXT,ts BIGINT,bal DOUBLE,"
                    "upnl DOUBLE,PRIMARY KEY(acct,ts))")

    def fake_connect(**kwargs):
        captured.update(kwargs)
        return conn

    monkeypatch.setattr(storage_mod.pymysql, "connect", fake_connect)
    store = storage_mod.Storage({"user": "isolated-timeout",
                                 "read_timeout": 12, "write_timeout": 0})
    assert store.enabled
    assert captured["read_timeout"] == 12
    assert captured["write_timeout"] is None
    conn.db.close()


def test_main_round2_wiring_static():
    source = (REPO / "main.py").read_text(encoding="utf-8")
    # Agent 状态注入 Storage（未连库时 None，退回 JSON）
    assert "import secrets_store" in source
    assert "state_store=storage if storage.enabled else None" in source
    # 新增保留期参数接线
    assert 'candle_archive_days=int(scfg.get("candle_archive_days", 0) or 0)' in source
    assert 'agent_chat_days=int(scfg.get("agent_chat_days", 0) or 0)' in source
    # 启动解密必须早于 asyncio.run
    decrypt_index = source.index(
        "cfg = secrets_store.decrypt_config(cfg, args.config)")
    run_index = source.index("asyncio.run(run(cfg, args.config))")
    assert decrypt_index < run_index
    # 运行时写配置统一走加密写入函数，旧的直接明文 safe_dump 已移除
    assert source.count("write_config_file(cfg, config_path)") == 3
    assert "yaml.safe_dump(cfg, f" not in source
    # 启动与保存时都会收紧权限 / 提示明文
    assert "secrets_store.harden_permissions(args.config)" in source
    assert "secrets_store.audit_plaintext(cfg)" in source

    # write_config_file 必须是原子写：tmp -> fsync -> replace -> finally 清理
    write_start = source.index("def write_config_file")
    write_body = source[write_start:source.index("def start_static")]
    assert "os.fsync" in write_body
    assert "os.replace(tmp_path, path)" in write_body
    assert "finally:" in write_body

    # README 的归档说明必须与实现一致（1h/4h 不在每次收线归档）
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "1h/4h 辅助周期在启动回补与按需订阅时归档" in readme

    # export step 不再把 interval 毫秒重复乘 1000；load_store 在 try 内
    export_source = (REPO / "tools" / "export_klines.py").read_text(encoding="utf-8")
    assert "INTERVAL_MS.get(interval, 60_000))" in export_source
    assert "INTERVAL_MS.get(interval, 60_000) * 1000" not in export_source
    load_index = export_source.index("store = load_store(args.config)")
    assert "try:" in export_source[load_index - 60:load_index]

    storage_source = (REPO / "storage.py").read_text(encoding="utf-8")
    assert "read_timeout=_timeout_seconds" in storage_source
    assert "write_timeout=_timeout_seconds" in storage_source
    # 新表 DDL 都在 _init 里
    for table in ("candle_archive", "agent_state", "agent_chat"):
        assert "CREATE TABLE IF NOT EXISTS " + table in storage_source
