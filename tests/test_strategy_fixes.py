"""数据驱动的策略修复回归：默认开关、rr 下界、方向过滤、回放判定口径。

依据 scripts/param_sweep.py 的 7 天 2809 笔 / 30 天 4094 笔线上回放：
只做空为正期望、rr 3.0 是扫描最优档、55/15 硬门槛在制造负 alpha。
"""
from __future__ import annotations

import inspect
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from decision.config import DecisionConfig  # noqa: E402
from decision.paper import PaperBook  # noqa: E402
from decision.scoring import score_signal  # noqa: E402
from param_sweep import replay  # noqa: E402
from quant import QuantEngine  # noqa: E402


def test_defaults_follow_replay_data():
    cfg = DecisionConfig()
    assert cfg.allow_long is False
    assert cfg.score_gate_enabled is False
    assert cfg.watch_tp_atr_mult == 4.5
    assert cfg.watch_sl_min_pct == 0.004
    assert cfg.watch_sl_max_pct == 0.020
    assert cfg.auto_trade_enabled is True


def test_switches_can_be_reenabled_from_config():
    cfg = DecisionConfig.from_dict({
        "allow_long": True, "score_gate_enabled": True,
        "watch_tp_atr_mult": 2.5,
    })
    assert cfg.allow_long is True
    assert cfg.score_gate_enabled is True
    assert cfg.watch_tp_atr_mult == 2.5


def _state():
    return {"regime": "range", "adx": 10, "rsi": 50,
            "plusDI": 20, "minusDI": 20}


def _features():
    return {"close": 100.0, "emaFast": 100.0, "emaSlow": 100.0,
            "emaMacro": 100.0}


def test_low_score_is_allowed_when_gate_disabled():
    off = score_signal("bullish", _state(), _features(),
                       DecisionConfig(score_gate_enabled=False))
    assert off.allowed
    assert not any("共鸣分" in v for v in off.vetoes)


def test_gate_still_vetoes_when_explicitly_enabled():
    on = score_signal("bullish", _state(), _features(),
                      DecisionConfig(score_gate_enabled=True,
                                     minimum_score=999.0))
    assert not on.allowed
    assert any("共鸣分" in v for v in on.vetoes)


def test_long_signal_is_filtered_by_default():
    eng = object.__new__(QuantEngine)
    eng.enabled = True
    eng._op_lock = threading.Lock()
    eng.decision_cfg = DecisionConfig()
    eng.symbol_filter = None
    eng.feed_is_live_fn = None
    eng.stats = {"signals": 0}
    eng._primary_account = lambda: "acct"
    eng._event = MagicMock()
    eng._trace_start = MagicMock()
    eng.price_fn = lambda s: 100.0

    eng.on_signal({"symbol": "btcusdt", "direction": "bullish",
                   "pattern": "hammer", "price": 100.0})
    eng._event.assert_called_once()
    assert "只做空" in eng._event.call_args.args[1]
    eng._trace_start.assert_not_called()


def test_short_signal_is_not_filtered():
    eng = object.__new__(QuantEngine)
    eng.enabled = True
    eng._op_lock = threading.Lock()
    eng.decision_cfg = DecisionConfig()
    eng.symbol_filter = None
    eng.feed_is_live_fn = None
    eng.bars_fn = None
    eng.stats = {"signals": 0, "errors": 0}
    eng._primary_account = lambda: "acct"
    eng._event = MagicMock()
    eng._trace_start = MagicMock(return_value={})
    eng.price_fn = lambda s: 100.0
    eng._decide_open = MagicMock()

    # 只验证方向门没拦做空；后续决策链需要完整运行时上下文，异常由
    # on_signal 自己兜住，这里不关心。
    try:
        eng.on_signal({"symbol": "btcusdt", "direction": "bearish",
                       "pattern": "hammer", "price": 100.0})
    except Exception:
        pass
    reasons = [str(c.args[1]) for c in eng._event.call_args_list
               if len(c.args) > 1]
    assert not any("只做空" in r for r in reasons)


def test_paper_skips_long_when_disabled():
    book = PaperBook()
    ctx = {"direction": "bullish", "atr": 1.0, "score": 70.0, "gap": 20.0,
           "grade": "medium", "pattern": "hammer", "patternStats": {},
           "maxLeverage": 20, "allowLong": False, "price": 100.0}
    assert book._open("btcusdt", ctx, 100.0) is None
    assert not book.positions


def test_paper_default_rr_is_three():
    book = PaperBook()
    ctx = {"direction": "bearish", "atr": 1.0, "score": 70.0, "gap": 20.0,
           "grade": "medium", "pattern": "hammer", "patternStats": {},
           "maxLeverage": 20, "allowLong": False, "price": 100.0}
    book._open("btcusdt", ctx, 100.0)
    assert book.positions
    pos = book.positions["btcusdt"]
    assert pos.rr == 3.0


def _bars(*rows):
    return [(0, 100.0, 100.0, 100.0, 100.0)] + list(rows)


def test_replay_same_bar_double_touch_is_loss():
    bars = _bars((60_000, 100.0, 102.0, 99.0, 100.0),
                 (120_000, 100.0, 100.1, 99.9, 100.0),
                 (180_000, 100.0, 100.1, 99.9, 100.0))
    r = replay(bars, 0, 100.0, "bullish", 0.004, 3.0, 10, 0.1)
    assert r == pytest.approx(-0.5)


def test_replay_target_only_is_win():
    bars = _bars((60_000, 100.0, 101.3, 99.9, 101.2),
                 (120_000, 100.0, 101.4, 100.0, 101.3),
                 (180_000, 100.0, 101.4, 100.0, 101.3))
    r = replay(bars, 0, 100.0, "bullish", 0.004, 3.0, 10, 0.1)
    assert r == pytest.approx(1.1)


def test_replay_short_window_returns_none():
    assert replay(_bars((60_000, 100.0, 100.1, 99.9, 100.0)),
                  0, 100.0, "bullish", 0.004, 3.0, 10, 0.1) is None


# ---------- 信号质量门 / 实时特征永久化 ----------

from datetime import datetime, timezone  # noqa: E402
from types import SimpleNamespace  # noqa: E402


def _utc_ms(hour: int) -> int:
    return int(datetime(2026, 11, 6, hour, 0, tzinfo=timezone.utc).timestamp() * 1000)


def _noise_bars(body: float, span: float, n: int = 30):
    return [SimpleNamespace(open=100.0, high=100.0 + span / 2,
                            low=100.0 - span / 2, close=100.0 + body,
                            volume=0.0) for _ in range(n)]


def test_quality_gate_rejects_noise_session_and_pattern():
    from decision.config import DecisionConfig
    from decision.quality import evaluate_quality
    # 影子对照后默认已关掉时段/噪声硬拒；这里显式打开旧规则，
    # 验证质量门本身仍然能工作（回滚路径可用）。
    cfg = DecisionConfig.from_dict({
        "quality_blackouts_utc": ["12:00-18:00"],
        "quality_noise_reject": 0.45,
    })
    common = dict(cfg=cfg, direction="bearish", regime="range", atr_pct=50)
    v = evaluate_quality(**common, pattern="bearish_engulfing", now_ms=_utc_ms(13),
                         bars=_noise_bars(0.6, 1.0))
    assert not v.allowed and "12:00-18:00" in v.reasons[0]
    v = evaluate_quality(**common, pattern="bearish_engulfing", now_ms=_utc_ms(3),
                         bars=_noise_bars(0.0, 2.0))
    assert not v.allowed and "噪声" in v.reasons[0]
    v = evaluate_quality(**common, pattern="hammer", now_ms=_utc_ms(3),
                         bars=_noise_bars(0.6, 1.0))
    assert not v.allowed and "形态" in v.reasons[0]


def test_quality_gate_defaults_follow_shadow_control():
    """影子对照后：时段/噪声不再硬拒，趋势不降仓，黑名单仍生效。"""
    from decision.config import DecisionConfig
    from decision.quality import evaluate_quality
    cfg = DecisionConfig()
    assert cfg.quality_blackouts == ()
    assert cfg.quality_noise_reject >= 1.0
    assert cfg.quality_trend_size_factor == 1.0
    common = dict(cfg=cfg, direction="bearish", regime="trend", atr_pct=50)
    # 13 点 + 高噪声：旧默认会被硬拒，新默认必须放行
    v = evaluate_quality(**common, pattern="bearish_engulfing",
                         now_ms=_utc_ms(13), bars=_noise_bars(0.0, 2.0))
    assert v.allowed, v.reasons
    assert v.size_factor == 1.0, v.size_factor
    # 形态黑名单是唯一有正向依据的硬拒，保留
    v2 = evaluate_quality(**common, pattern="hammer", now_ms=_utc_ms(3),
                          bars=_noise_bars(0.6, 1.0))
    assert not v2.allowed and "形态" in v2.reasons[0]


def test_empty_blackouts_config_really_disables_them():
    from decision.config import DecisionConfig
    assert DecisionConfig.from_dict(
        {"quality_blackouts_utc": []}).quality_blackouts == ()
    assert len(DecisionConfig.from_dict(
        {"quality_blackouts_utc": ["12:00-18:00"]}).quality_blackouts) == 1


def test_quality_gate_downsizes_trend_and_high_score():
    """降仓链路本身仍能工作：显式传旧系数，验证乘法叠加。"""
    from decision.config import DecisionConfig
    from decision.quality import evaluate_quality
    cfg = DecisionConfig.from_dict({"quality_trend_size_factor": 0.5})
    v = evaluate_quality(
        cfg=cfg, direction="bearish", pattern="bearish_engulfing",
        regime="trend", atr_pct=50, bars=_noise_bars(0.6, 1.0),
        score=70.0, gap=10.0, now_ms=_utc_ms(3))
    assert v.allowed
    assert v.size_factor == round(0.5 * 0.8, 4)
    assert v.tags["sizeFactor"] == v.size_factor
    # 新默认不再按 trend 降仓，只剩高分降仓
    v2 = evaluate_quality(
        cfg=DecisionConfig(), direction="bearish", pattern="bearish_engulfing",
        regime="trend", atr_pct=50, bars=_noise_bars(0.6, 1.0),
        score=70.0, gap=10.0, now_ms=_utc_ms(3))
    assert v2.allowed and v2.size_factor == 0.8


def test_quality_gate_can_be_disabled():
    from decision.config import DecisionConfig
    from decision.quality import evaluate_quality
    v = evaluate_quality(
        cfg=DecisionConfig(quality_enabled=False), direction="bearish",
        pattern="hammer", regime="trend", atr_pct=None,
        bars=_noise_bars(0.0, 2.0), now_ms=_utc_ms(13))
    assert v.allowed and v.size_factor == 1.0


def test_signal_feature_snapshot_is_persisted():
    from quant import QuantEngine
    eng = object.__new__(QuantEngine)
    sink = MagicMock()
    eng.feature_storage_fn = sink
    eng._event = MagicMock()
    hit = {"symbol": "btcusdt", "interval": "1m", "pattern": "hammer",
           "mode": "confirmed", "price": 100.0, "bar_open_time": 123}
    st = {"regime": "range", "adx": 12.0, "atr": 0.5, "atrPct": 40.0,
          "volState": "normal", "rsi": 50.0, "z": 0.2, "squeeze": False}
    from decision.quality import QualityVerdict
    verdict = QualityVerdict(True, 0.5, ["趋势市降仓"], {"regime": "trend"})
    eng._persist_signal_features(hit, "btcusdt", "bearish", 100.0, st, {},
                                 None, _noise_bars(0.6, 1.0), verdict)
    sink.assert_called_once()
    row = sink.call_args.args[0]
    for key in ("ts", "symbol", "regime", "noise_ratio", "price",
                "bar_open_time", "quality_allowed", "quality_size_factor"):
        assert key in row, key
    assert row["regime"] == "range"
    assert row["noise_ratio"] is not None


def test_prune_never_deletes_signal_features_or_critical_tables():
    from storage import Storage
    src = inspect.getsource(Storage.prune)
    assert "DELETE FROM signal_features" not in src
    # 关键表默认永久：0 天时 prune 跳过清理
    assert Storage({"user": "x"}).hit_days == 0


def test_system_prompt_documents_current_mechanism():
    """Agent 不能拿历史聊天里的旧参数解释当前行为。"""
    from ai.agent import SYSTEM_PROMPT
    for token in ("allowLong=false", "scoreGateEnabled=false", "minRr=3.0",
                  "只做空", "不再市价平仓", "已过期", "read_risk"):
        assert token in SYSTEM_PROMPT, token
    # 旧的「补挂失败就平仓」示例必须已经改掉
    assert "系统自己把它平掉了" not in SYSTEM_PROMPT


def test_snapshot_params_expose_new_switches():
    """read_risk(what=params) 的数据源必须带着新机制字段。"""
    src = inspect.getsource(QuantEngine.snapshot)
    for token in ('"allowLong"', '"autoTradeEnabled"', '"scoreGateEnabled"',
                  '"minRr"', '"watchSlMinPct"', '"policyNote"'):
        assert token in src, token


def _mk_bars(n, step, interval):
    from core.candle import Candle
    return [Candle(i * 60_000, 100 + i * step, 100 + i * step + 0.05,
                   100 + i * step - 0.05, 100 + i * step, volume=100,
                   interval=interval, closed=True) for i in range(n)]


def test_watch_plan_clamps_stop_distance():
    """watch_plan 是 _decide_open 首选分支，低 ATR 时也必须 ≥0.4%。"""
    from decision.watch import build_watch_plan
    entry = _mk_bars(80, 0.05, "3m")
    h1 = _mk_bars(120, 0.35, "1h")
    h4 = _mk_bars(120, 1.2, "4h")
    plan = build_watch_plan(
        "btcusdt", "bullish", entry, h1, h4,
        equity=10_000, available=5_000,
        config=DecisionConfig(watch_sl_atr_mult=0.0001))
    assert plan.allowed, plan.reason
    sl_pct = abs(plan.entry - plan.sl) / plan.entry
    tp_pct = abs(plan.tp - plan.entry) / plan.entry
    assert sl_pct >= 0.004 - 1e-9, sl_pct
    assert tp_pct >= sl_pct * 3.0 - 1e-9, (tp_pct, sl_pct)


def test_reused_evaluation_respects_gate_switch():
    """复用页面评估的主路径不能绕过 score_gate_enabled。"""
    from quant import _ScoreFromEvaluation
    ev = {"score": 40.0, "oppositeScore": 60.0, "gap": 5.0, "grade": "weak",
          "confidence": 0.4, "breakdown": {}, "reasons": [], "vetoes": [],
          "direction": "bearish"}
    off = _ScoreFromEvaluation(ev, "bearish",
                               DecisionConfig(score_gate_enabled=False))
    assert off.allowed and off.vetoes == []
    on = _ScoreFromEvaluation(ev, "bearish",
                              DecisionConfig(score_gate_enabled=True))
    assert not on.allowed
    assert any("共鸣分" in v for v in on.vetoes)


def test_shadow_mode_blocks_real_orders():
    eng = object.__new__(QuantEngine)
    eng.enabled = True
    eng._op_lock = threading.Lock()
    eng.decision_cfg = DecisionConfig(auto_trade_enabled=False)
    eng.symbol_filter = None
    eng.feed_is_live_fn = None
    eng.stats = {"signals": 0}
    eng._primary_account = lambda: "acct"
    eng._event = MagicMock()
    eng._trace_start = MagicMock()
    eng.price_fn = lambda s: 100.0
    eng.on_signal({"symbol": "btcusdt", "direction": "bearish",
                   "pattern": "hammer", "price": 100.0})
    eng._event.assert_called_once()
    assert "影子模式" in eng._event.call_args.args[1]
    eng._trace_start.assert_not_called()
