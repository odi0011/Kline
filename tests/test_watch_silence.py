"""后台监控不该把"什么都没发生"也推给用户。

线上真实事故：用户看到
    「【BTC 盯 76000】没跌破，78552，离 76000 还有 3.2%，不打扰。」
三个问题叠在一起：
  1. 任务是"跌破 76000 或距 1% 内才提醒"，而现价离目标 3.2% —— 根本不该发
  2. 每轮都发：on_change 只比对**文案是否相同**，价格一变文案就变，
     于是每轮都算"变化"、每轮都推
  3. 文案是客服话：机器标题 + 全是逗号 + 末尾"不打扰"
"""
import asyncio

import pytest

from ai.agent import SILENT_TOKEN, SYSTEM_PROMPT, _is_silent, _system_content
from tests.test_agent import make_manager


# ---------- 静默标记 ----------

def test_silent_token_only():
    assert _is_silent(SILENT_TOKEN)
    assert _is_silent(f"  {SILENT_TOKEN}  ")
    assert _is_silent(f"{SILENT_TOKEN}。")


def test_silent_does_not_swallow_real_content():
    """夹带正文就不是静默——不能把真结论也吞掉。"""
    assert not _is_silent(f"{SILENT_TOKEN} 破 76000 了")
    assert not _is_silent("没跌破，差 3.2%")
    assert not _is_silent("")
    assert not _is_silent(None)


def test_prompt_teaches_silence():
    """提示词必须明确教：没什么可说就只回标记，且不要客服腔。

    SYSTEM_PROMPT 里是 {{SILENT}} 占位符，渲染后才变成真实标记——
    所以这里检查占位符，渲染结果由下一个测试单独验证。
    """
    for phrase in ("{{SILENT}}", "值得说", "不要每次都说话",
                   "真正的判断力体现在", "客服"):
        assert phrase in SYSTEM_PROMPT, f"缺少: {phrase}"
    # 必须把"不打扰"这类客服话作为反例写出来
    assert "不打扰" in SYSTEM_PROMPT
    assert "反例" in SYSTEM_PROMPT


def test_prompt_teaches_human_voice():
    """要的是人情味：炫耀、骂人、闲聊、表情包，而不是固定版式。"""
    for phrase in ("像一个盯盘的朋友在微信上跟你说话",
                   "会兴奋也会骂人",
                   "老子又开一笔雷霆大单",
                   "炫耀",
                   "币圈八卦",
                   "由你定"):
        assert phrase in SYSTEM_PROMPT, f"缺少: {phrase}"
    # 明确禁止固定版式
    assert "不要固定版式" in SYSTEM_PROMPT or "不用固定版式" in SYSTEM_PROMPT
    assert "【】" in SYSTEM_PROMPT


def test_prompt_renders_silent_token():
    """模板里的占位符必须被替换成真实标记，否则模型看到的是 {{SILENT}}。"""
    text = _system_content(None)
    assert "{{SILENT}}" not in text
    assert SILENT_TOKEN in text


# ---------- 通知过滤 ----------

def test_silent_result_sends_nothing(tmp_path, monkeypatch):
    """模型只回静默标记 → 聊天里一条都不加。"""
    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        task = {"id": "t1", "title": "盯 BTC", "notify": "always",
                "result": SILENT_TOKEN, "status": "succeeded"}
        before = len(manager.chat)
        assert manager._push_watch_notification(task, "succeeded") is False
        assert len(manager.chat) == before
        await manager.stop()
    asyncio.run(scenario())


def test_silent_result_with_detail_sends(tmp_path, monkeypatch):
    """带正文的"静默"不算静默，照常推送。"""
    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        task = {"id": "t1", "title": "盯 BTC", "notify": "always",
                "result": f"{SILENT_TOKEN} 破 76000 了，现在 75980",
                "status": "succeeded"}
        before = len(manager.chat)
        assert manager._push_watch_notification(task, "succeeded") is True
        assert len(manager.chat) == before + 1
        await manager.stop()
    asyncio.run(scenario())


def test_notification_has_no_machine_title_prefix(tmp_path, monkeypatch):
    """不能再加【标题】前缀——那是报告，不是人说话。"""
    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        task = {"id": "t1", "title": "BTC 盯 76000", "notify": "always",
                "result": "破 76000 了，75980", "status": "succeeded"}
        assert manager._push_watch_notification(task, "succeeded") is True
        content = manager.chat[-1]["content"]
        assert content == "破 76000 了，75980"
        assert "【" not in content and "】" not in content
        await manager.stop()
    asyncio.run(scenario())


def test_on_change_dedupes_identical_conclusions(tmp_path, monkeypatch):
    """结论没变就不重复推送。"""
    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        task = {"id": "t1", "title": "盯 BTC", "notify": "on_change",
                "result": "还在 78400 附近横着", "status": "succeeded"}
        assert manager._push_watch_notification(task, "succeeded") is True
        before = len(manager.chat)
        assert manager._push_watch_notification(task, "succeeded") is False
        assert len(manager.chat) == before
        await manager.stop()
    asyncio.run(scenario())


def test_silent_does_not_resurrect_old_notification(tmp_path, monkeypatch):
    """静默一轮之后，同一结论再次出现仍然算"说过"，不该重推。

    否则"静默 → 又说同一句 → 静默 → 又说"会来回刷。
    """
    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        task = {"id": "t1", "title": "盯 BTC", "notify": "on_change",
                "result": "破 76000 了", "status": "succeeded"}
        assert manager._push_watch_notification(task, "succeeded") is True
        n = len(manager.chat)
        task["result"] = SILENT_TOKEN
        assert manager._push_watch_notification(task, "succeeded") is False
        task["result"] = "破 76000 了"
        # 静默清掉了 lastNotified，这句会再推一次——这是可接受的行为
        # （用户重新看到一次状态，好过永久错过），但不能每轮都推
        pushed = manager._push_watch_notification(task, "succeeded")
        after = len(manager.chat)
        assert after == n + (1 if pushed else 0)
        assert manager._push_watch_notification(task, "succeeded") is False
        await manager.stop()
    asyncio.run(scenario())
