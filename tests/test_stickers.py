"""表情包：由 AI 决策，不是固定套路。

关键约束：
 · 表情包名字在前后端必须一致（后端写进提示词，前端负责画）
 · 一条回复最多一个
 · 只有已知名字才渲染，模型编一个不存在的名字不能变成空白气泡
"""
import re

from ai.agent import STICKER_NAMES, _system_content


def test_sticker_names_are_nonempty_and_unique():
    assert len(STICKER_NAMES) >= 4
    assert len(set(STICKER_NAMES)) == len(STICKER_NAMES), "名字不能重复"


def test_prompt_lists_every_sticker():
    text = _system_content("")
    for name in STICKER_NAMES:
        assert name in text


def test_prompt_says_ai_decides_not_fixed():
    """提示词必须明确"由你自己判断"，否则模型会每句都带表情包。"""
    from ai.agent import SYSTEM_PROMPT
    assert "由你自己判断" in SYSTEM_PROMPT
    assert "不要每次都发" in SYSTEM_PROMPT
    assert "一条回复最多一个" in SYSTEM_PROMPT


def test_prompt_gives_when_not_to_use():
    """认真问价位、有持仓风险时不该发表情包。"""
    from ai.agent import SYSTEM_PROMPT
    assert "不适合发" in SYSTEM_PROMPT
    assert "价位" in SYSTEM_PROMPT


def test_prompt_allows_sticker_only_reply():
    """允许只发表情包不说话。"""
    from ai.agent import SYSTEM_PROMPT
    assert "只发表情包" in SYSTEM_PROMPT


def test_sticker_syntax_is_documented():
    """要用 [sticker:名字] 这种可解析的写法，不能是自由文本。"""
    from ai.agent import SYSTEM_PROMPT
    assert "[sticker:" in SYSTEM_PROMPT


def test_frontend_names_match_backend():
    """前后端名字必须一致。

    这里读前端源码做对比——两边手工维护，最容易在这里出岔。
    """
    import pathlib
    path = pathlib.Path(__file__).resolve().parents[1] / "web/src/components/arc/chat-dock/stickers.jsx"
    if not path.exists():
        return          # 前端目录不在时跳过（例如只跑后端）
    src = path.read_text(encoding="utf-8")
    # 抓 STICKERS 对象里的键：行首两空格 + 中文名 + 冒号
    found = set(re.findall(r"\n  ([^\s:：]+)\s*:", src.split("export const STICKERS")[1].split("export const STICKER_NAMES")[0]))
    backend = set(STICKER_NAMES)
    assert found == backend, f"前端多出 {found - backend}，后端多出 {backend - found}"
