"""Execution, sentiment and market-stream guard tests."""
from __future__ import annotations

import json
from pathlib import Path

import accounts
import yaml
from decision.config import DecisionConfig
from decision.drift import ADWIN
from decision.pool_audit import audit_symbol
from feed.liquidation import LiquidationStream
from feed.sentiment import aggregate_news_sentiment, score_item


NOW = 1_760_000_000_000


def test_decision_config_accepts_human_percentages():
    cfg = DecisionConfig.from_dict({
        "daily_loss_limit_pct": 3,
        "portfolio_heat_max_pct": 6,
        "risk_per_trade_pct": 1,
    })
    assert cfg.daily_loss_limit_pct == 0.03
    assert cfg.portfolio_heat_max_pct == 0.06
    assert cfg.risk_per_trade_pct == 0.01


def test_example_config_parses_to_decision_config():
    raw = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config.example.yaml").read_text("utf-8"))
    cfg = DecisionConfig.from_dict(raw["quant"]["decision"])
    assert cfg.exchange_protection
    assert cfg.portfolio_heat_max_pct == 0.06
    assert cfg.pool_max_spread == 0.0005


def test_protective_order_payload(monkeypatch):
    captured = {}

    def fake_request(api_key, api_secret, path, params=None, **kwargs):
        captured.update({
            "api_key": api_key, "path": path, "params": params,
            "method": kwargs.get("method"),
        })
        return {"orderId": 7}

    monkeypatch.setattr(accounts, "signed_request", fake_request)
    result = accounts.place_protective_order(
        "key", "secret", "btcusdt", "SELL", "STOP_MARKET", 0.0046, 79700.0)
    assert result["orderId"] == 7
    assert captured["path"] == "/fapi/v1/order"
    assert captured["method"] == "POST"
    assert captured["params"]["type"] == "STOP_MARKET"
    assert captured["params"]["reduceOnly"] == "true"
    assert captured["params"]["workingType"] == "MARK_PRICE"


def test_liquidation_stream_blocks_cascade_and_cooldown():
    stream = LiquidationStream(["btcusdt"])
    for i in range(5):
        raw = json.dumps({"data": {"o": {
            "s": "BTCUSDT", "S": "SELL", "ap": "100", "q": "20000",
            "T": NOW - 1000 + i,
        }}})
        stream._handle(raw)
    active = stream.cascade("btcusdt", now_ms=NOW, min_events=5,
                            min_notional=1_000_000, cooldown_sec=900)
    assert active["active"] and active["blocked"]
    later = stream.cascade("btcusdt", now_ms=NOW + 120_000, min_events=5,
                           min_notional=1_000_000, cooldown_sec=900)
    assert later["cooldown"] and later["blocked"]


def test_news_sentiment_catalog_and_keywords():
    negative = score_item({
        "cat": "delisting", "title": "Exchange will delist BTC perpetual",
        "ts": NOW,
    })
    positive = score_item({
        "cat": "new listing", "title": "Exchange announces BTC listing",
        "ts": NOW,
    })
    assert negative["score"] < -0.5
    assert positive["score"] > 0.5
    aggregate = aggregate_news_sentiment([
        {"cat": "delisting", "title": "BTC delist", "ts": NOW, "rel": 1000},
    ], "btcusdt", NOW)
    assert aggregate["score"] < 0 and aggregate["confidence"] > 0


def test_adwin_detects_mean_shift():
    detector = ADWIN(delta=0.002, min_window=8)
    detected = False
    for _ in range(40):
        detected = detector.add(0.0) or detected
    for _ in range(60):
        detected = detector.add(1.0) or detected
    assert detected
    assert detector.drift_count >= 1


def test_pool_audit_is_advisory_and_flags_wide_spread():
    from decision.config import DecisionConfig
    report = audit_symbol(
        "btcusdt", DecisionConfig(pool_max_spread=0.0005),
        bars=[], depth={"bids": [[100, 1]], "asks": [[101, 1]]},
        quote_volume=100_000_000, onboard_ms=NOW - 90 * 86_400_000,
        now_ms=NOW,
    )
    assert report["status"] == "downgrade"
    assert "spread too wide" in report["reasons"]
