import asyncio
import json

import websockets

from server.hub import BroadcastHub


async def receive_type(ws, kind):
    while True:
        message = json.loads(await asyncio.wait_for(ws.recv(), 2))
        if message.get("type") == kind:
            return message


def test_agent_state_is_pushed_and_actions_run_without_token():
    async def scenario():
        hub = BroadcastHub({}, [], [])
        hub.agent_state_fn = lambda: {"tasks": [{"id": "visible"}], "chat": []}
        calls = []

        async def action(kind, payload):
            calls.append(kind)
            return {"ok": True, "id": "created"}

        hub.agent_fn = action
        async with websockets.serve(hub._handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                assert (await receive_type(ws, "agent_state"))["tasks"][0]["id"] == "visible"
                await ws.send(json.dumps({"type": "agent_action", "action": "create", "requestId": "1", "payload": {}}))
                ack = await receive_type(ws, "agent_ack")
                assert ack["result"]["ok"] and ack["requestId"] == "1"
                assert calls == ["create"]
                await hub.broadcast({"type": "agent_state", "tasks": [{"id": "updated"}], "chat": []})
                assert (await receive_type(ws, "agent_state"))["tasks"][0]["id"] == "updated"
    asyncio.run(scenario())


def test_article_only_fetches_known_source_and_caches():
    async def scenario():
        hub = BroadcastHub({}, [], [])
        item = {"url": "https://www.binance.com/known", "title": "Known"}
        hub.news = [item]
        calls = []

        def article(source):
            calls.append(source)
            return {"ok": True, "content": "Article body", "partial": False}

        hub.news_article_fn = article
        async with websockets.serve(hub._handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await receive_type(ws, "news")
                await ws.send(json.dumps({"type": "query_news_article", "url": "http://127.0.0.1/secret", "requestId": "x"}))
                assert not (await receive_type(ws, "news_article"))["result"]["ok"]
                assert not calls
                for request_id in ("y", "z"):
                    await ws.send(json.dumps({"type": "query_news_article", "url": item["url"], "requestId": request_id}))
                    result = await receive_type(ws, "news_article")
                    assert result["requestId"] == request_id and result["result"]["content"] == "Article body"
                assert calls == [item]
    asyncio.run(scenario())


def test_cross_origin_agent_action_is_rejected():
    async def scenario():
        hub = BroadcastHub({}, [], [])
        hub.agent_state_fn = lambda: {"tasks": [], "chat": []}
        calls = []

        async def action(kind, payload):
            calls.append(kind)
            return {"ok": True}

        hub.agent_fn = action
        async with websockets.serve(hub._handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with websockets.connect(f"ws://127.0.0.1:{port}", origin="https://untrusted.invalid") as ws:
                await receive_type(ws, "agent_state")
                await ws.send(json.dumps({"type": "agent_action", "action": "create", "requestId": "x"}))
                assert not (await receive_type(ws, "agent_ack"))["result"]["ok"]
                assert calls == []
    asyncio.run(scenario())
