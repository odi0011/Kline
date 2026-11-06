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
def test_card_keeps_its_own_width():
    """不能给权益卡加 width:100%——那会把卡拉宽、把仓位挤到无处可放。"""
    css = (ROOT / "web/src/components/arc/account-card/account-card.module.css").read_text(encoding="utf-8")
    body = _rule(css, ".card")
    assert "width: 100%" not in body, "账户卡不该撑满，拉宽会让右侧没有空间"


def test_position_stack_is_outside_the_card():
    """仓位渲染在独立组件里，不在账户卡内部。"""
    jsx = CARD_JSX.read_text(encoding="utf-8")
    assert "PositionCard" not in jsx, "账户卡内不该再渲染持仓卡"
    stack = ROOT / "web/src/components/arc/account-card/position-stack.jsx"
    assert stack.exists(), "缺少仓位竖排组件"
    src = stack.read_text(encoding="utf-8")
    css = (ROOT / "web/src/components/arc/account-card/position-stack.css").read_text(encoding="utf-8")
    assert "pos-stack" in src
    assert "flex-direction: column" in _rule(css, ".pos-stack")


def test_position_fields_are_not_simplified():
    """持仓卡必须保留 数量/开仓/强平/保证金 四项，一项都不许省。"""
    src = (ROOT / "web/src/components/arc/account-card/position-stack.jsx").read_text(encoding="utf-8")
    for field in ("数量", "开仓", "强平", "保证金"):
        assert f'"{field}"' in src, f"持仓卡丢了字段 {field}"


def test_closed_positions_are_tagged_closed():
    """已平仓标签写「已平」，不用多/空。"""
    src = (ROOT / "web/src/components/arc/account-card/position-stack.jsx").read_text(encoding="utf-8")
    assert "已平" in src
    assert "stack-tag-closed" in src


def test_stack_height_is_capped_by_row():
    """竖排栏高度 = 首行高度，超出内部滚动，不能撑高整行。"""
    css = (ROOT / "web/src/components/arc/account-card/position-stack.css").read_text(encoding="utf-8")
    body = _rule(css, ".pos-stack")
    assert "height: 100%" in body
    assert "overflow-y: auto" in body


def test_app_renders_stack_beside_card():
    app = (ROOT / "web/src/App.jsx").read_text(encoding="utf-8")
    assert "PositionStack" in app
    assert "positions={account?.positions" in app
