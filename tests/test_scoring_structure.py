"""结构分项不能被 10 档挂单的秒级快照单独决定方向。

2026-11-11 用户反馈 ETH 1h 连涨 12 根却判做空。实机 13:22 算式：
    空方 势70.3×1.25 + 能35.5×0.95 + 量20.2×0.65 + 构100×1.0 = 60.99
    多方 36.09 → 判空 medium
构=100 来自 OBI×250 被打满。1380 条带 breakdown 的决策里 43.5% 的
structure 落在 0/100 极值，多空结构差中位 77.8（趋势差仅 39.2），
结构置中性会让 11.0% 的方向判定翻转。
"""
from __future__ import annotations

from decision.config import DecisionConfig
from decision.scoring import _structure_scores, score_signal


def _features(bias: float) -> dict:
    return {"obi": {"valid": True, "bias": bias}}


def test_default_scale_and_config_rollback():
    assert DecisionConfig().structure_obi_scale == 50.0
    assert DecisionConfig.from_dict(
        {"structure_obi_scale": 250}).structure_obi_scale == 250.0
    assert DecisionConfig.from_dict(
        {"structure_obi_scale": 0}).structure_obi_scale == 0.0
    assert DecisionConfig.from_dict(
        {"structure_obi_scale": 999}).structure_obi_scale == 250.0
    assert DecisionConfig.from_dict(
        {"structure_obi_scale": -5}).structure_obi_scale == 0.0


def test_obi_no_longer_saturates_structure():
    assert _structure_scores({}, _features(0.20), None, None) == (60.0, 40.0)
    assert _structure_scores({}, _features(-0.20), None, None) == (40.0, 60.0)
    # 极端挂单也不能打满 0/100
    bull, bear = _structure_scores({}, _features(0.42), None, None)
    assert 0.0 < bear < 100.0 and 0.0 < bull < 100.0


def test_old_behavior_is_restorable():
    assert _structure_scores({}, _features(0.20), None, None,
                             obi_scale=250.0) == (100.0, 0.0)
    assert _structure_scores({}, _features(0.20), None, None,
                             obi_scale=0.0) == (50.0, 50.0)


def test_score_signal_uses_config_and_shrinks_gap():
    """接近实机的场景：DI 偏空、MACD 为负、盘口卖压。

    方向仍由趋势/动能决定（这里确实是空），但分离度不该被盘口放大一倍。
    """
    state = {"regime": "trend", "adx": 39.3, "plusDI": 20.0,
             "minusDI": 28.16, "rsi": 47.2, "roc": 0.0115, "z": 0.0}
    feats = {**_features(-0.20), "macd": {"hist": -1.0, "rising": False}}

    old = score_signal("bearish", state, feats,
                       DecisionConfig(structure_obi_scale=250.0))
    new = score_signal("bearish", state, feats,
                       DecisionConfig(structure_obi_scale=50.0))
    assert old.breakdown["bear"]["structure"] == 100.0
    assert old.breakdown["bull"]["structure"] == 0.0
    assert new.breakdown["bear"]["structure"] == 60.0
    assert new.breakdown["bull"]["structure"] == 40.0
    assert new.gap < old.gap * 0.6, (new.gap, old.gap)


def test_eth_1322_counterfactual_documents_the_bug():
    """实机 13:22 那轮：仅把结构项中性化，方向就会翻多。"""
    w = {"trend": 1.25, "momentum": 0.95, "volume": 0.65, "structure": 1.0}
    tw = sum(w.values())
    bear_parts = {"trend": 70.3, "momentum": 35.5, "volume": 20.2,
                  "structure": 100.0}
    bear_total = 60.99
    bull_total = 36.09
    assert bear_total > bull_total

    # 反推多方结构项（实机同样被饱和到 0），再两边都置 50
    bull_struct = 0.0
    neutral_bear = bear_total - (bear_parts["structure"] - 50) * w["structure"] / tw
    neutral_bull = bull_total - (bull_struct - 50) * w["structure"] / tw
    assert neutral_bull > neutral_bear, (neutral_bull, neutral_bear)
