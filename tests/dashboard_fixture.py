import asyncio
import math
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.candle import Candle
from server.hub import BroadcastHub

TOKEN = "local-browser-test-token-not-for-production"


async def main():
    symbols = ["btcusdt", "ethusdt", "bnbusdt", "solusdt"]
    prices = [68420, 2480, 586, 171]
    intervals = ["1m", "3m"]
    timestamp = int(time.time() * 1000)
    start = timestamp // 180000 * 180000 - 200 * 180000
    aggs = {}
    for symbol, price in zip(symbols, prices):
        for interval in intervals:
            history = []
            for i in range(200):
                value = price * (1 + .007 * math.sin(i / 8) + i * .00002)
                history.append(Candle(start + i * 180000, value, value * 1.0015,
                                      value * .998, value * (1 + .0008 * math.sin(i)),
                                      40 + i % 20, interval=interval, closed=True,
                                      buy_vol=25, sell_vol=17))
            aggs[symbol, interval] = SimpleNamespace(history=history, current=None)
    hub = BroadcastHub(aggs, symbols, intervals)
    hub.agent_token = TOKEN
    snapshot = {
        "enabled": True, "ts": timestamp, "stats": {"signals": 286, "opens": 42, "closes": 42},
        "events": [], "positions": [], "accounts": ["演示验证账户"],
        "analysis": {
            "symbols": {symbol: {"price": price, "bars": 200, "atr": price * .006,
                "atrPct": .6, "rsi": 61.4, "adx": 32.8, "z": 1.22, "roc": .82,
                "regime": "trend", "volState": "normal", "boll": {"upper": price * 1.02, "mid": price, "lower": price * .98}}
                for symbol, price in zip(symbols, prices)},
            "liveFeatures": {},
            "liveEvaluations": {symbol: {"ts": timestamp, "direction": "bullish", "score": 72 + i,
                "oppositeScore": 31, "gap": 41 + i, "grade": "A", "confidence": .82,
                "breakdown": {}, "reasons": [], "vetoes": []} for i, symbol in enumerate(symbols)},
            "drawdown": {"current": -1.28, "max": -2.1, "peak": 12000, "paused": False, "limit": -5},
            "params": {"calcIntervalSec": 5, "minScore": 65, "minScoreGap": 12, "riskPerTradePct": .8,
                "atrSlMult": 1.5, "atrTpMult": 2.8, "maxPositions": 4, "maxSameDir": 2, "ddLimitPct": -5},
            "liveScoreHistory": {s: [{"ts": timestamp - (40 - i) * 5000, "score": 58 + i / 3 + math.sin(i)}
                                      for i in range(40)] for s in symbols},
            "positionPlans": [], "gateLog": [], "decisionTraces": [],
        },
    }
    day = 86400000
    today = timestamp // day * day
    history = [{"date": time.strftime("%Y-%m-%d", time.gmtime((today - (280 - i) * day) / 1000)), "t": today - (280 - i) * day,
                "open": 9200 + i * 8, "close": 9240 + i * 8 + math.sin(i / 7) * 180,
                "high": 9400 + i * 8, "low": 9100 + i * 8,
                "firstTs": today - (280 - i) * day, "lastTs": today - (280 - i) * day + 60000}
               for i in range(281) if i % 17 != 0]
    hub.last_accounts = [{"name": "演示验证账户", "id": "ui-fixture", "balance": 11480.32,
                          "unrealized": 0, "available": 11480.32, "positions": [], "equityHistory": history,
                          "series": [{"t": timestamp - (100 - i) * 30000, "bal": 11400 + i + math.sin(i / 5) * 20, "upnl": 0}
                                     for i in range(101)]}]
    hub.settings_fn = lambda: {"ai": {"models": [{"id": "fixture", "label": "验证模型", "active": True}]},
                              "ui": {"font": "system"}}
    hub.quant_fn = lambda: snapshot
    hub.quant_events_fn = lambda account, limit: [
        {"id": i, "ts": timestamp - (70 - i) * 60000, "kind": "skip", "symbol": symbols[i % 4],
         "reason": "等待评分确认", "account": "演示验证账户"} for i in range(70)]
    hub.news = [{"url": f"https://www.binance.com/ui-fixture-{i}", "source": "币安" if i % 2 else "CoinDesk",
                 "title": f"测试新闻 {i:02d} · 市场流动性与风险观察", "ts": timestamp - (80 - i) * 60000,
                 "desc": "仅用于浏览器回归测试的新闻摘要，不是真实新闻。"} for i in range(80)]
    hub.news_article_fn = lambda item: {"ok": True, "partial": False,
        "content": "测试正文：本段用于验证站内新闻阅读、滚动与 AI 解读。\n\n" +
                   "市场数据仅为测试夹具，不构成实际行情或投资建议。" * 35}
    tasks, chat = [], []
    hub.agent_state_fn = lambda: {"tasks": tasks, "chat": chat}

    async def action(kind, payload):
        if kind == "create":
            task = {"id": str(len(tasks) + 1), "title": payload.get("title", "分析任务"),
                    "prompt": payload.get("prompt", ""), "status": "running", "kind": "once", "createdAt": timestamp,
                    "runCount": 0, "steps": [], "result": "", "intervalSec": payload.get("intervalSec", 0)}
            tasks.insert(0, task)
            await hub.broadcast({"type": "agent_state", "tasks": tasks, "chat": chat})

            async def finish():
                await asyncio.sleep(.5)
                task.update(status="succeeded", result="测试分析完成：事实与推断已区分，请核查数据时效。", runCount=1)
                await hub.broadcast({"type": "agent_state", "tasks": tasks, "chat": chat})
            asyncio.create_task(finish())
            return {"ok": True, "task": task}
        return {"ok": True}

    hub.agent_fn = action
    async with websockets.serve(hub._handler, "127.0.0.1", 8877):
        print("UI fixture ready on 8877", flush=True)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
