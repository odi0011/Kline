"""量化引擎单元测试（离线：交易所调用全部 mock）"""
from unittest.mock import MagicMock

import pytest

from decision.config import DecisionConfig
from decision.risk import RiskEngine
from core.candle import Candle
from quant import QuantEngine

KEY = "k"
SECRET = "s"


@pytest.fixture
def engine(tmp_path):
    accts = {"list": [{"name": "ODDD测试盘1", "api_key": KEY, "api_secret": SECRET, "icon": ""}]}
    eng = QuantEngine(
        accts, str(tmp_path / "quant.yaml"), price_fn=lambda s: 100.0,
        min_pattern_winrate=0.35, min_pattern_samples=5,
        decision_config=DecisionConfig(allow_long=True, quality_enabled=False,
            scoring_enabled=False, watch_enabled=False,
            exchange_protection=False,
        ),
    )
    eng._step_size = MagicMock(return_value=0.001)
    eng._available = MagicMock(return_value=500.0)
    eng._balance_snapshot = MagicMock(return_value=(500.0, 500.0))
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


def test_watch_and_risk_engine_open_path(engine):
    def trend(n, step, interval):
        out = []
        for i in range(n):
            close = 100 + i * step
            out.append(Candle(i * 60_000, close - step, close + 0.2,
                              close - 0.2, close, volume=100 + i,
                              interval=interval, closed=True))
        return out

    entry = trend(80, 0.05, "3m")
    h1 = trend(120, 0.35, "1h")
    h4 = trend(120, 1.2, "4h")
    acct = engine.accts_box["list"][0]
    engine.decision_cfg = DecisionConfig(allow_long=True, quality_enabled=False,
        scoring_enabled=False, watch_enabled=True, exchange_protection=False,
    )
    engine.risk = RiskEngine(engine.decision_cfg)
    engine.bars_fn = lambda symbol: entry
    engine.watch_bars_fn = lambda symbol: (entry, h1, h4)
    engine.data_health_fn = lambda symbol=None: True
    engine.indicators["btcusdt"] = {
        "atr": 0.6, "atrPct": 50, "regime": "trend", "rsi": 60,
        "adx": 30, "plusDI": 28, "minusDI": 10,
    }
    hit = {**_hit("hammer"), "geometry": {"neck": 98.0}}
    engine._decide_open(
        acct["name"], acct, "btcusdt", "BUY", "bullish", 100.0, hit,
        features={}, signal_score=None, bars=entry,
    )
    engine._place_order.assert_called_once()
    pos = engine._st(acct["name"])["positions"]["btcusdt"]
    assert pos["sl"] < pos["entry"] < pos["tp"]
    assert pos["watchPlan"]["h4"]["direction"] == "bullish"
    assert pos["invalidation"] < pos["entry"]


def test_scored_signal_path_uses_confluence_gate(engine):
    entry = []
    for i in range(80):
        close = 100 + i * 0.05
        entry.append(Candle(i * 60_000, close, close + 0.2, close - 0.2,
                            close, volume=100 + i, interval="3m", closed=True))
    h4 = []
    for i in range(120):
        close = 100 + i * 1.2
        h4.append(Candle(i * 60_000, close, close + 0.2, close - 0.2,
                         close, volume=100 + i, interval="4h", closed=True))
    acct = engine.accts_box["list"][0]
    engine.decision_cfg = DecisionConfig(allow_long=True, quality_enabled=False,
        scoring_enabled=True, watch_enabled=True, exchange_protection=False,
    )
    engine.risk = RiskEngine(engine.decision_cfg)
    engine.bars_fn = lambda symbol: entry
    engine.watch_bars_fn = lambda symbol: (entry, entry, h4)
    engine.data_health_fn = lambda symbol=None: True
    engine.indicators["btcusdt"] = {
        "atr": 0.6, "atrPct": 50, "regime": "trend", "rsi": 61,
        "adx": 32, "plusDI": 30, "minusDI": 10, "z": 0.5, "roc": 0.01,
    }
    engine._build_features = MagicMock(return_value={
        "macd": {"hist": 0.3, "rising": True},
        "vsaBull": {"score": 82}, "vsaBear": {"score": 30},
        "cvd": {"signal": "bullish", "strength": 0.8},
        "obi": {"valid": True, "bias": 0.24},
        "oi": {"bias": "bullish", "strength": 0.8},
        "squeeze": {"on": False, "release": True, "duration": 8,
                    "direction": "bullish"},
        "htf": {"direction": "bullish", "strength": 1.0},
        "sentiment": {"score": 0.5, "confidence": 0.8},
        "funding": {"blocked": False},
    })
    engine.pattern_stats_fn = lambda: {
        "hammer": {"wins": 7, "losses": 3, "weightedWinrate": 0.7,
                   "effectiveSamples": 9},
    }
    engine.on_signal(_hit("hammer"))
    engine._place_order.assert_called_once()
    assert "btcusdt" in engine._st(acct["name"])["positions"]
    trace = engine.decision_traces[-1]
    assert trace["status"] == "opened"
    assert trace["score"]["breakdown"]["bull"]["total"] > 50
    assert trace["risk"]["allowed"]
    assert any(stage["id"] == "execution" for stage in trace["stages"])


def test_reconcile_drops_orphan_account_positions(tmp_path, monkeypatch):
    """账户改名后旧名下的仓位必须被清掉，否则引擎一直管理幽灵仓位。"""
    import quant as q

    eng = q.QuantEngine({"list": []}, str(tmp_path / "q.json"))
    # 配置里只有一个账户，但本地记录挂在已经不存在的老名字下
    monkeypatch.setattr(eng, "_tradable_accounts", lambda: [
        {"name": "新账户", "api_key": "k", "api_secret": "s"}])
    eng._st("老账户")["positions"] = {
        "btcusdt": {"symbol": "btcusdt", "side": "SELL", "qty": 1, "entry": 100},
    }
    saved = {}
    monkeypatch.setattr(eng, "_save_positions", lambda: saved.setdefault("called", True))
    eng.reconcile_positions()
    assert eng._st("老账户")["positions"] == {}, "孤儿账户的仓位没有被清理"
    assert saved.get("called"), "对账后应落盘"


def test_reconcile_drops_positions_exchange_no_longer_holds(tmp_path, monkeypatch):
    import quant as q

    eng = q.QuantEngine({"list": []}, str(tmp_path / "q.json"))
    monkeypatch.setattr(eng, "_tradable_accounts", lambda: [
        {"name": "A", "api_key": "k", "api_secret": "s"}])
    eng._st("A")["positions"] = {
        "btcusdt": {"symbol": "btcusdt", "side": "SELL", "qty": 1, "entry": 100},
        "ethusdt": {"symbol": "ethusdt", "side": "BUY", "qty": 1, "entry": 200},
    }
    # 交易所：BTC 已平（0），ETH 还有
    monkeypatch.setattr(eng, "_live_amt",
                        lambda k, s, sym: 0.0 if sym == "btcusdt" else 1.0)
    monkeypatch.setattr(eng, "_save_positions", lambda: None)
    eng.reconcile_positions()
    left = eng._st("A")["positions"]
    assert "btcusdt" not in left, "交易所已无持仓却没被清理"
    assert "ethusdt" in left, "交易所仍在的仓位不能被误删"


def test_reconcile_keeps_positions_when_exchange_unreachable(tmp_path, monkeypatch):
    import quant as q

    eng = q.QuantEngine({"list": []}, str(tmp_path / "q.json"))
    monkeypatch.setattr(eng, "_tradable_accounts", lambda: [
        {"name": "A", "api_key": "k", "api_secret": "s"}])
    eng._st("A")["positions"] = {
        "btcusdt": {"symbol": "btcusdt", "side": "SELL", "qty": 1, "entry": 100}}
    monkeypatch.setattr(eng, "_live_amt", lambda k, s, sym: None)  # 查询失败
    monkeypatch.setattr(eng, "_save_positions", lambda: None)
    eng.reconcile_positions()
    assert "btcusdt" in eng._st("A")["positions"], "查不到交易所数据时不能乱删"
