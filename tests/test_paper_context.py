"""端到端：QuantEngine 快照里的模拟仓能被 read_paper 读到。

这是用户报的那个 bug 的回归：Agent 拿不到模拟仓，只好把实盘账户名/
实盘成交当成模拟仓回答。这里从引擎快照一路走到工具输出。
"""
import time
from unittest.mock import MagicMock

from ai.agent_tools import project_paper_positions, read_snapshot
from decision.config import DecisionConfig
from decision.paper import PaperBook
from quant import QuantEngine


def _engine(tmp_path):
    accts = {"list": [{"name": "ODDD测试盘1", "api_key": "k", "api_secret": "s", "icon": ""}]}
    eng = QuantEngine(
        accts, str(tmp_path / "quant.yaml"), price_fn=lambda s: 100.0,
        min_pattern_winrate=0.35, min_pattern_samples=5,
        decision_config=DecisionConfig(scoring_enabled=False, watch_enabled=False,
                                       exchange_protection=False),
    )
    eng._step_size = MagicMock(return_value=0.001)
    eng._available = MagicMock(return_value=500.0)
    eng._balance_snapshot = MagicMock(return_value=(500.0, 500.0))
    eng._place_order = MagicMock(return_value={"orderId": 1, "avgPrice": "100.0"})
    eng._broadcast_positions = MagicMock()
    return eng


def _ctx(price=100.0):
    return {"price": price, "atr": 2.0, "score": 70, "gap": 20, "grade": "medium",
            "direction": "bullish", "reason": "测试", "regime": "trend",
            "pattern": "", "patternStats": {}, "maxLeverage": 20}


def test_engine_snapshot_paper_is_projectable(tmp_path):
    """引擎快照的 analysis.paper / paperStats / paperHistory 三块都要在。"""
    eng = _engine(tmp_path)
    eng.bars_fn = lambda sym: []
    eng.paper = PaperBook()
    eng.paper.tick("btcusdt", _ctx())
    eng.paper.tick("btcusdt", _ctx(price=eng.paper.positions["btcusdt"].tp + 1))
    eng.symbol_filter = {"btcusdt"}

    snap = eng.snapshot()
    analysis = snap["analysis"]
    assert analysis["paper"], "未平仓模拟仓要出现在快照里"
    assert analysis["paper"][0]["symbol"] == "btcusdt"
    assert analysis["paperStats"]["opened"] >= 2
    assert "bySymbol" in eng.paper.summary()

    # 走 main.py 用的同一份投射函数，再走 read_paper
    rows = project_paper_positions(analysis["paper"])
    assert "candles" not in rows[0], "context 不该带画图用的K线"
    out = read_snapshot("read_paper", {}, {
        "ts": int(time.time() * 1000), "paper": rows,
        "paperStats": analysis["paperStats"],
        "paperHistory": analysis["paperHistory"],
        "paperSummary": eng.paper.summary(),
    })
    data = out["data"]
    assert out["available"] is True
    assert data["openPositions"][0]["symbol"] == "btcusdt"
    assert data["realized"]["bySymbol"][0]["symbol"] == "btcusdt"
    assert data["realized"]["bySymbol"][0]["closed"] == 1
