"""主动发言：由系统事件驱动，说什么由 Agent 自己决定。

用户原话：
  "盯盘我不介意，说话要有人情味，而且不止要盯盘，有形态命中、仓位开停、
   数据异常等都要可以决策是否发出的，还有定时太死板了，要的是随机性"

所以这里有两层：
  1. ProactiveGate —— 决定"要不要开口"（冷却/去重/预算/概率/深夜克制）
  2. Agent.proactive —— 说什么完全交给模型，不预设版式
"""
import random
import time

import pytest

from ai.proactive import (DEFAULT_POLICY, FALLBACK_POLICY, MAX_PER_HOUR,
                          ProactiveEvent, ProactiveGate)


def _gate(rng_value=0.0, **kwargs):
    """构造一个可预测的闸门：rng 固定返回某个值，概率判定就确定。"""
    return ProactiveGate(rng=random.Random(kwargs.pop("seed", 0)), **kwargs)


def test_event_kinds_have_policies():
    for kind in ("risk", "position", "pattern", "error", "chatter", "paper"):
        assert kind in DEFAULT_POLICY
    # 紧急类必须大概率开口，闲聊类必须大概率沉默
    assert DEFAULT_POLICY["risk"]["prob"] == 1.0
    assert DEFAULT_POLICY["position"]["prob"] >= 0.8
    assert DEFAULT_POLICY["pattern"]["prob"] < 0.4
    assert DEFAULT_POLICY["chatter"]["prob"] < 0.5


def test_risk_event_almost_always_speaks():
    gate = ProactiveGate(rng=random.Random(1))
    # 直接看冷却之外的判定：risk 概率 1.0，一定过
    for i in range(5):
        gate = ProactiveGate(rng=random.Random(i))
        ev = ProactiveEvent(kind="risk", summary=f"仓位裸奔{i}", symbol="btc")
        assert gate.should_speak(ev, now=1000.0) is True


def test_pattern_event_is_mostly_silent():
    """形态命中天天有，绝大多数时候不该打断用户。"""
    spoke = 0
    for seed in range(50):
        gate = ProactiveGate(rng=random.Random(seed))
        ev = ProactiveEvent(kind="pattern", summary="吞没",
                            symbol=f"btc{seed}")
        if gate.should_speak(ev, now=1000.0):
            spoke += 1
    # 基础概率 0.28，50 次里应该明显少于一半
    assert spoke < 30, spoke


def test_cooldown_blocks_same_kind():
    gate = ProactiveGate(rng=random.Random(0))
    gate.policy["pattern"] = {"cooldown": 600, "prob": 1.0, "dedupe": 0}
    a = ProactiveEvent(kind="pattern", summary="A", symbol="btc")
    b = ProactiveEvent(kind="pattern", summary="B", symbol="eth")
    assert gate.should_speak(a, now=1000.0) is True
    assert gate.should_speak(b, now=1100.0) is False
    assert "cooldown" in gate.last_reason
    assert gate.should_speak(b, now=1700.0) is True


def test_dedupe_blocks_identical_event():
    """同一件事在窗口内说过就不再提——哪怕中间夹了别的事件。"""
    gate = ProactiveGate(rng=random.Random(0))
    gate.policy["pattern"] = {"cooldown": 0, "prob": 1.0, "dedupe": 3600}
    a = ProactiveEvent(kind="pattern", summary="BTC 吞没", symbol="btc",
                       dedupe_key="pattern:btc:engulf")
    assert gate.should_speak(a, now=1000.0) is True
    same = ProactiveEvent(kind="pattern", summary="BTC 吞没", symbol="btc",
                          dedupe_key="pattern:btc:engulf")
    assert gate.should_speak(same, now=1500.0) is False
    assert "dedupe" in gate.last_reason


def test_hourly_budget_caps_speaking():
    """行情剧烈时事件会成串，没有预算上限就是刷屏。"""
    gate = ProactiveGate(rng=random.Random(0), max_per_hour=3)
    for kind in gate.policy:
        gate.policy[kind] = {"cooldown": 0, "prob": 1.0, "dedupe": 0}
    now = 10_000.0
    for i in range(3):
        ev = ProactiveEvent(kind="position", summary=f"开仓{i}", symbol=f"s{i}")
        assert gate.should_speak(ev, now=now + i) is True
    blocked = ProactiveEvent(kind="position", summary="第四笔", symbol="s9")
    assert gate.should_speak(blocked, now=now + 5) is False
    assert "budget" in gate.last_reason
    # 一小时后预算恢复
    assert gate.should_speak(blocked, now=now + 3700) is True


def test_night_is_more_restrained():
    """凌晨 0-7 点概率减半，但不是静音——真出大事仍然要说。"""
    # 找一个 rng 值，让白天通过、深夜不通过
    night = time.mktime((2026, 11, 4, 3, 0, 0, 0, 0, -1))
    day = time.mktime((2026, 11, 4, 14, 0, 0, 0, 0, -1))
    gate = ProactiveGate(rng=random.Random(0))
    gate.policy["pattern"] = {"cooldown": 0, "prob": 0.6, "dedupe": 0}

    def speaks_at(ts):
        gate._last_by_kind.clear()
        gate._last_by_key.clear()
        gate._recent_speaks.clear()
        ev = ProactiveEvent(kind="pattern", summary="x", symbol="btc")
        return gate.should_speak(ev, now=ts)

    day_hits = sum(1 for i in range(200) if speaks_at(day))
    night_hits = sum(1 for i in range(200) if speaks_at(night))
    assert night_hits < day_hits, (night_hits, day_hits)
    # risk 类不受影响：概率 1.0 * 0.5 仍然是 0.5... 这里只验证"不是硬静音"
    gate2 = ProactiveGate(rng=random.Random(0))
    gate2.policy["risk"] = {"cooldown": 0, "prob": 1.0, "dedupe": 0}
    assert gate2.should_speak(ProactiveEvent(kind="risk", summary="裸奔"),
                              now=night) is True


def test_unknown_kind_uses_conservative_policy():
    gate = ProactiveGate(rng=random.Random(0))
    ev = ProactiveEvent(kind="some-new-kind", summary="未知事件")
    p = gate._policy("some-new-kind")
    assert p == FALLBACK_POLICY
    assert p["prob"] <= 0.2, "新事件类型一上线就刷屏是不可接受的"


def test_stats_reports_budget_and_reason():
    gate = ProactiveGate(rng=random.Random(0))
    gate.policy["position"] = {"cooldown": 0, "prob": 1.0, "dedupe": 0}
    # risk 类是 urgent，概率恒为 1，不会因为随机撞不上
    gate.policy["risk"] = {"cooldown": 0, "prob": 1.0, "dedupe": 0}
    assert gate.should_speak(ProactiveEvent(kind="risk", summary="裸奔")) is True
    s = gate.stats()
    assert s["spokenLastHour"] >= 1
    assert s["budget"] == MAX_PER_HOUR
    assert s["lastReason"] == "ok"


def test_invalid_event_is_rejected():
    gate = ProactiveGate()
    assert gate.should_speak("not an event") is False
    assert gate.last_reason == "invalid"


# ---------- 极端口径 ----------

def test_policy_overrides_merge_with_defaults():
    """自定义策略只覆盖给定字段，其余沿用默认（不会因为漏写 dedupe 就失效）。"""
    gate = ProactiveGate(policy={"pattern": {"prob": 0.9}})
    p = gate._policy("pattern")
    assert p["prob"] == 0.9
    assert p["cooldown"] == DEFAULT_POLICY["pattern"]["cooldown"]
    assert p["dedupe"] == DEFAULT_POLICY["pattern"]["dedupe"]


def test_zero_budget_silences_everything():
    gate = ProactiveGate(max_per_hour=0, rng=random.Random(0))
    ev = ProactiveEvent(kind="risk", summary="x")
    assert gate.should_speak(ev, now=1000.0) is False


# ---------- 端到端：事件 → 说话/闭嘴 ----------

def test_silent_event_leaves_no_message(tmp_path, monkeypatch):
    """模型回静默标记 → 聊天里连占位消息都不能留。"""
    import asyncio
    from tests.test_agent import make_manager, wait_for

    def provider(*a, **k):
        return {"ok": True, "reply": "[[静默]]", "usage": {"total_tokens": 5},
                "model": "test-model"}

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch, provider=provider)
        # 闸门本身有概率，这里显式放行，只验"静默不留消息"这一层
        manager.proactive_gate.policy["pattern"] = {"cooldown": 0, "prob": 1.0,
                                                     "dedupe": 0}
        before = len(manager.chat)
        ok = await manager.proactive(ProactiveEvent(
            kind="pattern", summary="BTC 吞没", symbol="btcusdt"))
        assert ok is True, "闸门放行"
        assert await wait_for(lambda: not manager.chat_runners)
        assert len(manager.chat) == before, f"静默不该留下任何消息: {manager.chat[before:]}"
        await manager.stop()
    asyncio.run(scenario())


def test_speaking_event_posts_human_style_messages(tmp_path, monkeypatch):
    """真要说的时候：多条气泡、无【】标题、带表情包都能过。"""
    import asyncio
    from tests.test_agent import make_manager, wait_for

    def provider(*a, **k):
        return {"ok": True,
                "reply": "老子又开一笔雷霆大单|||78.3k 空进去的，止损就在上面",
                "usage": {"total_tokens": 5}, "model": "test-model"}

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch, provider=provider)
        # 闸门有概率，测试显式放行，只验"说了什么"
        manager.proactive_gate.policy["position"] = {"cooldown": 0, "prob": 1.0,
                                                      "dedupe": 0}
        await manager.proactive(ProactiveEvent(
            kind="position", summary="开仓 btcusdt", symbol="btcusdt"))
        assert await wait_for(lambda: not manager.chat_runners)
        notes = [m for m in manager.chat if m.get("kind") == "proactive"]
        assert len(notes) == 2, notes
        assert notes[0]["content"] == "老子又开一笔雷霆大单"
        for note in notes:
            assert not note["content"].startswith("【")
            assert "不打扰" not in note["content"]
        await manager.stop()
    asyncio.run(scenario())


def test_gate_blocks_before_any_model_call(tmp_path, monkeypatch):
    """闸门判"不说"时不该花一次模型调用（省 token、也别扰民）。"""
    import asyncio
    from tests.test_agent import make_manager

    calls = []

    def provider(*a, **k):
        calls.append(1)
        return {"ok": True, "reply": "说了", "usage": {}, "model": "test-model"}

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch, provider=provider)
        # 把 chatter 概率压到 0，一定不放行
        manager.proactive_gate.policy["chatter"] = {"cooldown": 0, "prob": 0.0,
                                                    "dedupe": 0}
        ok = await manager.proactive(ProactiveEvent(kind="chatter", summary="闲聊"))
        assert ok is False
        assert calls == [], "闸门拦住时不能调用模型"
        await manager.stop()
    asyncio.run(scenario())


def test_prob_one_is_never_discounted():
    """prob=1.0 表示"必说"，深夜也不能打折。

    真实 bug：深夜折半逻辑只判断 urgent 标记，结果把 prob=1.0 的类别
    折成 0.5——"必说"变成了"一半概率别说"，而且随本地时钟变化，
    测试在凌晨跑就会莫名其妙失败。
    """
    night = time.mktime((2026, 11, 5, 3, 0, 0, 0, 0, -1))
    # 预算调大：这条只测"概率不打折"，每小时上限是另一条测试的事
    gate = ProactiveGate(rng=random.Random(0), max_per_hour=100)
    gate.policy["pattern"] = {"cooldown": 0, "prob": 1.0, "dedupe": 0}
    for i in range(20):
        ev = ProactiveEvent(kind="pattern", summary=f"x{i}", symbol=f"s{i}")
        assert gate.should_speak(ev, now=night) is True, f"第 {i} 次被静默了"
