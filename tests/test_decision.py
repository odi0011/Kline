"""Decision-layer regression tests: pure calculations and risk gates."""
from __future__ import annotations

from datetime import datetime, timezone

from core.candle import Candle, apply_trade
from decision.backtest import deflated_sharpe_ratio, monte_carlo, run_backtest
from decision.config import DecisionConfig
from decision.features import (
    correlation,
    cvd_metrics,
    funding_context,
    macd,
    open_interest_bias,
    order_book_imbalance,
    squeeze_state,
)
from decision.risk import PositionRiskInput, RiskEngine, RiskInput, TradeRecord
from decision.regime import fit_gaussian_hmm, hmm_regime
from decision.scoring import score_signal
from decision.watch import build_watch_plan


NOW = 1_760_000_000_000


def bars(n=120, base=100.0, step=0.35, interval="3m"):
    out = []
    for i in range(n):
        close = base + i * step
        c = Candle(i * 180_000, close - step * 0.3, close + 0.5, close - 0.6,
                   close, interval=interval, closed=True)
        c.volume = 100 + i * 0.5
        c.buy_vol = c.volume * (0.62 if i % 3 else 0.45)
        c.sell_vol = c.volume - c.buy_vol
        out.append(c)
    return out


def base_risk_input(**patch):
    values = dict(
        account="a", symbol="ethusdt", side="BUY", entry=100.0, sl=99.0,
        equity=10_000.0, available=5_000.0, now_ms=NOW,
        step_size=0.001, positions=[], data_ok=True, signal_score=70.0,
    )
    values.update(patch)
    return RiskInput(**values)


def test_macd_cvd_and_orderbook_features():
    cs = bars()
    m = macd(cs)
    assert m and m["hist"] > 0
    cvd = cvd_metrics(cs)
    assert cvd["cvd"] > 0
    depth = {"ts": NOW - 500, "bids": [[99.9, 20], [99.8, 30]],
             "asks": [[100.1, 10], [100.2, 15]]}
    obi = order_book_imbalance(depth, now_ms=NOW, max_age_ms=2000)
    assert obi["valid"] and obi["bias"] > 0
    stale = order_book_imbalance(depth, now_ms=NOW + 5000, max_age_ms=2000)
    assert not stale["valid"]
    assert squeeze_state(cs)["on"] in (True, False)


def test_oi_funding_and_correlation():
    assert open_interest_bias(5, 2)["bias"] == "bullish"
    assert open_interest_bias(5, -2)["bias"] == "bearish"
    blocked = funding_context(0.0005, NOW + 5 * 60_000, "BUY", NOW)
    assert blocked["blocked"]
    assert correlation([1, 2, 3, 4] * 5, [1, 2, 3, 4] * 5) > 0.99


def test_scoring_rewards_aligned_trend():
    state = {
        "regime": "trend", "adx": 32, "plusDI": 31, "minusDI": 11,
        "rsi": 61, "z": 0.6, "roc": 0.012,
    }
    features = {
        "close": 105, "emaFast": 104, "emaSlow": 101, "emaMacro": 97,
        "macd": {"hist": 0.3, "rising": True},
        "vsaBull": {"score": 82}, "vsaBear": {"score": 32},
        "cvd": {"signal": "bullish", "strength": 0.7},
        "obi": {"valid": True, "bias": 0.22},
        "oi": {"bias": "bullish", "strength": 0.7},
        "squeeze": {"on": False, "release": True, "duration": 8, "direction": "bullish"},
        "htf": {"direction": "bullish", "strength": 0.9},
    }
    result = score_signal("bullish", state, features, DecisionConfig(), "hammer",
                          {"hammer": {"wins": 7, "losses": 3}})
    assert result.allowed
    assert result.score > result.opposite_score
    assert result.grade in ("medium", "strong")


def test_risk_daily_loss_and_profit_lock():
    engine = RiskEngine(DecisionConfig())
    engine.update_equity("a", 10_000, NOW)
    engine.record_trade("a", TradeRecord(NOW - 1000, -350, "btcusdt", "BUY"))
    denied = engine.evaluate(base_risk_input())
    assert not denied.allowed and denied.code == "daily_loss_limit"

    engine = RiskEngine(DecisionConfig())
    engine.update_equity("a", 10_000, NOW)
    engine.record_trade("a", TradeRecord(NOW - 1000, 600, "btcusdt", "BUY"))
    denied = engine.evaluate(base_risk_input())
    assert not denied.allowed and denied.code == "daily_profit_lock"


def test_risk_streak_cooldown_and_position_limits():
    engine = RiskEngine(DecisionConfig(consecutive_loss_limit=2))
    engine.update_equity("a", 10_000, NOW)
    engine.record_trade("a", TradeRecord(NOW - 2000, -1, "btcusdt", "BUY"))
    engine.record_trade("a", TradeRecord(NOW - 1000, -1, "ethusdt", "BUY"))
    denied = engine.evaluate(base_risk_input())
    assert not denied.allowed and denied.code == "loss_streak_pause"

    engine = RiskEngine(DecisionConfig())
    engine.update_equity("a", 10_000, NOW)
    engine.record_trade("a", TradeRecord(NOW - 1000, -1, "ethusdt", "BUY"))
    denied = engine.evaluate(base_risk_input(now_ms=NOW))
    assert not denied.allowed and denied.code == "cooldown"
    duplicate = [PositionRiskInput("ethusdt", "BUY", 1, 100, 99)]
    denied = engine.evaluate(base_risk_input(positions=duplicate, now_ms=NOW + 1_000_000))
    assert not denied.allowed and denied.code == "duplicate_symbol"


def test_risk_correlation_heat_and_volatility_scaling():
    engine = RiskEngine(DecisionConfig())
    engine.update_equity("a", 10_000, NOW)
    positions = [PositionRiskInput("btcusdt", "BUY", 5, 100, 99)]
    returns = {"btcusdt": [0.01, -0.01] * 30, "ethusdt": [0.01, -0.01] * 30}
    denied = engine.evaluate(base_risk_input(positions=positions, returns_by_symbol=returns))
    assert not denied.allowed and denied.code == "correlation_bucket"

    engine = RiskEngine(DecisionConfig())
    engine.update_equity("a", 10_000, NOW)
    result = engine.evaluate(base_risk_input(atr_pct=90))
    assert result.allowed and result.size_factor <= 0.5
    denied = engine.evaluate(base_risk_input(atr_pct=96))
    assert not denied.allowed and denied.code == "extreme_volatility"


def test_risk_order_rate_and_sizing():
    engine = RiskEngine(DecisionConfig(max_orders_per_min=2))
    engine.update_equity("a", 10_000, NOW)
    ok = engine.evaluate(base_risk_input())
    assert ok.allowed and ok.qty > 0 and ok.notional <= 500
    engine.record_order_attempt("a", NOW)
    engine.record_order_attempt("a", NOW)
    denied = engine.evaluate(base_risk_input())
    assert not denied.allowed and denied.code == "order_rate"


def test_session_and_macro_blackout_gates():
    at_2330 = int(datetime(2025, 11, 1, 23, 30, tzinfo=timezone.utc).timestamp() * 1000)
    cfg = DecisionConfig.from_dict({"session_blackouts_utc": ["23:00-01:00"]})
    engine = RiskEngine(cfg)
    engine.update_equity("a", 10_000, at_2330)
    denied = engine.evaluate(base_risk_input(now_ms=at_2330))
    assert not denied.allowed and denied.code == "session_blackout"

    at_noon = int(datetime(2025, 11, 1, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
    macro = [{
        "start": "2025-11-01T11:55:00Z",
        "end": "2025-11-01T12:30:00Z",
        "label": "FOMC",
    }]
    cfg = DecisionConfig.from_dict({"macro_events": macro})
    engine = RiskEngine(cfg)
    engine.update_equity("a", 10_000, at_noon)
    denied = engine.evaluate(base_risk_input(now_ms=at_noon))
    assert not denied.allowed and denied.code == "macro_blackout"


def test_multi_timeframe_watch_plan():
    entry = bars(80, 100, 0.05, "3m")
    h1 = bars(120, 100, 0.35, "1h")
    h4 = bars(120, 100, 1.2, "4h")
    plan = build_watch_plan("btcusdt", "bullish", entry, h1, h4,
                            equity=10_000, available=5_000,
                            config=DecisionConfig())
    assert plan.allowed, plan.reason
    assert plan.h4_direction == "bullish"
    assert plan.sl < plan.entry < plan.tp
    assert plan.qty > 0


def test_shadow_backtest_enters_next_bar_and_accounts_costs():
    class Marker:
        key = "marker"
        direction = "bullish"
        min_bars = 1
        priority = 1

        def check(self, cs):
            return len(cs) == 45

    cs = bars(90, 100, 0.25, "3m")
    result = run_backtest(cs, [Marker()], symbol="btcusdt", warmup=40,
                          max_hold_bars=10)
    assert result.trades
    first = result.trades[0]
    assert abs(first.entry - cs[45].open * (1 + 0.0001)) < 1e-8
    assert first.bars_held >= 1
    assert "maxDrawdownPct" in result.metrics


def test_monte_carlo_reports_tail_drawdown():
    result = monte_carlo([10, -5, 8, -12, 4, -3] * 10, iterations=200)
    assert result["iterations"] == 200
    assert result["p95"] >= result["p50"] >= 0


def test_deflated_sharpe_penalises_small_samples_and_many_trials():
    trial_srs = [0.1 + i * 0.01 for i in range(50)]
    high = deflated_sharpe_ratio(1.2, trial_srs, observations=500)
    low = deflated_sharpe_ratio(1.2, trial_srs, observations=20)
    assert high > low
    assert 0 <= high <= 1 and 0 <= low <= 1


def test_gaussian_hmm_probabilities_and_bull_state():
    observations = [-0.25] * 30 + [-0.12] * 30 + [0.05] * 10 + [
        0.05 + i * 0.005 for i in range(40)
    ]
    fit = fit_gaussian_hmm(observations, iterations=30)
    assert fit["ok"]
    assert abs(sum(fit["probabilities"].values()) - 1.0) < 1e-6
    assert fit["probabilities"]["bull"] > fit["probabilities"]["bear"]
    regime = hmm_regime(bars(120, 100, 0.3, "4h"), window=120)
    assert regime["ok"] and regime["label"] in ("bear", "range", "bull")
