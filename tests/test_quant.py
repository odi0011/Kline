"""量化引擎单元测试（离线：交易所调用全部 mock）"""
from unittest.mock import MagicMock

import pytest

from quant import QuantEngine

KEY = "k"
SECRET = "s"


@pytest.fixture
def engine(tmp_path):
    accts = {"list": [{"name": "ODDD测试盘1", "api_key": KEY, "api_secret": SECRET, "icon": ""}]}
    eng = QuantEngine(
        accts, str(tmp_path / "quant.yaml"), price_fn=lambda s: 100.0,
        min_pattern_winrate=0.35, min_pattern_samples=5,
    )
    eng._step_size = MagicMock(return_value=0.001)
    eng._available = MagicMock(return_value=500.0)
    eng._place_order = MagicMock(return_value={"orderId": 1, "avgPrice": "100.0"})
    eng._broadcast_positions = MagicMock()
    return eng


def _hit(pattern="hammer", direction="bullish"):
    return {"symbol": "btcusdt", "direction": direction, "pattern": pattern,
            "name_zh": "测试形态", "price": 100.0}


def test_quality_gate_blocks_low_winrate(engine):
    """历史胜率 17%（1/6，样本充足）< 35% → 质量门拦截，不下单"""
    engine.pattern_stats_fn = lambda: {"hammer": {"count": 6, "wins": 1, "losses": 5}}
    engine.on_signal(_hit("hammer"))
    engine._place_order.assert_not_called()
    assert "btcusdt" not in engine._st("ODDD测试盘1")["positions"]


def test_quality_gate_passes_good_pattern(engine):
    """历史胜率 83% ≥ 35% → 正常开仓"""
    engine.pattern_stats_fn = lambda: {"hammer": {"count": 6, "wins": 5, "losses": 1}}
    engine.on_signal(_hit("hammer"))
    engine._place_order.assert_called_once()
    assert "btcusdt" in engine._st("ODDD测试盘1")["positions"]


def test_undersampled_pattern_not_blocked(engine):
    """样本只有 2 个（< min_pattern_samples）→ 质量门不拦截"""
    engine.pattern_stats_fn = lambda: {"hammer": {"count": 2, "wins": 0, "losses": 2}}
    engine.on_signal(_hit("hammer"))
    engine._place_order.assert_called_once()


def test_no_stats_fn_trades_freely(engine):
    """未接入形态统计 → 行为与旧版一致，直接开仓"""
    engine.on_signal(_hit("hammer"))
    engine._place_order.assert_called_once()


def test_neutral_pattern_skipped(engine):
    engine.on_signal(_hit("doji", direction="neutral"))
    engine._place_order.assert_not_called()


def test_duplicate_position_skipped(engine):
    st = engine._st("ODDD测试盘1")
    st["positions"]["btcusdt"] = {"symbol": "btcusdt", "side": "BUY", "qty": 0.25}
    engine.on_signal(_hit("hammer"))
    engine._place_order.assert_not_called()


def test_serialize_keeps_names():
    """串行化装饰器不应丢失方法名（可测试性）"""
    assert QuantEngine.on_signal.__name__ == "on_signal"
    assert QuantEngine.manage.__name__ == "manage"
    assert QuantEngine.audit.__name__ == "audit"
