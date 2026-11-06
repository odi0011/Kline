"""账户卡在有仓位时的高度与横向布局。

线上原话："这部分样式都错乱了，高度整行应该是统一的，现在有了仓位后
高度就超了啊，然后右侧还空了小部分"。

三个具体缺陷（都用 CDP 量过真实 DOM）：
  1. 持仓条写死 84px + 概览 110px → 整卡 284px，无仓位是 208px，差 76；
  2. 持仓卡固定 124px 宽 → 两张 248px 塞不进 208px 可用宽度，
     第二张被裁（截图里 XRP 的 "3x" 是断的）；
  3. 有仓位时权益曲线 46px → 头部 38px，无仓位时头部 28px。

修完实测：无仓位 208、有仓位 212（差 4），两张卡各 113px 完整可见。

这组测试读 CSS/JSX 源码做约束——不引入浏览器依赖，跑得快；
真正的视觉验收仍然用 tests/account-card.probe.mjs 量线上 DOM。
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
CARD_CSS = ROOT / "web/src/components/arc/account-card/account-card.module.css"
OV_CSS = ROOT / "web/src/components/arc/account-card/market-overview.module.css"
CARD_JSX = ROOT / "web/src/components/arc/account-card/account-card.jsx"


def _rule(css: str, selector: str) -> str:
    """取出某条规则的声明块（不处理嵌套，够用）。"""
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    return match.group(1) if match else ""


def test_split_area_has_no_fixed_min_height():
    """.posArea.split 不能再给一个"两块都完整显示"的最小高度。

    线上那行是 `min-height: 178px`，它把整卡顶到 284。
    """
    css = CARD_CSS.read_text(encoding="utf-8")
    body = _rule(css, ".posArea.split")
    assert body, "找不到 .posArea.split 规则"
    assert "min-height: 178px" not in body
    assert "min-height: 0" in body


def test_position_strip_is_compact():
    """持仓条要压到 48px 左右，否则和概览叠不下。"""
    css = CARD_CSS.read_text(encoding="utf-8")
    scroll = _rule(css, ".posArea.split .posScroll")
    height = re.search(r"height:\s*(\d+)px", scroll)
    assert height, scroll
    assert int(height.group(1)) <= 50, f"持仓条 {height.group(1)}px 太高"


def test_position_cards_fill_the_row_exactly():
    """两张卡必须各占半行，不能用固定宽度。

    固定 124px × 2 = 248 > 208 可用宽度 → 第二张被裁。
    同一条选择器在多处出现（padding 一处、宽度一处），必须全部收集。
    """
    css = CARD_CSS.read_text(encoding="utf-8")
    bodies = re.findall(r"\.posArea\.split \.pcard\s*\{([^}]*)\}", css)
    joined = " ".join(bodies)
    assert "calc(50%" in joined, joined
    assert "124px" not in joined


def test_curve_shrinks_when_positions_exist():
    """有仓位时曲线也收成 28px，让头部高度和无仓位一致。"""
    css = CARD_CSS.read_text(encoding="utf-8")
    assert ".card[data-has-position] .curve" in css
    jsx = CARD_JSX.read_text(encoding="utf-8")
    assert "data-has-position" in jsx
    assert "showHistory || positions.length > 0 ? 28 : 46" in jsx


def test_overview_compacts_when_positions_exist():
    """有仓位时概览要收紧，否则整卡还是高出 20+px。"""
    css = OV_CSS.read_text(encoding="utf-8")
    for selector in (".wrap[data-has-position] .block",
                     ".wrap[data-has-position] .statValue",
                     ".wrap[data-has-position] .windows"):
        assert selector in css, f"缺少压缩规则 {selector}"


def test_position_row_hiding_keeps_quantity():
    """只保留"数量"这一行——它是持仓卡里唯一不可省的明细。"""
    css = CARD_CSS.read_text(encoding="utf-8")
    assert ".posArea.split .prow:nth-child(n + 2) { display: none; }" in css
    jsx = CARD_JSX.read_text(encoding="utf-8")
    # 数量必须写在 prows 里、且排在循环渲染的明细行之前
    block = jsx.split('className={styles.prows}')[1].split("{rows.map")[0]
    assert "数量" in block, block[:200]

