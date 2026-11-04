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


def test_read_accounts_trades_newest_first_and_lean():
    """成交明细必须最新在前，且不能因为超预算被截成 excerpt。

    线上实测：20 条全字段成交撑爆 12000 预算，bounded() 从末尾砍，
    最新的那笔正好没了——Agent 只能拿聊天历史答"止损挂上了没"。
    """
    now = int(time.time() * 1000)
    trades = [{"ts": now - i * 1000, "symbol": "btcusdt", "side": "BUY",
               "qty": 0.0045, "entry": 80000 + i, "exit": 80100 + i, "pnl": 1.2,
               "movePct": 0.5, "reason": "移动止盈", "entryTs": now - i * 1000 - 60_000,
               "holdMin": 12.0, "pattern": "bullish_engulfing",
               # 排障字段：占长度但对回答没用
               "orderId": 12345678901, "closeId": 12345678902,
               "slippageAtr": 0.11, "entrySlippageAtr": 0.22, "atrAtEntry": 45.5}
              for i in range(20)]
    snap = {"ts": now, "accounts": [{"name": "O", "balance": 7352.95, "positions": [],
                                     "error": None}],
            "positions": [], "trades": trades}
    data = read_snapshot("read_accounts", {}, snap)["data"]
    assert "truncated" not in data, "账户数据不能被整段截断"
    assert data["trades"][0]["ts"] == now, "最新一笔必须在第一个"
    assert "orderId" not in data["trades"][0], "排障字段不该占预算"
    assert data["accounts"][0]["balance"] == 7352.95


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


def test_winrate_denominator_excludes_draws_and_timeouts():
    """胜率分母只算有明确输赢的笔数，不能把平局/超时算进去。

    线上实测 ada 936 笔里只有 3 笔有输赢（其余是平价离场/超时），
    按 closed 做分母得到 0.2%，看着像"几乎全输"，实际是 2 胜 1 负。
    """
    from decision.paper import PaperBook
    book = PaperBook()
    # 造 6 笔：2 胜 1 负 + 3 笔平价（pnl 恰好 0）
    book.history = [
        {"symbol": "adausdt", "pnl": 1.0, "reason": "tp"},
        {"symbol": "adausdt", "pnl": 0.5, "reason": "tp"},
        {"symbol": "adausdt", "pnl": -1.0, "reason": "sl"},
        {"symbol": "adausdt", "pnl": 0.0, "reason": "manual"},
        {"symbol": "adausdt", "pnl": 0.0, "reason": "manual"},
        {"symbol": "adausdt", "pnl": 0.0, "reason": "timeout"},
    ]
    summary = book.summary()
    row = summary["bySymbol"][0]
    assert row["closed"] == 6
    assert row["wins"] == 2 and row["losses"] == 1
    assert row["decided"] == 3
    # 2/3 = 66.7%，不是 2/6 = 33.3%
    assert row["winrate"] == 66.7, row


def test_session_winrate_uses_decided_denominator():
    from decision.paper import PaperBook
    book = PaperBook()
    book.stats.update(closed=10, wins=3, losses=2, timeouts=5)
    session = book.summary()["session"]
    assert session["decided"] == 5
    assert session["winrate"] == 60.0, session


# ---------- 历史必须能在截断预算内读到 ----------

def test_recent_closed_survives_payload_trim():
    """recentClosed 不能被 bounded() 截掉。

    线上实测：未平仓明细写在前面，把 recentClosed 挤成 excerpt，
    Agent 于是回答"查不到明细，没有可列的平仓记录"。
    """
    book = _book_with_history()
    snap = _snapshot(book)
    # 造 16 个未平仓仓位，撑大体积
    snap["paper"] = [
        {"symbol": f"coin{i}usdt", "direction": "long", "entry": 1.0, "mark": 1.0,
         "leverage": 20, "notional": 1000, "margin": 50, "sl": 0.9, "tp": 1.1,
         "liq": 0.8, "upnl": 0.0, "upnl_pct": 0.0, "winrate": 55.0, "score": 60,
         "grade": "medium", "reason": "测试信号", "trailing": False,
         "entry_ts": 1, "expires_ts": 2}
        for i in range(16)
    ]
    out = read_snapshot("read_paper", {}, snap)
    data = out["data"]
    assert "truncated" not in data, "不该被截断"
    assert data["recentClosed"], "最近平仓记录必须读得到"
    assert data["realized"]["bySymbol"], "按币汇总必须在"


def test_paper_payload_order_is_summary_first():
    """字段顺序：汇总和 recentClosed 在前，未平仓明细在后。"""
    book = _book_with_history()
    out = read_snapshot("read_paper", {}, _snapshot(book))
    keys = list(out["data"].keys())
    assert keys.index("realized") < keys.index("openPositions")
    assert keys.index("recentClosed") < keys.index("openPositions")


# ---------- "最近 N 笔"必须按币查，不能靠全市场窗口 ----------

def _stored_book():
    """造 6 笔 BTC + 10 笔 ETH 的平仓流水，顺序打乱（ETH 更新）。"""
    book = PaperBook()
    now = int(time.time() * 1000)
    for i in range(6):
        book.history.append({"symbol": "btcusdt", "direction": "long", "entry": 100.0 - i,
                             "exit": 101.0 - i, "pnl": 1.0, "pnlPct": 1.0,
                             "reason": "tp", "closedAt": now - (100 - i) * 1000})
    for i in range(10):
        book.history.append({"symbol": "ethusdt", "direction": "short", "entry": 10.0,
                             "exit": 9.9, "pnl": 0.5, "pnlPct": 0.5,
                             "reason": "sl", "closedAt": now - i * 1000})
    book.history.sort(key=lambda r: r["closedAt"])    # 内存里保持时间升序
    return book


def test_recent_trades_filters_by_symbol():
    book = _stored_book()
    got = book.recent_trades(5, "BTC")
    assert got["source"] == "memory"
    assert len(got["rows"]) == 5, "BTC 有 6 笔，limit=5 应给 5 笔"
    assert {r["symbol"] for r in got["rows"]} == {"btcusdt"}
    # 最新在前：closedAt 降序
    stamps = [r["closedAt"] for r in got["rows"]]
    assert stamps == sorted(stamps, reverse=True)
    # 不传 symbol 就是全市场
    assert len(book.recent_trades(3, "")["rows"]) == 3


def test_recent_trades_normalizes_symbol_forms():
    """模型会写 BTC / btc-usdt / BTCUSDT，都要命中库里的 btcusdt。"""
    book = _stored_book()
    for form in ("BTC", "btc", "BTC-USDT", "btcusdt", "BTCUSDT"):
        assert len(book.recent_trades(10, form)["rows"]) == 6, form


def test_read_paper_uses_history_fn_for_symbol_and_limit():
    """read_paper 带 symbol/limit 时要走现查，而不是快照里的 12 条窗口。"""
    book = _stored_book()
    snap = _snapshot(book)
    # 快照窗口里只有 ETH（模拟"BTC 被别的币挤出窗口"）
    snap["paperHistory"] = [
        {"symbol": "ethusdt", "pnl": 0.1, "reason": "sl", "closedAt": 1},
    ]
    seen = {}

    def history_fn(limit, symbol):
        seen["args"] = (limit, symbol)
        return book.recent_trades(limit, symbol)

    out = read_snapshot("read_paper", {"symbol": "BTC", "limit": 5}, snap, history_fn)
    data = out["data"]
    assert seen["args"] == (5, "BTC")
    assert len(data["recentClosed"]) == 5, "必须按币现查到 5 笔 BTC"
    assert {r["symbol"] for r in data["recentClosed"]} == {"btcusdt"}
    assert data["recentClosedMeta"] == {"order": "newestFirst", "source": "memory",
                                       "total": 6}


def test_read_paper_falls_back_when_history_fn_fails():
    """查库失败不能把整次读取打断，退回快照窗口。"""
    book = _book_with_history()
    snap = _snapshot(book)

    def boom(limit, symbol):
        raise RuntimeError("db down")

    out = read_snapshot("read_paper", {"symbol": "BTC"}, snap, boom)
    assert out["available"] is True
    assert out["data"]["recentClosedMeta"]["source"] == "snapshotWindow"


def test_read_paper_rejects_bad_limit():
    book = _book_with_history()
    for bad in (0, 101, "5", True, 2.5):
        try:
            read_snapshot("read_paper", {"limit": bad}, _snapshot(book))
        except ValueError:
            continue
        raise AssertionError(f"limit={bad!r} 应该被拒绝")


def test_read_market_rejects_limit():
    """limit 只属于 read_paper，别让它悄悄变成 read_market 的参数。"""
    try:
        read_snapshot("read_market", {"limit": 5}, {"market": {}})
    except ValueError:
        return
    raise AssertionError("read_market 不该接受 limit")


def test_large_history_trims_tail_not_summary():
    """明细太多时从**最旧**截掉，汇总必须完整可读。

    不能像以前那样整段变成 {"truncated": true, "excerpt": "..."}——
    那等于什么都读不到。limit 上限 100（clean 的列表裁剪也是 100）。
    """
    book = _stored_book()
    snap = _snapshot(book)
    now = int(time.time() * 1000)
    # 用长 reason 把 payload 撑过 24000 字符预算
    rows = [{"symbol": "btcusdt", "direction": "long", "entry": 100.0, "exit": 101.0,
             "pnl": 1.0, "pnlPct": 1.0, "reason": "tp:" + "x" * 400,
             "score": 70, "grade": "medium", "closedAt": now - i * 1000}
            for i in range(100)]

    out = read_snapshot("read_paper", {"symbol": "BTC", "limit": 100}, snap,
                        lambda limit, symbol: {"rows": rows, "source": "mysql", "total": 100})
    data = out["data"]
    assert "truncated" not in data, "不能整段被截成 excerpt"
    assert data["realized"]["bySymbol"], "汇总必须在"
    assert data["recentClosed"], "明细至少留得下最新的几条"
    assert data["recentClosed"][0]["closedAt"] == now, "留下的是最新的那些"
    meta = data["recentClosedMeta"]
    assert meta["omitted"] == 100 - len(data["recentClosed"]) > 0
    assert meta["total"] == 100


def test_recent_closed_explains_stop_exit_semantics():
    """reason=sl 也可能是盈利单（移动止损把止损推到成本之上）。

    线上实测：Agent 看到"盈利单标成止损"就断定"是记录口径问题"，
    其实是 trailing 生效后的正常结果——工具 note 必须说清楚，
    明细里也要带 trailing 字段，否则模型只能瞎猜。
    """
    book = _stored_book()
    snap = _snapshot(book)
    rows = [{"symbol": "btcusdt", "direction": "long", "entry": 100.0, "exit": 105.0,
             "pnl": 2.0, "pnlPct": 4.0, "reason": "sl", "trailing": True,
             "closedAt": int(time.time() * 1000)}]
    out = read_snapshot("read_paper", {"symbol": "BTC"}, snap,
                        lambda limit, symbol: {"rows": rows, "source": "mysql", "total": 1})
    row = out["data"]["recentClosed"][0]
    assert row["reason"] == "sl" and row["trailing"] is True
    note = out["data"]["note"]
    assert "移动止损" in note and "也可能是盈利单" in note


def test_prompt_requires_fresh_state_lookup():
    """提示词要禁止拿聊天历史当现状。

    线上实测：那笔空单早就平了、止损也清掉了，Agent 仍说
    "实盘那笔空单的止损还没挂上"——引用的是几轮前的旧状态。
    """
    from ai.agent import SYSTEM_PROMPT
    for phrase in ("现状类问题必须现查", "不能引用聊天历史",
                   "先调工具现查", "那笔单可能早就平了"):
        assert phrase in SYSTEM_PROMPT, f"缺少: {phrase}"
