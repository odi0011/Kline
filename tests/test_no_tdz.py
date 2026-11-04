"""静态检查：不能在声明之前使用 useMemo/useState 变量（TDZ 白屏）。

线上事故：把 useEffect(() => setProperty('--up', chartColors.up)) 写在了
`const chartColors = useMemo(...)` **之前**，构建能过（ESM 提升），
运行时直接抛 "Cannot access 'chartColors' before initialization"，
整个页面白屏。JS 不会报编译错，只有真的打开页面才看得见。

这里用简单的位置比较兜住最常见的一类：App.jsx 里顶层 const 声明
必须在所有使用点之前。
"""
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[1]
APP = (REPO / "web/src/App.jsx").read_text(encoding="utf-8")


def _declared_consts(src: str) -> dict[str, int]:
    """顶层 hook 声明：const xxx = useMemo/useState/useRef/useCallback(...)"""
    out = {}
    for m in re.finditer(r"^\s*const (\w+) = (useMemo|useState|useRef|useCallback|useEffect)\b",
                         src, re.M):
        out.setdefault(m.group(1), m.start())
    return out


def test_hooks_declared_before_use():
    """每个 useMemo 变量的首次使用必须晚于它的声明。"""
    consts = _declared_consts(APP)
    assert consts, "没解析到任何声明，检查一下正则"
    problems = []
    for name, decl_at in consts.items():
        # 找第一次出现（跳过声明本身）
        for m in re.finditer(rf"\b{re.escape(name)}\b", APP):
            if m.start() <= decl_at:
                continue
            if m.start() < decl_at:
                continue
            break
        # 反过来：查有没有在声明之前就出现
        first = APP.find(name)
        if first < decl_at:
            problems.append(f"{name} 在声明前被引用（{first} < {decl_at}）")
    assert not problems, "；".join(problems)


def test_chart_colors_effect_after_memo():
    """chartColors 那个 effect 必须在 useMemo 之后（事故原样回归）。"""
    memo_at = APP.find("const chartColors = useMemo")
    effect_at = APP.find("setProperty('--up', chartColors.up)")
    assert memo_at > 0 and effect_at > 0
    assert effect_at > memo_at, (
        "涨跌色同步的 useEffect 写在了 chartColors 声明之前——"
        "会抛 Cannot access 'chartColors' before initialization，页面白屏")
