"""数据互通与实时状态文案。

两件事：
1. read_pattern_stats：形态胜率前端卡片上有，Agent 之前读不到，
   被问"哪种形态胜率最高"只能回"我没有这个工具"。
2. _status_line：标题栏那句"在判断方向…"，要跟用户问题相关、不能是技术日志。
"""
import asyncio
import json

from ai.agent import _status_line
from ai.agent_tools import read_snapshot


STATS = {
    "hammer": {"count": 12, "wins": 8, "losses": 4,
               "weightedWinrate": 0.67, "windowDays": 7},
    "doji": {"count": 2, "wins": 1, "losses": 1,
             "weightedWinrate": 0.5, "windowDays": 7},
    "shooting_star": {"count": 20, "wins": 4, "losses": 16,
                      "weightedWinrate": 0.2, "windowDays": 7},
}


def test_pattern_stats_are_ranked():
    out = read_snapshot("read_pattern_stats", {}, {"ts": 1, "patternStats": STATS})
    ranked = out["data"]["ranked"]
    assert [r["pattern"] for r in ranked] == ["hammer", "doji", "shooting_star"]
    assert ranked[0]["winrate"] == 0.67
    assert ranked[0]["count"] == 12


def test_low_sample_is_flagged():
    """样本太少要说清楚，不能把一个 1胜1负 的形态当 50% 胜率推荐。"""
    out = read_snapshot("read_pattern_stats", {}, {"ts": 1, "patternStats": STATS})
    by = {r["pattern"]: r for r in out["data"]["ranked"]}
    assert by["doji"]["lowSample"] is True
    assert by["hammer"]["lowSample"] is False
    assert "样本少于 3 次" in out["data"]["note"]


def test_pattern_stats_missing_is_not_an_error():
    """没有统计数据时返回空榜，不抛异常。"""
    out = read_snapshot("read_pattern_stats", {}, {"ts": 1})
    assert out["data"]["ranked"] == []


def test_status_line_is_about_the_question():
    assert "K线" in _status_line("帮我看下BTC的K线")
    assert "方向" in _status_line("BTC现在能做多吗")
    assert "持仓" in _status_line("我现在持仓怎么样")
    assert "胜率" in _status_line("哪种形态胜率最高")


def test_status_line_for_tools():
    assert "K线" in _status_line("btcusdt 怎么样", "read_candles")
    assert "系统建议" in _status_line("该做多吗", "read_advice")
    assert "胜率" in _status_line("哪种形态好", "read_pattern_stats")


def test_status_line_has_no_technical_jargon():
    """给用户看的短句里不能出现轮次、tool_calls 这类内部词。"""
    for prompt in ("BTC能做吗", "分析一下", "你好", "胜率最高的形态"):
        for tool in ("", "read_candles", "read_advice"):
            line = _status_line(prompt, tool, 3)
            for word in ("tool", "round", "轮", "null", "None"):
                assert word not in line, f"{line!r} 含内部术语 {word}"


def test_status_line_reaches_the_client(tmp_path, monkeypatch):
    """端到端：进行中的消息要带上 progress.status。"""
    from tests.test_agent import SlowHTTP, make_manager, wait_for

    slow = SlowHTTP()
    seen = []

    async def scenario():
        manager, events = make_manager(tmp_path, monkeypatch, provider=slow)
        try:
            await manager.ask("BTC现在能做多吗", "test-model")
            assert await wait_for(lambda: slow.active == 1)
            for event in events:
                for m in event.get("chat") or []:
                    if m.get("status") == "running" and (m.get("progress") or {}).get("status"):
                        seen.append(m["progress"]["status"])
            assert seen, "进行中的消息没有 status 文案"
            assert any("方向" in s for s in seen), seen
        finally:
            slow.release.set()
            await manager.stop()
    asyncio.run(scenario())
