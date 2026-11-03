"""交易员口吻：默认短答，只有用户明确要求分析才展开。

提示词是软约束，模型经常照样写一大段，所以服务端还有一道硬闸：
_wants_detail() 判定用户是否点名要分析，没点名就只留第一句并裁到 60 字。
"""
import asyncio

from ai.agent import SHORT_REPLY_CHARS, _wants_detail, split_reply


def test_default_is_short():
    """没要求分析时只留第一句。"""
    text = "空。79500 止损，77800 止盈，20 倍。评分 61.5，RSI 偏高。结构还没转。"
    assert split_reply(text, detailed=False) == ["空。"]


def test_long_first_sentence_is_trimmed():
    """第一句本身就很长时也要裁掉，不能靠"只用第一句"就放过一整段。"""
    text = "我先说明一下当前的市场环境然后再说结论：BTC 在四小时级别上已经跌破了关键的位置" + "，很多细节" * 20 + "。"
    out = split_reply(text, detailed=False)
    assert len(out) == 1
    assert len(out[0]) <= SHORT_REPLY_CHARS + 1, out
    assert out[0].endswith("。"), "截断后要补句号，不能半句话结尾"


def test_detail_request_keeps_structure():
    """用户明确要分析时，保留原有的多条切分。"""
    text = "第一句。第二句。第三句。第四句。"
    out = split_reply(text, detailed=True)
    assert len(out) == 2
    assert out[0] == "第一句。第二句。第三句。"


def test_short_mode_respects_explicit_bubbles():
    """短答模式下模型主动用 ||| 分段 → 尊重它的分段，每段一个气泡。

    这条规则反转过一次：早先短答模式无条件只取第一段，模型想分两条
    也发不出来，用户看到的永远是单气泡（"回复多个气泡"做不出来）。
    分段是模型判断"这个场景该分几条"的结果，服务端只做长度保护。
    """
    text = "空。|||因为评分偏高。"
    assert split_reply(text, detailed=False) == ["空。", "因为评分偏高。"]


def test_short_mode_still_trims_when_no_split():
    """没分段时仍然是"只留第一句"：防模型无视提示词写一整段。"""
    text = "空。因为评分偏高。还有别的理由。"
    assert split_reply(text, detailed=False) == ["空。"]


def test_bubble_count_is_capped_by_prompt_not_code():
    """代码不硬砍气泡数量，但每个气泡要单独满足长度保护。"""
    text = "第一句。" + "长" * 80 + "。|||第二句。"
    out = split_reply(text, detailed=False)
    assert len(out) == 2
    assert len(out[0]) <= SHORT_REPLY_CHARS + 1, out
    assert out[1] == "第二句。"


def test_empty_reply_stays_empty():
    assert split_reply("", detailed=False) == []
    assert split_reply(None, detailed=False) == []


def test_wants_detail_detects_explicit_requests():
    for prompt in ("分析一下BTC", "讲讲逻辑", "为什么看空", "详细说说", "给我理由",
                   "复盘一下这单", "explain your reasoning", "why short"):
        assert _wants_detail(prompt), f"应识别为需要展开: {prompt}"


def test_plain_questions_are_short():
    """普通问句不该触发展开。"""
    for prompt in ("BTC现在能做吗", "你是谁", "系统建议啥", "仓位多少",
                   "开多少", "哪个币值得开单", "你好"):
        assert not _wants_detail(prompt), f"不该当成分析请求: {prompt}"


def test_identity_answer_is_not_trimmed():
    """问"你是谁"时不裁：回答本身很短，裁了会丢模型名。"""
    raw = "ODDD，盘面分析。底层是 claude-opus-5.5。"
    assert split_reply(raw, detailed=False, identity=True) == [raw]


def test_identity_question_is_detected():
    from ai.agent import _IDENTITY_QUESTION
    for q in ("你是谁", "你什么模型", "你啥模型啊", "which model are you"):
        assert _IDENTITY_QUESTION.search(q), q
    for q in ("BTC能做吗", "系统建议啥"):
        assert not _IDENTITY_QUESTION.search(q), q


def test_tool_markup_is_detected():
    """模型把工具调用写成正文时，不能当回答发出。"""
    from ai.agent import _is_tool_markup
    for bad in ('<｜｜DSML｜｜ calls>\n<｜｜DSML｜｜ invoke name="read_candles">',
                '<tool_call>{"name": "read_advice"}</tool_call>',
                '<|tool_calls|>', '<function_calls>'):
        assert _is_tool_markup(bad), bad
    for good in ("空。79500 止损。", "我是 ODDD，盘面分析。", "RSI 偏高，注意回落。"):
        assert not _is_tool_markup(good), good


def test_tool_markup_reply_is_not_sent_to_user(tmp_path, monkeypatch):
    """端到端：模型把调用写成文本时，不能把这堆尖括号发给用户。"""
    from tests.test_agent import make_manager, wait_for, tool_call, tool_result

    calls = {"n": 0}

    def provider(*args, **kwargs):
        calls["n"] += 1
        if kwargs.get("tool_choice") == "required":
            return tool_result(tool_call())
        return {"ok": True,
                "reply": '<｜｜DSML｜｜ calls>\n<｜｜DSML｜｜ invoke name="read_candles">',
                "usage": {"total_tokens": 10}, "model": "test-model"}

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch, provider=provider)
        await manager.ask("BTC能做吗", "test-model")
        assert await wait_for(lambda: not manager.chat_runners)
        answer = [m for m in manager.chat if m.get("role") == "assistant"][-1]
        assert "DSML" not in (answer.get("content") or ""), \
            f"工具标记被当成回答发出去了: {answer.get('content')!r}"
        await manager.stop()
    asyncio.run(scenario())


def test_prompt_forbids_filler():
    """提示词要明确列出禁止的废话模式，并给出正反例。"""
    from ai.agent import SYSTEM_PROMPT
    for rule in ("一句话，40 字以内", "短答是默认", "需要我查点什么吗",
                 "复述用户的问题", "自我介绍"):
        assert rule in SYSTEM_PROMPT, f"缺少约束: {rule}"


def test_chat_turn_uses_short_reply(tmp_path, monkeypatch):
    """端到端：普通提问时，模型写了一大段也只落一条短消息。"""
    from tests.test_agent import make_manager, wait_for

    long_reply = "空。79500 止损。评分 61.5，RSI 偏高。结构还没转。还有很多别的细节。"

    def provider(*args, **kwargs):
        if kwargs.get("tool_choice") == "required":
            from tests.test_agent import tool_call, tool_result
            return tool_result(tool_call())
        return {"ok": True, "reply": long_reply, "usage": {"total_tokens": 10},
                "model": "test-model"}

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch, provider=provider)
        await manager.ask("BTC现在能做吗", "test-model")
        assert await wait_for(lambda: not manager.chat_runners)
        answers = [m for m in manager.chat if m.get("role") == "assistant" and m.get("content")]
        assert len(answers) == 1, f"短答不该被切成多条: {answers!r}"
        assert answers[0]["content"] == "空。"
        await manager.stop()
    asyncio.run(scenario())


def test_analysis_request_keeps_multiple_bubbles(tmp_path, monkeypatch):
    """用户点名要分析时，仍然允许多条。"""
    from tests.test_agent import make_manager, wait_for

    long_reply = "第一点。第二点。第三点。第四点。"

    def provider(*args, **kwargs):
        if kwargs.get("tool_choice") == "required":
            from tests.test_agent import tool_call, tool_result
            return tool_result(tool_call())
        return {"ok": True, "reply": long_reply, "usage": {"total_tokens": 10},
                "model": "test-model"}

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch, provider=provider)
        await manager.ask("分析一下这个结构", "test-model")
        assert await wait_for(lambda: not manager.chat_runners)
        answers = [m for m in manager.chat if m.get("role") == "assistant" and m.get("content")]
        assert len(answers) == 2, f"明确要求分析时应保留多条: {answers!r}"
        await manager.stop()
    asyncio.run(scenario())


def test_model_split_becomes_multiple_messages(tmp_path, monkeypatch):
    """端到端：模型用 ||| 分段时，服务端要落成多条消息（多气泡）。"""
    from tests.test_agent import make_manager, wait_for

    def provider(*args, **kwargs):
        if kwargs.get("tool_choice") == "required":
            from tests.test_agent import tool_call, tool_result
            return tool_result(tool_call())
        return {"ok": True, "reply": "能，空。|||79500 止损，20 倍。",
                "usage": {"total_tokens": 10}, "model": "test-model"}

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch, provider=provider)
        await manager.ask("BTC 能做吗", "test-model")
        assert await wait_for(lambda: not manager.chat_runners)
        answers = [m for m in manager.chat
                   if m.get("role") == "assistant" and m.get("content")]
        assert len(answers) == 2, f"应落成两条消息: {answers!r}"
        assert answers[0]["content"] == "能，空。"
        assert answers[1]["content"] == "79500 止损，20 倍。"
        # 第二条必须能和第一条归到同一轮（前端按 id 前缀分组）
        base = answers[0]["id"]
        assert answers[1]["id"].startswith(base + "-")
        await manager.stop()

    asyncio.run(scenario())


def test_prompt_teaches_when_to_split():
    """提示词要教模型什么时候分多条气泡。"""
    from ai.agent import SYSTEM_PROMPT
    assert "分多条气泡" in SYSTEM_PROMPT
    assert "最多 3 个气泡" in SYSTEM_PROMPT
