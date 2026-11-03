"""账户（实盘）与模拟仓是两套数据，Agent 不能混着读。

曾经的 bug：context 里只有账户权益/持仓/成交，没有模拟仓，
Agent 被问"哪个模拟仓胜率最高"就拿账户名和账户成交去答——
把实盘账户名当成了模拟仓，把实盘成交当成了模拟仓战绩。
"""
import asyncio
import json
import time

from ai.agent import SYSTEM_PROMPT, _status_line
from ai.agent_tools import TOOL_NAMES, read_snapshot
from decision.paper import PaperBook
from tests.test_agent import finished, make_manager, tool_call, tool_result


def ctx(price=100.0, direction="bullish", score=70, gap=20):
    return {"price": price, "atr": 2.0, "score": score, "gap": gap,
            "grade": "medium", "direction": direction, "reason": "测试",
            "regime": "trend", "pattern": "", "patternStats": {}, "maxLeverage": 20}


def _book_with_history():
    """造两个币的平仓记录：btc 两胜，eth 一胜一负。"""
    book = PaperBook()
    book.tick("btcusdt", ctx())
    book.tick("btcusdt", ctx(price=book.positions["btcusdt"].tp + 1))
    book.tick("btcusdt", ctx(price=book.positions["btcusdt"].tp + 2))
    book.tick("ethusdt", ctx())
    book.tick("ethusdt", ctx(price=book.positions["ethusdt"].sl - 1))
    return book


def test_summary_ranks_by_realized_winrate():
    summary = _book_with_history().summary()
    by = {row["symbol"]: row for row in summary["bySymbol"]}
    assert by["btcusdt"]["closed"] == 2 and by["btcusdt"]["wins"] == 2
    assert by["btcusdt"]["winrate"] == 100.0
    assert by["ethusdt"]["losses"] == 1 and by["ethusdt"]["winrate"] == 0.0
    # 胜率高的在前
    assert summary["bySymbol"][0]["symbol"] == "btcusdt"
    # 窗口口径（bySymbol）与进程累计（session）分开报，不能混成一个数
    assert summary["session"]["closed"] == 3 and summary["session"]["wins"] == 2
    assert summary["window"] == {"records": 3, "limit": 200}
    assert "不产生真实委托" in summary["note"]


def test_summary_ignores_open_positions_only():
    """只开了仓、还没平：没有胜率可言，不能编一个上来。"""
    book = PaperBook()
    book.tick("btcusdt", ctx())
    summary = book.summary()
    assert summary["bySymbol"] == []
    assert summary["session"]["winrate"] is None
    assert summary["openCount"] == 1


def _snapshot(book):
    now = int(time.time() * 1000)
    return {
        "ts": now,
        "accounts": [{"name": "ODDD测试盘1", "balance": 5200, "positions": []}],
        "positions": [{"symbol": "adausdt", "side": "BUY", "qty": 10, "entry": 0.5}],
        "trades": [{"symbol": "adausdt", "pnl": 0.6}],
        "paper": [{"symbol": "btcusdt", "direction": "long", "entry": 100.0,
                   "mark": 101.0, "leverage": 20, "notional": 1000, "margin": 50,
                   "sl": 98, "tp": 105, "liq": 95, "upnl": 10, "upnl_pct": 20,
                   "winrate": 61.5, "score": 72, "grade": "medium",
                   "reason": "medium 信号", "trailing": False,
                   "entry_ts": now - 60_000, "expires_ts": now + 840_000}],
        "paperStats": {"opened": 3, "closed": 3, "wins": 2, "losses": 1,
                       "timeouts": 0, "netPnl": 1.6},
        "paperHistory": [{"symbol": "btcusdt", "pnl": 0.8, "reason": "tp",
                          "closedAt": now - 1000}],
        "paperSummary": book.summary(),
    }


def test_read_paper_returns_positions_and_realized_winrate():
    book = _book_with_history()
    book.tick("solusdt", ctx())
    out = read_snapshot("read_paper", {}, _snapshot(book))
    data = out["data"]
    assert out["available"] is True
    assert data["openPositions"], "未平仓的模拟仓要能看到"
    open_btc = next(p for p in data["openPositions"] if p["symbol"] == "btcusdt")
    assert open_btc["estimatedWinrate"] == 61.5
    assert open_btc["heldSec"] is not None and open_btc["expiresInSec"] > 0
    # 历史胜率来自 realized，不是仓位上的估算值
    assert data["realized"]["bySymbol"][0]["winrate"] == 100.0
    # 账户数据不能混进来
    assert "accounts" not in data and "positions" not in data and "trades" not in data


def test_read_paper_symbol_filter():
    book = _book_with_history()
    out = read_snapshot("read_paper", {"symbol": "ETH"}, _snapshot(book))
    rows = out["data"]["realized"]["bySymbol"]
    assert [r["symbol"] for r in rows] == ["ethusdt"]
    assert out["symbol"] == "ETH"
    # 按币种筛过时，小计也得是同一个口径：只有 ETH 那一笔
    subtotal = out["data"]["realized"]["subtotal"]
    assert subtotal["closed"] == 1 and subtotal["winrate"] == 0.0
    assert "sessionAllSymbols" not in out["data"]["realized"], \
        "按币种筛过就不该再给全市场的累计值"
    assert out["data"]["best"]["symbol"] == "ethusdt"


def test_read_accounts_does_not_expose_paper():
    """问账户就是账户，模拟仓的数据一点都不能混进 read_accounts。"""
    out = read_snapshot("read_accounts", {}, _snapshot(PaperBook()))
    data = out["data"]
    assert data["accounts"][0]["name"] == "ODDD测试盘1"
    assert "paper" not in data and "paperSummary" not in data
    assert "模拟仓" in data["note"]


def test_read_paper_missing_data_is_empty_not_error():
    out = read_snapshot("read_paper", {}, {"ts": 1})
    assert out["available"] is False
    assert out["data"]["openPositions"] == []
    assert out["data"]["best"]["basis"] is None


def test_best_answer_prefers_realized_over_estimated():
    """'哪个模拟仓胜率最高'必须按已平仓实际胜率答，不能被估算概率抢走。"""
    book = _book_with_history()
    out = read_snapshot("read_paper", {}, _snapshot(book))
    best = out["data"]["best"]
    assert best["basis"] == "realized"
    assert best["symbol"] == "btcusdt"
    assert best["winrate"] == 100.0


def test_best_answer_falls_back_to_estimated_when_nothing_closed():
    book = PaperBook()
    out = read_snapshot("read_paper", {}, _snapshot(book))
    best = out["data"]["best"]
    assert best["basis"] == "estimated"
    assert best["symbol"] == "btcusdt"
    assert best["winrate"] == 61.5 and best["closed"] == 0


def test_prompt_separates_account_and_paper():
    for phrase in ("账户 vs 模拟仓", "read_paper", "estimatedWinrate",
                   "账户身份始终是实盘"):
        assert phrase in SYSTEM_PROMPT, f"系统提示缺少: {phrase}"


def test_read_paper_is_a_real_tool():
    assert "read_paper" in TOOL_NAMES


def test_status_line_for_paper():
    assert "模拟仓" in _status_line("哪个模拟仓胜率最高", "read_paper")
    assert "模拟仓" in _status_line("模拟仓现在怎么样")


def test_agent_dispatches_read_paper_end_to_end(tmp_path, monkeypatch):
    """端到端：模型调 read_paper 时，工具真的被派发、真的拿到模拟仓数据。

    这条覆盖 dispatch 的 allowlist：read_paper 不在"免快照"名单里，
    必须采集 snapshot 才读得到；漏掉就会静默走成快照缺失错误。
    """
    book = _book_with_history()
    snap = _snapshot(book)
    captured = []

    def provider(*args, **kwargs):
        captured.append(json.dumps(args[2], ensure_ascii=False))
        if kwargs.get("tool_choice") == "required":
            return tool_result(tool_call("read_paper", {"symbol": "BTC"}, "p1"))
        return {"ok": True, "reply": "模拟仓 BTC 样本 2 笔全胜。",
                "usage": {"total_tokens": 20}, "model": "test-model"}

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch, context=lambda: snap, provider=provider)
        ack = await manager.handle("create", {"prompt": "哪个模拟仓胜率最高"})
        task = manager._find(ack["task"]["id"])
        await finished(manager, task)
        assert task["status"] == "succeeded", task
        step = task["steps"][0]
        assert step["tool"] == "read_paper" and step["status"] == "succeeded"
        # steps 里存的是给界面看的预览（超长会截成 excerpt）；
        # 模型真正收到的是第二轮请求里的 tool 消息，断言那一份。
        sent = captured[-1]
        assert "btcusdt" in sent
        assert "estimatedWinrate" in sent and "61.5" in sent
        assert "realized" in sent
        assert "ODDD测试盘1" not in sent, "模拟仓结果里不能混进账户名"
        assert "adausdt" not in sent, "模拟仓结果里不能混进账户持仓"
        await manager.stop()

    asyncio.run(scenario())
