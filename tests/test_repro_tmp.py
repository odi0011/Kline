import asyncio, json, tempfile, pathlib
from tests.test_agent import make_manager, tool_call, tool_result, wait_for
from ai.sticker_cache import StickerCache

def test_repro_three_rounds(tmp_path, monkeypatch):
    rounds = {"n": 0}
    def provider(*args, **kwargs):
        rounds["n"] += 1
        n = rounds["n"]
        if n == 1:
            return tool_result(tool_call("list_stickers", {}, "c1"))
        if n == 2:
            return tool_result(tool_call("send_sticker", {"sticker_id": "S9r"}, "c2"))
        return {"ok": True, "reply": "摊手。", "usage": {"total_tokens": 5}, "model": "test-model"}

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch, provider=provider)
        cache = StickerCache(str(tmp_path / "s"))
        cache.save("S9r", b"GIF89a" + b"x"*16, 480, 480,
                   url_hint="https://media.giphy.com/a.gif", content_type="image/gif")
        manager.sticker_cache = cache
        await manager.ask("来个摊手", "test-model")
        await wait_for(lambda: not manager.chat_runners)
        ans = [m for m in manager.chat if m.get("role")=="assistant"][-1]
        print("CONTENT:", json.dumps(ans.get("content"), ensure_ascii=False))
        print("STEPS:", [(s.get("tool"), s.get("status")) for s in ans.get("steps") or []])
        await manager.stop()

    asyncio.run(scenario())
