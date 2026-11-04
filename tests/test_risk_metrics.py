"""风险指标：CVaR / 已实现波动率 / 回撤。

这几个数之前看板上完全没有，能看到的只有一个"回撤"百分比。
但没有尾部指标就看不出"一笔亏损能吃多少笔利润"——胜率高不等于能活。
"""
import math

import pytest

from analysis import risk_metrics as R


# ---------- CVaR ----------

def test_cvar_is_mean_of_worst_tail():
    # 100 个样本，最差 5 个是 -0.05..-0.01，均值 -0.03
    returns = [-0.05, -0.04, -0.03, -0.02, -0.01] + [0.001] * 95
    assert R.cvar(returns, 0.95) == pytest.approx(-0.03, abs=1e-9)


def test_cvar_refuses_to_guess_on_small_samples():
    """3 笔样本的"最差 5%"是伪统计，必须返回 None 而不是一个假数字。"""
    assert R.cvar([-0.1, 0.05, 0.02], 0.95) is None
    assert R.cvar([], 0.95) is None
    assert R.cvar(None, 0.95) is None


def test_cvar_99_is_deeper_than_95():
    returns = [round(-0.01 * i, 4) for i in range(1, 51)] + [0.002] * 50
    assert R.cvar(returns, 0.99) < R.cvar(returns, 0.95)


def test_cvar_ignores_non_finite_values():
    returns = [-0.05] * 20 + [float("nan"), float("inf"), "bad"]
    out = R.cvar(returns, 0.95)
    assert out is not None and math.isfinite(out)


# ---------- 波动率 ----------

def test_volatility_is_sample_stddev():
    assert R.volatility([0.01, 0.01, 0.01]) == 0.0
    # 两个点：样本标准差 = |差|/sqrt(2)
    assert R.volatility([0.0, 0.02]) == pytest.approx(0.02 / math.sqrt(2), abs=1e-6)


def test_volatility_needs_two_points():
    assert R.volatility([0.01]) is None
    assert R.volatility([]) is None


def test_volatility_is_not_annualised():
    """3 分钟周期的年化毫无意义（会得到 100%+ 的噪声）。

    这里只验证量级：日频 1% 的波动不该变成几百个百分点。
    """
    returns = [0.01, -0.01] * 50
    vol = R.volatility(returns)
    assert 0 < vol < 0.05, vol


# ---------- 回撤 ----------

def test_drawdown_from_peak():
    dd = R.drawdown([100, 110, 105, 90, 95])
    assert dd["peak"] == 110
    # 最低点 90 相对峰值 110 → -18.18%
    assert dd["max"] == pytest.approx(-18.18, abs=0.01)
    # 当前 95 相对峰值 110 → -13.64%
    assert dd["current"] == pytest.approx(-13.64, abs=0.01)
    assert dd["samples"] == 5


def test_drawdown_current_bars_counts_samples_since_peak():
    """回撤持续了多久——"刚回撤 1%"和"回撤三小时"是两件事。"""
    dd = R.drawdown([100, 105, 110, 108, 106, 104])
    # 峰值在 index 2，之后还有 3 个点
    assert dd["currentBars"] == 3
    assert dd["maxBars"] == 3


def test_drawdown_needs_three_points():
    assert R.drawdown([100, 99]) is None
    assert R.drawdown([]) is None


def test_drawdown_at_new_high_is_zero():
    dd = R.drawdown([100, 105, 110])
    assert dd["current"] == 0.0 and dd["max"] == 0.0
    assert dd["currentBars"] == 0


# ---------- 尾部画像 ----------

def test_tail_profile_reports_sample_count_even_when_insufficient():
    profile = R.tail_profile([0.01, -0.02, 0.03])
    assert profile["samples"] == 3
    assert profile["enoughSamples"] is False
    assert profile["cvar95"] is None, "样本不足时不能给 CVaR"
    assert profile["worst"] == -0.02
    assert profile["best"] == 0.03


def test_tail_profile_full_payload():
    returns = [round(-0.02 + 0.001 * i, 5) for i in range(40)]
    profile = R.tail_profile(returns)
    assert profile["enoughSamples"] is True
    assert profile["cvar95"] is not None
    assert profile["cvar99"] is not None
    assert profile["volatility"] is not None
    assert profile["mean"] is not None
    assert profile["cvar95"] < profile["mean"], "尾部均值必须低于整体均值"


def test_tail_profile_empty_is_all_none():
    profile = R.tail_profile([])
    assert profile["samples"] == 0
    assert profile["cvar95"] is None and profile["volatility"] is None
    assert profile["worst"] is None and profile["mean"] is None
