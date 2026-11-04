"""成交历史合并：MySQL 只有近几条，本地流水文件有全部历史。

线上实测：quant_trades 表只有 3 笔（表是后来才接的），而 quant-trades.json
有 260 笔、净 +110U。只查库会把"全历史"答成 3 笔。
"""
from ai.agent_tools import trades_view

NOW = 1_800_000_000_000
DAY = 86_400_000


def test_merges_db_and_local_file_without_duplicates():
    """同一条成交在两处都有时只能算一次。"""
    shared = {"ts": NOW - DAY, "symbol": "btcusdt", "side": "SELL", "qty": 0.0012,
              "pnl": 1.0, "account": "OODD"}
    local = [shared, {"ts": NOW - 2 * DAY, "symbol": "ethusdt", "side": "BUY",
                      "qty": 1.0, "pnl": -0.5, "account": "OODD"}]
    view = trades_view([shared], local, ["OODD"], days=0, now_ms=NOW)
    assert view["allTime"]["count"] == 2, view
    assert view["trades"][0]["ts"] == NOW - DAY, "必须按时间倒序"
    assert view["source"] == "mysql+json"


def test_local_only_history_still_counts():
    """库里一条都没有时，本地文件的成交不能因此消失。"""
    local = [{"ts": NOW - i * 1000, "symbol": "btcusdt", "side": "BUY", "qty": 1.0,
              "pnl": 1.0, "account": "OODD"} for i in range(5)]
    view = trades_view([], local, ["OODD"], days=0, now_ms=NOW)
    assert view["allTime"] == {"count": 5, "wins": 5, "losses": 0, "pnl": 5.0,
                               "avgSlippageAtr": 0.0}
    assert view["source"] == "json"


def test_other_accounts_are_reported_separately():
    """换过 API Key 之前的测试盘成交不能混进当前账户的胜率/盈亏。"""
    local = [{"ts": NOW - 1000, "symbol": "btcusdt", "side": "BUY", "qty": 1.0,
              "pnl": 5.0, "account": "ODDD测试盘1"},
             {"ts": NOW - 2000, "symbol": "btcusdt", "side": "BUY", "qty": 1.0,
              "pnl": 1.0, "account": "OODD"}]
    view = trades_view([], local, ["OODD"], days=0, now_ms=NOW)
    assert view["allTime"]["count"] == 1 and view["allTime"]["pnl"] == 1.0
    assert view["byAccount"] == {"ODDD测试盘1": 1}


def test_days_window_filters_trades_and_stats():
    local = [{"ts": NOW - 1 * DAY, "symbol": "btcusdt", "side": "BUY", "qty": 1.0,
              "pnl": 2.0, "account": "OODD"},
             {"ts": NOW - 40 * DAY, "symbol": "btcusdt", "side": "SELL", "qty": 1.0,
              "pnl": -1.0, "account": "OODD"}]
    view = trades_view([], local, ["OODD"], days=7, now_ms=NOW)
    assert view["stats"]["count"] == 1 and view["stats"]["pnl"] == 2.0
    # 全历史口径不受 days 影响
    assert view["allTime"]["count"] == 2 and view["allTime"]["pnl"] == 1.0


def test_symbol_filter_matches_stats():
    local = [{"ts": NOW - 1000, "symbol": "btcusdt", "side": "BUY", "qty": 1.0,
              "pnl": 3.0, "account": "OODD"},
             {"ts": NOW - 2000, "symbol": "ethusdt", "side": "BUY", "qty": 1.0,
              "pnl": -2.0, "account": "OODD"}]
    view = trades_view([], local, ["OODD"], days=0, symbol="btc", now_ms=NOW)
    assert view["stats"]["count"] == 1 and view["stats"]["pnl"] == 3.0
    # allTime 是"当前账户全部"，不按币筛——提示词里已说明，避免两个口径混用
    assert view["allTime"]["count"] == 2


def test_limit_truncates_but_total_is_full():
    local = [{"ts": NOW - i * 1000, "symbol": "btcusdt", "side": "BUY", "qty": 1.0,
              "pnl": 0.1, "account": "OODD"} for i in range(300)]
    view = trades_view([], local, ["OODD"], days=0, limit=10, now_ms=NOW)
    assert len(view["trades"]) == 10
    assert view["total"] == 300, "要能说出总共有多少笔"


def test_slippage_average_skips_missing_values():
    local = [{"ts": NOW - 1000, "symbol": "btcusdt", "side": "BUY", "qty": 1.0,
              "pnl": 1.0, "account": "OODD", "slippageAtr": -0.2},
             {"ts": NOW - 2000, "symbol": "btcusdt", "side": "BUY", "qty": 1.0,
              "pnl": 1.0, "account": "OODD", "slippageAtr": 0.4},
             {"ts": NOW - 3000, "symbol": "btcusdt", "side": "BUY", "qty": 1.0,
              "pnl": 1.0, "account": "OODD"}]
    view = trades_view([], local, ["OODD"], days=0, now_ms=NOW)
    assert view["allTime"]["avgSlippageAtr"] == 0.3, "取绝对值平均，缺值不参与"
