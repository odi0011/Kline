"""模拟仓卡片顶部：左上刷新、右上胜率徽标、没有多余的"模拟"标。

用户要求：
 · 刷新按钮挪到左上角
 · 右上角放该币全部仓位的总预估胜率徽标
 · 配色跟 K 线形态卡片的「胜XX%」一致（同一套 --accent 三档），不要自造的玻璃底
 · 左上角"模拟"两个字删掉（卡片已经在模拟仓板块里，重复标注没意义）
"""
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[1]
JSX = (REPO / "web/src/components/arc/paper-trade/paper-trade.jsx").read_text(encoding="utf-8")
CSS = (REPO / "web/src/components/arc/paper-trade/paper-trade.module.css").read_text(encoding="utf-8")
STYLES = (REPO / "web/src/styles.css").read_text(encoding="utf-8")


def test_simulated_label_removed():
    assert "paperTag" not in JSX, "「模拟」字标应已删除"
    assert "paperTag" not in CSS, "对应的样式也要删掉"
    assert "模拟" not in re.sub(r"//.*|/\*.*?\*/", "", JSX, flags=re.S), \
        "卡片正文里不该再渲染「模拟」字样"


def test_refresh_is_top_left():
    """刷新钮必须定位在左上（top+left），不是跟随标题栏。"""
    block = re.search(r"\.refresh \{([^}]*)\}", CSS, re.S)
    assert block, "缺少 .refresh 样式"
    body = block.group(1)
    assert "left:" in body and "top:" in body, body
    assert "margin-left: auto" not in body, "不该再用 margin-auto 挤到右边"
    # JSX 里刷新钮要在图表区（chartWrap）内、胜率徽标之前
    chart = JSX[JSX.index("chartWrap"):JSX.index("styles.head")]
    assert "onRefresh" in chart, "刷新钮应在图表区（左上）"


def test_winrate_badge_top_right_and_labelled_sheng():
    """右上角徽标写「胜XX%」，不是「估XX%」。"""
    assert "胜{pos.winrate" in JSX, "徽标文案应为 胜XX%"
    assert "估 {pos.winrate" not in JSX
    block = re.search(r"\.winrate \{([^}]*)\}", CSS, re.S)
    assert block and "right:" in block.group(1) and "top:" in block.group(1)


def test_winrate_colours_match_pattern_badge():
    """三档配色要和 styles.css 里的 .win-badge 用同一套 color-mix 规则。"""
    for tier in ("low", "mid", "hi"):
        pat = re.search(rf"\.win-badge\.{tier} \{{([^}}]*)\}}", STYLES)
        pap = re.search(rf"\.winrate\.{tier} \{{([^}}]*)\}}", CSS)
        assert pat and pap, f"{tier} 档缺失"
        # 两边的 background 必须都是同一种 percent 的 --accent 派生
        p = re.search(r"var\(--accent\)\s*(\d+)%", pat.group(1))
        q = re.search(r"var\(--accent\)\s*(\d+)%", pap.group(1))
        assert p and q and p.group(1) == q.group(1), \
            f"{tier} 档色值不一致: 形态={p and p.group(1)} 模拟仓={q and q.group(1)}"


def test_winrate_tier_thresholds_match_pattern_cards():
    """分档阈值与形态卡片一致：>=50 hi、>=10 mid、其余 low。"""
    assert "winPct >= 50 ? \"hi\"" in JSX
    assert "winPct >= 10 ? \"mid\"" in JSX
    app = (REPO / "web/src/App.jsx").read_text(encoding="utf-8")
    assert "winPct >= 50 ? 'hi'" in app
    assert "winPct >= 10 ? 'mid'" in app


def test_no_extra_glass_on_badge():
    """徽标不要自造玻璃底（backdrop-filter）。"""
    block = re.search(r"\.winrate \{([^}]*)\}", CSS, re.S)
    assert "backdrop-filter" not in block.group(1)
