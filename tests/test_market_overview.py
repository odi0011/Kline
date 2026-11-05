"""账户卡下半区：市场情绪 + 30 分钟/今日战绩。

替代原来的权益热力图——热力图只回答"哪天赚了多少"，
这块空间该回答"现在市场什么状态、这套策略在这些窗口里打得怎么样"。
"""
import time

import pytest

from decision.config import DecisionConfig
import quant as Q


def _engine(tmp_path, monkeypatch=None):
    return Q.QuantEngine(DecisionConfig(), str(tmp_path / "config.yaml"))


def _at(engine, trades, accounts=("A",)):
    engine.trades = trades
    engine._tradable_accounts = lambda: [{"name": n} for n in accounts]
    return engine


# ---------- 情绪 ----------

def test_sentiment_counts_direction_breadth(tmp_path):
    eng = _engine(tmp_path)
    eng.live_evaluations = {
        "btc": {"direction": "bullish", "score": 62.0},
        "eth": {"direction": "bullish", "score": 58.0},
        "sol": {"direction": "bearish", "score": 44.0},
    }
    s = eng._sentiment()
    assert s["bullish"] == 2 and s["bearish"] == 1
    assert s["breadth"] == pytest.approx(66.7, abs=0.1)
    assert s["label"] == "偏多"
    assert s["avgScore"] == pytest.approx(54.7, abs=0.1)


def test_sentiment_labels_the_three_regimes(tmp_path):
    eng = _engine(tmp_path)
    cases = [
        ({"a": {"direction": "bullish"}, "b": {"direction": "bullish"},
          "c": {"direction": "bearish"}}, "偏多"),
        ({"a": {"direction": "bearish"}, "b": {"direction": "bearish"},
          "c": {"direction": "bullish"}}, "偏空"),
        ({"a": {"direction": "bullish"}, "b": {"direction": "bearish"}}, "分歧"),
    ]
    for evals, expected in cases:
        eng.live_evaluations = evals
        assert eng._sentiment()["label"] == expected, evals


def test_sentiment_without_direction_is_safe(tmp_path):
    eng = _engine(tmp_path)
    eng.live_evaluations = {}
    s = eng._sentiment()
    assert s["label"] == "无方向"
    assert s["breadth"] is None
    assert s["avgScore"] is None


# ---------- 窗口统计 ----------

def test_window_stats_winrate_uses_decided_denominator(tmp_path):
    """胜率分母只算有明确输赢的笔数，平局不进分母。"""
    eng = _engine(tmp_path)
    rows = [
        {"pnl": 3.0, "movePct": 0.3},
        {"pnl": -1.0, "movePct": -0.1},
        {"pnl": 0.0, "movePct": 0.0},      # 平局
        {"pnl": 2.0, "movePct": 0.2},
    ]
    s = eng._window_stats(rows)
    assert s["trades"] == 4
    assert s["wins"] == 2 and s["losses"] == 1
    assert s["winrate"] == pytest.approx(66.7, abs=0.1), "2/(2+1)，平局不进分母"
    assert s["pnl"] == pytest.approx(4.0)


def test_window_stats_drawdown_from_equity_curve(tmp_path):
    eng = _engine(tmp_path)
    # 先赚 5 再亏 8：峰值 5、当前 -3、最大回撤 -8
    rows = [{"pnl": 5.0, "movePct": 0.5}, {"pnl": -8.0, "movePct": -0.8}]
    s = eng._window_stats(rows)
    assert s["maxDrawdown"] == pytest.approx(-8.0)
    assert s["pnl"] == pytest.approx(-3.0)


def test_window_stats_sharpe_not_annualised(tmp_path):
    eng = _engine(tmp_path)
    returns = [0.002, -0.001, 0.003, -0.002, 0.001]
    rows = [{"pnl": 0.1, "movePct": r * 100} for r in returns]
    s = eng._window_stats(rows)
    assert s["sharpe"] is not None
    # 不年化：量级是个位数以内，不可能出现几百
    assert -10 < s["sharpe"] < 10


def test_window_stats_empty(tmp_path):
    eng = _engine(tmp_path)
    s = eng._window_stats([])
    assert s["trades"] == 0
    assert s["winrate"] is None and s["sharpe"] is None
    assert s["pnl"] == 0 and s["maxDrawdown"] == 0


def test_window_stats_needs_three_samples_for_sharpe(tmp_path):
    eng = _engine(tmp_path)
    s = eng._window_stats([{"pnl": 1, "movePct": 0.1},
                           {"pnl": -1, "movePct": -0.1}])
    assert s["sharpe"] is None, "两笔样本算不出有意义的夏普"


# ---------- 概览整体 ----------

def test_market_overview_windows(tmp_path):
    eng = _engine(tmp_path)
    now = int(time.time() * 1000)
    _at(eng, [
        {"ts": now - 60_000, "account": "A", "pnl": 3.0, "movePct": 0.3},     # 30m + 今日
        {"ts": now - 40 * 60_000, "account": "A", "pnl": -2.0, "movePct": -0.2},  # 今日、非 30m
    ])
    ov = eng.market_overview()
    assert ov["m30"]["trades"] == 1 and ov["m30"]["pnl"] == pytest.approx(3.0)
    assert ov["today"]["trades"] == 2 and ov["today"]["pnl"] == pytest.approx(1.0)


def test_market_overview_excludes_other_accounts(tmp_path):
    """换过 API Key 的旧账户成交不能混进当前账户战绩。"""
    eng = _engine(tmp_path)
    now = int(time.time() * 1000)
    _at(eng, [
        {"ts": now - 60_000, "account": "A", "pnl": 1.0, "movePct": 0.1},
        {"ts": now - 60_000, "account": "OLD", "pnl": 99.0, "movePct": 9.9},
    ])
    ov = eng.market_overview()
    assert ov["today"]["trades"] == 1
    assert ov["today"]["pnl"] == pytest.approx(1.0)


def test_market_overview_does_not_recurse(tmp_path):
    """market_overview 会被 snapshot 调用，内部绝不能再调 snapshot。

    （第一版就是这么写的，直接无限递归。）检查时先剥掉注释，
    否则"不要调 snapshot"这句注释本身会把测试搞假阳性。
    """
    import inspect
    import re
    src = inspect.getsource(Q.QuantEngine.market_overview)
    code = re.sub(r"#[^\n]*", "", src)
    assert "self.snapshot(" not in code


def test_snapshot_contains_overview(tmp_path):
    eng = _engine(tmp_path)
    eng.live_evaluations = {"btc": {"direction": "bullish", "score": 60.0}}
    _at(eng, [])
    snap = eng.snapshot()
    ov = (snap.get("analysis") or {}).get("overview")
    assert isinstance(ov, dict)
    assert ov["sentiment"]["label"] == "偏多"
    assert "m30" in ov and "today" in ov


def test_overview_is_in_wire_whitelist():
    """漏进白名单 → 前端拿到 undefined，整块不显示（risk/paperChecks 都踩过）。"""
    from server.hub import BroadcastHub
    assert "overview" in BroadcastHub._WIRE_ANALYSIS


# ---------- 前端接线 ----------

def test_account_card_renders_overview_not_heatmap():
    """账户卡下半区：无仓位显示概览，有仓位显示仓位 + 概览。"""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    card = (root / "web/src/components/arc/account-card/account-card.jsx").read_text(encoding="utf-8")
    assert "MarketOverview" in card
    assert "EquityHeatmap" not in card
    # 有仓位时仓位卡优先，概览不藏起来
    assert "styles.split" in card
    compass = (root / "web/src/components/arc/account-card/market-overview.jsx").read_text(encoding="utf-8")
    for phrase in ("情绪", "胜率", "盈亏", "回撤", "夏普"):
        assert phrase in compass, f"概览缺少: {phrase}"


def test_app_passes_overview_to_account_card():
    from pathlib import Path
    app = (Path(__file__).resolve().parent.parent / "web/src/App.jsx").read_text(encoding="utf-8")
    assert "overview={quantSnap?.analysis?.overview}" in app


def test_window_stats_counts_draws(tmp_path):
    """平局要能被区分出来：有成交但全平局 ≠ 没交易。

    线上实测：今日 2 笔都是 0 盈亏，winrate=None，前端显示"无交易"——
    其实交易了，只是没分出输赢。
    """
    eng = _engine(tmp_path)
    s = eng._window_stats([{"pnl": 0.0, "movePct": 0.0},
                           {"pnl": 0.0, "movePct": 0.0}])
    assert s["trades"] == 2
    assert s["wins"] == 0 and s["losses"] == 0
    assert s["draws"] == 2
    assert s["winrate"] is None


def test_window_stats_draws_excluded_from_winrate(tmp_path):
    eng = _engine(tmp_path)
    s = eng._window_stats([{"pnl": 2.0, "movePct": 0.2},
                           {"pnl": -1.0, "movePct": -0.1},
                           {"pnl": 0.0, "movePct": 0.0}])
    assert s["draws"] == 1
    assert s["winrate"] == pytest.approx(50.0), "1/(1+1)，平局不进分母"


# ---------- 模拟仓必须算进概览 ----------

def test_overview_includes_paper_trades(tmp_path):
    """模拟仓流水要一起统计。

    线上实测：只算实盘时 30 分钟 0 笔、今日 2 笔（还全是平局），
    而模拟仓 30 分钟有 21 笔、今日 60 笔——面板看着像坏了。
    """
    eng = _engine(tmp_path)
    now = int(time.time() * 1000)
    _at(eng, [{"ts": now - 60_000, "account": "A", "pnl": 3.0, "movePct": 0.3}])

    class _Paper:
        store = None

        def history_list(self):
            return [{"closedAt": now - 120_000, "pnl": 0.5,
                     "entry": 100.0, "exit": 100.5, "direction": "long"},
                    {"closedAt": now - 180_000, "pnl": -0.2,
                     "entry": 100.0, "exit": 99.8, "direction": "long"}]

    eng.paper = _Paper()
    ov = eng.market_overview()
    m30 = ov["m30"]
    assert m30["trades"] == 3, m30
    assert m30["realTrades"] == 1
    assert m30["paperTrades"] == 2
    assert m30["pnl"] == pytest.approx(3.3)


def test_overview_short_position_move_pct_is_signed(tmp_path):
    """模拟仓只有 pnlPct（保证金收益率），要用 entry/exit/方向反算价格涨跌幅。

    否则夏普会把空头的盈利算成"价格涨了"——方向是反的。
    """
    eng = _engine(tmp_path)
    now = int(time.time() * 1000)
    _at(eng, [])

    class _Paper:
        store = None

        def history_list(self):
            # 空头：开 100 平 99（赚），价格涨跌幅应该是 +1%
            return [{"closedAt": now - 60_000, "pnl": 1.0,
                     "entry": 100.0, "exit": 99.0, "direction": "short"}]

    eng.paper = _Paper()
    norm = eng._norm_trade(eng._paper_rows_for_overview()[0], "paper")
    assert norm["movePct"] == pytest.approx(1.0)


def test_overview_paper_rows_are_cached(tmp_path):
    """概览每 5 秒被调用一次，不能每次都拉 2000 行库。"""
    eng = _engine(tmp_path)
    eng._tradable_accounts = lambda: [{"name": "A"}]
    pulls = []

    class _Store:
        enabled = True

        def paper_trades(self, limit=200):
            pulls.append(limit)
            return [{"closedAt": 1, "pnl": 1.0}]

    class _Paper:
        store = _Store()

        def history_list(self):
            return []

    eng.paper = _Paper()
    eng.market_overview()
    eng.market_overview()
    assert len(pulls) == 1, "第二次应命中 15 秒缓存"
