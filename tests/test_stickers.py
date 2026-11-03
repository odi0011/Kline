"""表情包：由 AI 决策，不是固定套路。

关键约束：
 · 表情包名字在前后端必须一致（后端写进提示词，前端负责画）
 · 一条回复最多一个
 · 只有已知名字才渲染，模型编一个不存在的名字不能变成空白气泡
"""
import re

from ai.agent import STICKER_NAMES, _system_content


def test_sticker_names_are_nonempty_and_unique():
    assert len(STICKER_NAMES) >= 3, "至少要有几个可选，否则等于没得挑"
    assert len(set(STICKER_NAMES)) == len(STICKER_NAMES), "名字不能重复"


def test_prompt_lists_every_sticker():
    text = _system_content("")
    for name in STICKER_NAMES:
        assert name in text


def test_prompt_says_when_it_must_send():
    """提示词必须给出"必须发"的场合。

    早先是「由你自己判断」——结果模型永远判断为不发，实测几乎从不触发。
    现在改成明确的场合清单（短情绪、骂人自嘲、明说要表情），
    并且服务端还有一层 _auto_sticker 兜底。
    """
    from ai.agent import SYSTEM_PROMPT
    assert "必须发的场合" in SYSTEM_PROMPT
    assert "直接发出去" in SYSTEM_PROMPT
    assert "一条回复最多一个" in SYSTEM_PROMPT


def test_prompt_gives_when_not_to_use():
    """认真问价位/仓位/风险时不该发表情包，带情绪词也不行。"""
    from ai.agent import SYSTEM_PROMPT
    assert "不发的场合" in SYSTEM_PROMPT
    assert "价位" in SYSTEM_PROMPT
    assert "还能加仓吗" in SYSTEM_PROMPT


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
    block = src.split("export const STICKERS")[1].split("export const STICKER_NAMES")[0]
    # 抓映射里的键：行首两空格 + 名字 + 冒号
    found = set(re.findall(r"\n  ([^\s:/]+)\s*:", block))
    backend = set(STICKER_NAMES)
    assert found == backend, f"前端多出 {found - backend}，后端多出 {backend - found}"


def test_every_sticker_file_exists():
    """每个表情都要有对应图片文件，不能指向一个 404。"""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1] / "web"
    path = root / "src/components/arc/chat-dock/stickers.jsx"
    if not path.exists():
        return
    src = path.read_text(encoding="utf-8")
    block = src.split("export const STICKERS")[1].split("export const STICKER_NAMES")[0]
    refs = re.findall(r'"(/stickers/[^"]+)"', block)
    assert refs, "没有找到任何图片路径"
    for ref in refs:
        target = root / "public" / ref.lstrip("/")
        assert target.exists(), f"表情图片不存在: {target}"
