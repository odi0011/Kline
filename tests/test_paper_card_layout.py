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


def test_two_badges_estimated_and_total():
    """右上角两枚徽标：这一仓的估算胜率 + 这个币累计实际胜率。

    两者是不同口径，必须分开、并各自标注清楚：
      估XX% = 前瞻概率（SL/TP 距离 + 信号分算出来的）
      胜XX% = 后验实际值（这个币历史上平掉的单子里赢了多少）
    """
    assert "估{winPct" in JSX, "第一枚应为 估XX%"
    assert "胜{totalPct" in JSX, "第二枚应为 胜XX%"
    # 数据源不同：估算来自 pos.winrate，累计来自 paperBySymbol
    assert "pos.winrate" in JSX
    assert "total?.[pos.symbol]" in JSX
    block = re.search(r"\.badges \{([^}]*)\}", CSS, re.S)
    assert block, "缺少 .badges 容器样式"
    assert "right:" in block.group(1) and "top:" in block.group(1)
    assert "display: flex" in block.group(1)


def test_estimated_badge_matches_pattern_badge():
    """「估」徽标三档要和形态卡片 .win-badge 用同一套 --accent 规则。"""
    for tier in ("low", "mid", "hi"):
        pat = re.search(rf"\.win-badge\.{tier} \{{([^}}]*)\}}", STYLES)
        pap = re.search(rf"\.winrate(?:\.actual)?\.{tier}\s*\{{([^}}]*)\}}", CSS)
        assert pat and pap, f"{tier} 档缺失"
        p = re.search(r"var\(--accent\)\s*(\d+)%", pat.group(1))
        q = re.search(r"var\(--accent\)\s*(\d+)%", pap.group(1))
        assert p and q and p.group(1) == q.group(1), \
            f"{tier} 档色值不一致: 形态={p and p.group(1)} 模拟仓={q and q.group(1)}"


def test_actual_winrate_badge_uses_green_family():
    """「胜」徽标必须用绿色系，不能和「估」同色。

    两枚并排放在右上角，同色系会分不清哪个是哪个：
      估 = 主题色（--accent，金）
      胜 = 绿色系（--up 派生），深浅表示胜率高低
    """
    for tier in ("low", "mid", "hi"):
        block = re.search(rf"\.winrate\.actual\.{tier} \{{([^}}]*)\}}", CSS)
        assert block, f"缺少 .winrate.actual.{tier} 的绿色样式"
        body = block.group(1)
        assert "var(--up)" in body, f"{tier} 档没有用 --up 绿系"
        assert "var(--accent)" not in body, f"{tier} 档不该再用主题色"
    # JSX 里第二枚要带 actual 修饰类
    assert "styles.actual" in JSX


def test_winrate_tier_thresholds_match_pattern_cards():
    """分档阈值与形态卡片一致：>=50 hi、>=10 mid、其余 low。"""
    # 模拟仓用 tierOf() 统一分档（两枚徽标都走它）
    assert "pct >= 50 ? \"hi\"" in JSX
    assert "pct >= 10 ? \"mid\"" in JSX
    assert "const tierOf" in JSX
    # 形态卡片那边是同样的阈值
    app = (REPO / "web/src/App.jsx").read_text(encoding="utf-8")
    assert "winPct >= 50 ? 'hi'" in app
    assert "winPct >= 10 ? 'mid'" in app


def test_no_extra_glass_on_badge():
    """徽标不要自造玻璃底（backdrop-filter）。"""
    block = re.search(r"\.winrate \{([^}]*)\}", CSS, re.S)
    assert "backdrop-filter" not in block.group(1)
