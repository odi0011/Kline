import asyncio
import json

import websockets

from server.hub import BroadcastHub


async def receive_type(ws, kind):
    while True:
        message = json.loads(await asyncio.wait_for(ws.recv(), 2))
        if message.get("type") == kind:
            return message


def test_agent_requires_token_and_keeps_state_private():
    async def scenario():
        hub = BroadcastHub({}, [], [])
        hub.agent_token = "test-token-" + "x" * 40
        hub.agent_state_fn = lambda: {"tasks": [{"id": "private"}], "chat": []}
        calls = []

        async def action(kind, payload):
            calls.append(kind)
            return {"ok": True, "id": "created"}

        hub.agent_fn = action
        async with websockets.serve(hub._handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with websockets.connect(f"ws://127.0.0.1:{port}") as owner:
                assert (await receive_type(owner, "agent_access"))["locked"]
                await owner.send(json.dumps({"type": "agent_action", "action": "create", "requestId": "1"}))
                denied = await receive_type(owner, "agent_ack")
                assert not denied["result"]["ok"] and denied["requestId"] == "1"
                assert calls == []
                await owner.send(json.dumps({"type": "agent_auth", "token": hub.agent_token, "requestId": "2"}))
                assert (await receive_type(owner, "agent_auth_ack"))["result"]["ok"]
                assert (await receive_type(owner, "agent_state"))["tasks"][0]["id"] == "private"
                async with websockets.connect(f"ws://127.0.0.1:{port}") as visitor:
                    await receive_type(visitor, "agent_access")
                    await hub.broadcast({"type": "agent_state", "tasks": [{"id": "updated"}], "chat": []})
                    assert (await receive_type(owner, "agent_state"))["tasks"][0]["id"] == "updated"
                    await visitor.send(json.dumps({"type": "query_news"}))
                    assert json.loads(await visitor.recv())["type"] == "news"
                await owner.send(json.dumps({"type": "agent_action", "action": "create", "requestId": "3", "payload": {}}))
                assert (await receive_type(owner, "agent_ack"))["result"]["ok"]
                assert calls == ["create"]
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


def test_cross_origin_token_is_rejected():
    async def scenario():
        hub = BroadcastHub({}, [], [])
        hub.agent_token = "x" * 40
        hub.agent_state_fn = lambda: {"tasks": [], "chat": []}
        async with websockets.serve(hub._handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with websockets.connect(f"ws://127.0.0.1:{port}", origin="https://untrusted.invalid") as ws:
                await receive_type(ws, "agent_access")
                await ws.send(json.dumps({"type": "agent_auth", "token": hub.agent_token}))
                assert not (await receive_type(ws, "agent_auth_ack"))["result"]["ok"]
    asyncio.run(scenario())
