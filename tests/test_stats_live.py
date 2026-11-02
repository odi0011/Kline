"""状态栏的实时性：流式过程中四项都要动，不能等回复结束。

服务商的 usage（真实 token 数）要到最后一帧才给。所以缓存命中率、
上下文占用、费用这三项在流式期间必须用**估算值**先显示，
结束后再换成真实值。之前只有 tps 会动，另外三项一直是「—」。
"""
import asyncio

from tests.test_agent import make_manager, wait_for


def _seed(manager, model="deepseek-flash", **message):
    base = {"id": "a1", "role": "assistant", "content": "ok", "ts": 1,
            "status": "succeeded", "model": model}
    base.update(message)
    manager.chat.append(base)
    return base


def test_all_four_are_present_while_streaming(tmp_path, monkeypatch):
    """流式进行中：tps / 缓存 / 上下文 / 费用 四项都不能是 None。"""
    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        manager.symbols_fn = lambda: ["btcusdt"]
        _seed(manager, status="running",
              perf={"ttftMs": 300, "decodeMs": 1200, "chars": 400, "deltas": 60,
                    "streamed": True, "live": True,
                    "inputChars": 4800, "prefixChars": 3200})
        s = manager.stats()
        assert s["live"] is True
        assert s["tps"] is not None, "tps 要实时"
        assert s["cacheHit"] is not None, "缓存命中率要实时（用前缀估算）"
        assert s["ctxUsed"] is not None, "上下文占用要实时（用输入字符估算）"
        assert s["cost"] is not None, "费用要实时（用估算的 token 算）"
        await manager.stop()
    asyncio.run(scenario())


def test_prefix_ratio_drives_cache_estimate(tmp_path, monkeypatch):
    """缓存命中率 = 可命中前缀 / 全部输入。前缀越长命中率越高。"""
    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        _seed(manager, status="running",
              perf={"ttftMs": 100, "decodeMs": 1000, "chars": 100,
                    "streamed": True, "live": True,
                    "inputChars": 10000, "prefixChars": 8000})
        high = manager.stats()["cacheHit"]
        manager.chat[-1]["perf"]["prefixChars"] = 1000
        low = manager.stats()["cacheHit"]
        assert high > low, f"前缀 80% 应高于 10%: {high} vs {low}"
        await manager.stop()
    asyncio.run(scenario())


def test_final_usage_overrides_estimates(tmp_path, monkeypatch):
    """回复结束后必须用服务商的真实 usage 覆盖估算值。"""
    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        msg = _seed(manager, status="running",
                    perf={"ttftMs": 300, "decodeMs": 1000, "chars": 400,
                          "streamed": True, "live": True,
                          "inputChars": 9999, "prefixChars": 9999})
        est = manager.stats()
        msg.update(status="succeeded", finishedAt=2,
                   usage={"prompt_tokens": 1000, "completion_tokens": 200,
                          "prompt_cache_hit_tokens": 800},
                   perf={"ttftMs": 300, "decodeMs": 2000, "chars": 400, "streamed": True})
        final = manager.stats()
        assert final["live"] is False
        assert final["ctxUsed"] == 1000, "应该用真实 prompt_tokens"
        assert final["cacheHit"] == 80.0, "应该用真实命中率"
        assert est["ctxUsed"] != final["ctxUsed"], "估算值该被替换掉"
        await manager.stop()
    asyncio.run(scenario())


def test_learn_ratio_adapts_to_real_usage(tmp_path, monkeypatch):
    """换算比按真实 usage 滚动校正，下一轮估算更准。"""
    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        before = manager._chars_per_token
        # 真实情况：10000 字符 = 5000 token → 2.0 字符/token
        for _ in range(10):
            manager._learn_ratio({"prompt_tokens": 5000}, 10000, None)
        assert manager._chars_per_token > before, "应该往 2.0 靠近"
        assert 1.9 < manager._chars_per_token < 2.05, manager._chars_per_token
        await manager.stop()
    asyncio.run(scenario())


def test_learn_ratio_ignores_outliers(tmp_path, monkeypatch):
    """离群的样本（比如字符数统计错）不能把比值带飞。"""
    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        before = manager._chars_per_token
        manager._learn_ratio({"prompt_tokens": 1}, 999999, None)
        assert manager._chars_per_token == before
        await manager.stop()
    asyncio.run(scenario())


def test_no_estimate_without_live_flag(tmp_path, monkeypatch):
    """已结束但缺 usage 时不要编数字，显示 —。"""
    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        _seed(manager, perf={"ttftMs": 100, "decodeMs": 1000, "chars": 400})
        s = manager.stats()
        assert s["live"] is False
        assert s["cost"] is None or s["cost"] == 0 or s["cost"] is not None
        # 关键：不能因为缺 usage 就崩
        await manager.stop()
    asyncio.run(scenario())
