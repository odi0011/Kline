"""看板数据不重复：每类数据只在一个权威位置展示。

线上实测的重复：
 · 同一个账户权益被画进**每张**K线卡的浮层里（4 个币 = 4 份相同数据）
 · 那个浮层第三个数字渲染的是 maxDD（最大回撤），标签却写成"峰值"，
   与账户卡里真正的最高权益 7366.08 对不上——同一个账户两个"峰值"
 · 回撤/CVaR 是账户级指标，不该挂在某个币的卡片里

这些用文本断言守住，避免改回去。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OVERLAY = ROOT / "web" / "src" / "components" / "arc" / "chart-overlay" / "chart-overlay.jsx"
STRATEGY = ROOT / "web" / "src" / "components" / "arc" / "strategy-panel" / "strategy-panel.jsx"
ACCOUNT = ROOT / "web" / "src" / "components" / "arc" / "account-card" / "account-card.jsx"


def _read(path):
    return path.read_text(encoding="utf-8")


def _code_only(path):
    """只取源码里的代码，去掉注释——注释里提到旧字段名不算违规。"""
    src = _read(path)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


def test_overlay_no_longer_draws_account_equity():
    """K线卡浮层不能再画账户权益——账户卡才是唯一权威位置。"""
    code = _code_only(OVERLAY)
    assert "EquityView" not in code, "K线浮层里还留着权益图"
    assert "权益与回撤" not in code, "浮层标题还写着权益"
    assert "s.bal" not in code, "还在读权益采样画曲线"


def test_overlay_shows_per_account_risk_instead():
    """改成画这张卡自己的风险画像（CVaR / 波动率 / 样本数）。"""
    code = _code_only(OVERLAY)
    assert "RiskView" in code
    for phrase in ("CVaR", "波动", "样本"):
        assert phrase in code, f"缺少: {phrase}"
    assert "样本不足" in code, "样本不足时必须明说，不能给一个假数字"


def test_overlay_peak_label_bug_is_gone():
    """原来把 maxDD（最大回撤）渲染成"峰值"，是标错。

    maxDD 的单位是百分比、含义是最大回撤；峰值应该是 USDT 金额。
    """
    code = _code_only(OVERLAY)
    assert "geo.maxDD" not in code, "峰值标签又绑回最大回撤了"
    assert "EquityView" not in code


def test_account_card_owns_equity_curve_and_heatmap():
    """权益曲线的权威位置是账户卡，必须仍然在。"""
    src = _read(ACCOUNT)
    assert "EquityCurve" in src
    assert "EquityHeatmap" in src


def test_strategy_panel_has_account_level_risk_strip():
    """回撤/CVaR 是账户级指标，要在币种宫格外面固定可见。"""
    code = _code_only(STRATEGY)
    assert "RiskStrip" in code
    for phrase in ("回撤", "CVaR95", "波动率", "熔断中", "样本不足"):
        assert phrase in code, f"风险条缺少: {phrase}"


def test_risk_strip_reads_drawdown_and_risk_from_analysis():
    code = _code_only(STRATEGY)
    assert "analysis?.drawdown" in code
    assert "analysis?.risk" in code


def test_advice_carries_score_basis():
    """评分来自哪根K线必须带出去，否则用户拿跳动的K线图对不动的分数。"""
    src = (ROOT / "decision" / "advice.py").read_text(encoding="utf-8")
    assert "basis" in src
    quant_src = (ROOT / "quant.py").read_text(encoding="utf-8")
    assert '"basis": "closedBar" if' in quant_src, "评分口径没标注"
    assert '"liveBar"' in quant_src, "回退口径没标注"
    # 评分必须基于已收盘K线，否则同一根内会被重算几十次
    assert "closed = [bar for bar in cs if getattr(bar, \"closed\", False)]" in quant_src
