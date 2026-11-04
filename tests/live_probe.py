"""线上 Agent 体检：以真实用户口吻提问，记录每一步工具返回与最终回答。

用法：python tests/live_probe.py "问题1" "问题2" ...
只看结论不看实现：每条打印工具名、参数、是否被截断、最终回答。
"""
import asyncio
import json
import sys
import uuid

import websockets

HOST = "ws://154.36.184.56:8765"


async def ask(ws, prompt):
    rid = uuid.uuid4().hex
    await ws.send(json.dumps({"type": "agent_action", "action": "chat", "requestId": rid,
                              "payload": {"prompt": prompt, "model": ""}}))
    mid = None
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=300))
        if msg.get("type") == "agent_ack" and msg.get("requestId") == rid:
            mid = msg["result"]["id"]
        if msg.get("type") == "agent_state":
            chat = msg.get("chat") or []
            idx = next((i for i, c in enumerate(chat) if c.get("id") == mid), None)
            if idx is not None and chat[idx].get("status") in ("succeeded", "failed", "error"):
                steps = []
                for s in chat[idx].get("steps") or []:
                    res = s.get("result") or {}
                    steps.append({
                        "tool": s.get("tool"),
                        "args": s.get("arguments"),
                        "status": s.get("status"),
                        "truncated": bool(isinstance(res, dict) and res.get("truncated")),
                        "keys": list((res.get("data") or {}).keys())
                                if isinstance(res.get("data"), dict) else None,
                        "error": s.get("error"),
                    })
                bubbles = [c.get("content") or "" for c in chat[idx:]
                           if c.get("role") == "assistant"]
                return steps, bubbles, chat[idx].get("status")


async def main(prompts):
    async with websockets.connect(HOST, open_timeout=15) as ws:
        for prompt in prompts:
            try:
                steps, bubbles, status = await ask(ws, prompt)
            except Exception as exc:
                print(f"\n### {prompt}\n!! {type(exc).__name__}: {exc}")
                continue
            print(f"\n### {prompt}  [{status}]")
            for st in steps:
                print(f"  · {st['tool']}({st['args']}) {st['status']}"
                      f"{' TRUNCATED' if st['truncated'] else ''}"
                      f"{' keys=' + ','.join(st['keys']) if st['keys'] else ''}"
                      f"{' err=' + str(st['error']) if st['error'] else ''}")
            for b in bubbles:
                print("  →", b.replace("\n", "\n    "))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
