"""状态条数据：TPS / 首字 / 缓存命中 / 上下文占用，全部来自真实用量。

重点是「没测到就返回 None」——前端显示 —，不能显示 0。
"""
import asyncio

import pytest

from tests.test_agent import make_manager, wait_for


def _seed(manager, **message):
    base = {"id": "a1", "role": "assistant", "content": "ok", "ts": 1,
            "status": "succeeded", "model": "test-model"}
    base.update(message)
    manager.chat.append(base)
    return base


def test_stats_derives_tps_ttft_and_cache(tmp_path, monkeypatch):
    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        _seed(manager,
              usage={"prompt_tokens": 1000, "completion_tokens": 200,
                     "prompt_cache_hit_tokens": 750},
              perf={"ttftMs": 850, "decodeMs": 2000, "chars": 300, "streamed": True})
        stats = manager.stats()
        assert stats["tps"] == 100.0                 # 200 tokens / 2s
        assert stats["ttftMs"] == 850
        assert stats["cacheHit"] == 75.0             # 750 / 1000
        assert stats["ctxUsed"] == 1000
        await manager.stop()
    asyncio.run(scenario())


def test_stats_is_empty_without_usage(tmp_path, monkeypatch):
    """没有真实用量时不能编数字：全部 None，前端显示 —。"""
    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        _seed(manager)
        stats = manager.stats()
        assert stats["tps"] is None
        assert stats["ttftMs"] is None
        assert stats["cacheHit"] is None
        assert stats["completionTokens"] is None
        await manager.stop()
    asyncio.run(scenario())


def test_stats_live_estimates_tps_from_chars(tmp_path, monkeypatch):
    """回复进行中：状态栏要实时跳，所以用字符数估算 TPS。

    服务商的真实 token 数要到最后一帧才有，进行中只能估——
    标 live=True，前端据此区分"估算"和"实测"，结束后换成真实值。
    """
    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        msg = _seed(manager, status="running",
                    perf={"ttftMs": 400, "decodeMs": 1000, "deltas": 50,
                          "chars": 120, "streamed": True, "live": True})
        live = manager.stats()
        assert live["live"] is True
        assert live["ttftMs"] == 400, "首字延迟在回复中就该可见"
        assert live["tps"] is not None, "进行中也要有估算值，否则状态栏不动"
        # 120 字 / 1.6 ≈ 75 token，1 秒解码 → 约 75 tok/s
        assert 70 <= live["tps"] <= 80, live["tps"]

        msg.update(status="succeeded", finishedAt=2,
                   usage={"prompt_tokens": 500, "completion_tokens": 80},
                   perf={"ttftMs": 400, "decodeMs": 2000, "chars": 120,
                         "streamed": True, "requests": 1})
        final = manager.stats()
        assert final["live"] is False
        assert final["tps"] == 40.0                   # 80 tokens / 2s
        await manager.stop()
    asyncio.run(scenario())


def test_ctx_max_is_raised_when_config_is_too_small(tmp_path, monkeypatch):
    """配置 ctx 小于实际用量的常见窗口时，按实际用量兜底，不显示 >100%。"""
    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch,
                                  models=[{"id": "test-model", "active": True, "ctx": 2000}])
        _seed(manager, usage={"prompt_tokens": 15000, "completion_tokens": 10})
        stats = manager.stats()
        assert stats["ctxUsed"] == 15000
        assert stats["ctxMax"] >= 15000, "上限不能小于已用量"
        assert stats["ctxUsed"] / stats["ctxMax"] <= 1
        await manager.stop()
    asyncio.run(scenario())


def test_stats_skips_continuation_bubbles(tmp_path, monkeypatch):
    """长回复被拆成 a1/a1-2/a1-3，续气泡没有 usage：要取带数据的那条。"""
    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        _seed(manager, id="a1",
              usage={"prompt_tokens": 1000, "completion_tokens": 300,
                     "prompt_cache_hit_tokens": 500},
              perf={"ttftMs": 700, "decodeMs": 1500, "streamed": True})
        _seed(manager, id="a1-2", perf=None, ts=2)
        _seed(manager, id="a1-3", perf=None, ts=3)
        stats = manager.stats()
        assert stats["tps"] == 200.0, "续气泡不能把统计冲掉"
        assert stats["ttftMs"] == 700
        assert stats["cacheHit"] == 50.0
        await manager.stop()
    asyncio.run(scenario())


def test_stats_survives_unknown_model(tmp_path, monkeypatch):
    """历史消息里的模型已被删掉时，不能因为查不到配置就整体报错。"""
    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch)
        _seed(manager, model="gone-model",
              usage={"prompt_tokens": 10, "completion_tokens": 5},
              perf={"ttftMs": 100, "decodeMs": 1000})
        stats = manager.stats()
        assert stats["tps"] == 5.0
        assert stats["ctxMax"] is None
        await manager.stop()
    asyncio.run(scenario())


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
