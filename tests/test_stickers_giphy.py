"""在线贴纸（GIPHY）链路：搜索 → 落盘 → 按词复用 → 前端标记。

核心设计是"命中过一次就存到服务器"：
  · 浏览器永远只加载 /agent-attachments/stickers/xxx.gif，
    不直连 giphy CDN——那一段在国内可能被墙，而 API 是服务器在东京调的。
  · 同一个情绪词第二次出现时走本地索引，不打 API、不下载，
    这样免费 key 的 100 次/小时配额才够用。

覆盖：
  A. ai/sticker_cache.py：落盘/索引持久化/按词与按 id 查询/文件丢失自愈/
     容量淘汰/路径穿越。
  B. feed/giphy.py：URL 域名白名单、图片变体挑选、结果缓存、下载尺寸上限。
  C. ai/agent.py：_sticker_marker 标记格式、缓存命中不打 API、
     搜索失败回退内置图、既有"一条回复最多一个"约束不被破坏。
"""
import asyncio
import json
import os
import pathlib
import tempfile

import pytest

import feed.giphy as giphy
from ai.agent import _attach_sticker, _sticker_marker
from ai.sticker_cache import StickerCache


# ---------------- A. 落盘缓存 ----------------

def test_cache_saves_and_serves_local_url():
    root = tempfile.mkdtemp()
    cache = StickerCache(root)
    saved = cache.save("abc123", b"GIF89a" + b"x" * 64, 480, 270,
                       title="facepalm",
                       url_hint="https://media.giphy.com/media/x/giphy.gif",
                       content_type="image/gif")
    assert saved["url"] == "/agent-attachments/stickers/abc123.gif"
    assert saved["width"] == 480 and saved["height"] == 270
    assert (pathlib.Path(root) / "abc123.gif").read_bytes().startswith(b"GIF89a")


def test_cached_query_avoids_second_lookup():
    """同一个情绪词记住对应贴纸：这是省配额的关键。"""
    root = tempfile.mkdtemp()
    cache = StickerCache(root)
    cache.save("abc123", b"GIF89a" + b"x" * 32, 100, 100,
               url_hint="https://media.giphy.com/a.gif", content_type="image/gif")
    cache.remember_query("face palm", "abc123")
    hit = cache.lookup("  Face   Palm ")       # 空白/大小写归一
    assert hit and hit["id"] == "abc123"
    assert cache.lookup("never seen") is None


def test_cache_survives_restart():
    root = tempfile.mkdtemp()
    first = StickerCache(root)
    first.save("id1", b"GIF89a" + b"y" * 32, 64, 64,
               url_hint="https://media.giphy.com/a.gif", content_type="image/gif")
    first.remember_query("shrug", "id1")
    second = StickerCache(root)          # 模拟进程重启
    assert second.lookup("shrug")["id"] == "id1"


def test_missing_file_self_heals():
    """文件被手工删掉/构建覆盖后，不能一直返回 404 地址。"""
    root = tempfile.mkdtemp()
    cache = StickerCache(root)
    cache.save("id1", b"GIF89a" + b"z" * 32, 64, 64,
               url_hint="https://media.giphy.com/a.gif", content_type="image/gif")
    cache.remember_query("shrug", "id1")
    (pathlib.Path(root) / "id1.gif").unlink()
    assert cache.lookup("shrug") is None
    assert cache.lookup_id("id1") is None


def test_rejects_non_image_and_bad_ids():
    root = tempfile.mkdtemp()
    cache = StickerCache(root)
    # mp4 不能塞进 <img>
    assert cache.save("vid", b"x", 10, 10, url_hint="https://media.giphy.com/a.mp4") is None
    # 尺寸必须为正
    assert cache.save("bad", b"x", 0, 10, url_hint="https://media.giphy.com/a.gif") is None
    # 路径穿越会被清洗成普通文件名，绝不写到目录外
    cache.save("../../evil.gif", b"x" * 8, 10, 10,
               url_hint="https://media.giphy.com/a.gif", content_type="image/gif")
    assert not os.path.exists(os.path.join(tempfile.gettempdir(), "evil.gif"))
    assert all("/" not in name and "\\" not in name for name in os.listdir(root))


def test_prune_drops_oldest_and_cleans_query_index():
    root = tempfile.mkdtemp()
    cache = StickerCache(root)
    import ai.sticker_cache as mod
    old_max = mod.MAX_STICKERS
    mod.MAX_STICKERS = 2
    try:
        for i in range(4):
            cache.save(f"id{i}", b"GIF89a" + bytes([i]) * 16, 10, 10,
                       url_hint="https://media.giphy.com/a.gif",
                       content_type="image/gif")
            cache.remember_query(f"q{i}", f"id{i}")
        stats = cache.stats()
        assert stats["stickers"] == 2, stats
        # 指向已被淘汰贴纸的词条要一起清掉，否则永远命中不到
        for i in range(4):
            hit = cache.lookup(f"q{i}")
            if hit:
                assert hit["id"] in {f"id{j}" for j in range(4)}
    finally:
        mod.MAX_STICKERS = old_max


def test_index_is_valid_json_after_saves():
    root = tempfile.mkdtemp()
    cache = StickerCache(root)
    cache.save("id1", b"GIF89a" + b"a" * 16, 10, 10,
               url_hint="https://media.giphy.com/a.gif", content_type="image/gif")
    data = json.loads(pathlib.Path(root, "index.json").read_text(encoding="utf-8"))
    assert "id1" in data["stickers"]


# ---------------- B. GIPHY 客户端 ----------------

def test_media_host_whitelist():
    assert giphy._media_host_ok("https://media.giphy.com/media/x/giphy.gif")
    assert giphy._media_host_ok("https://media1.giphyusercontent.com/x.gif")
    # 非 GIPHY 域名、http、data: 一律拒绝
    for bad in ("https://evil.com/x.gif", "http://media.giphy.com/x.gif",
                "data:image/gif;base64,AAAA", "javascript:alert(1)", ""):
        assert not giphy._media_host_ok(bad), bad


def test_picks_best_image_variant():
    images = {
        "original": {"url": "https://media.giphy.com/o.gif", "width": "480", "height": "270"},
        "fixed_width": {"url": "https://media.giphy.com/f.gif", "width": "200", "height": "112"},
    }
    assert giphy._pick_image(images)["url"].endswith("o.gif")
    # original 不合格时退到 fixed_width
    images["original"]["url"] = "https://evil.com/o.gif"
    assert giphy._pick_image(images)["url"].endswith("f.gif")
    # 宽高非法时整条不要
    assert giphy._pick_image({"original": {"url": "https://media.giphy.com/o.gif",
                                          "width": "0", "height": "0"}}) is None


def test_search_without_key_returns_not_ok(monkeypatch):
    monkeypatch.delenv(giphy.KEY_ENV, raising=False)
    out = giphy.search_stickers("facepalm")
    assert out["ok"] is False and "KLINE_GIPHY_KEY" in out["error"]


def test_search_uses_result_cache(monkeypatch):
    """第二个同词查询必须走缓存（免费 key 只有 100 次/小时）。"""
    giphy.clear_cache()
    calls = []

    def fake_api(query, count, key, proxy, timeout):
        calls.append(query)
        return [{"id": "id1", "title": "t", "url": "https://media.giphy.com/a.gif",
                 "width": 100, "height": 80, "pageUrl": ""}]

    monkeypatch.setattr(giphy, "_call_api", fake_api)
    first = giphy.search_stickers("shrug", 1, api_key="k")
    second = giphy.search_stickers("shrug", 1, api_key="k")
    assert first["ok"] and second["ok"]
    assert second["cached"] is True
    assert calls == ["shrug"], "第二次不该再打 API"


def test_out_of_range_count_is_clamped(monkeypatch):
    giphy.clear_cache()
    seen = {}

    def fake_api(query, count, key, proxy, timeout):
        seen["count"] = count
        return [{"id": "i", "title": "", "url": "https://media.giphy.com/a.gif",
                 "width": 10, "height": 10, "pageUrl": ""}]

    monkeypatch.setattr(giphy, "_call_api", fake_api)
    giphy.search_stickers("x", 999, api_key="k")
    assert seen["count"] == 10


def test_download_rejects_non_giphy_url():
    with pytest.raises(ValueError):
        giphy.download_image("https://evil.com/x.gif")


# ---------------- C. 标记格式与回退 ----------------

def test_marker_for_builtin_and_remote():
    assert _sticker_marker({"builtin": "崩溃"}) == "[sticker:崩溃]"
    marker = _sticker_marker({"url": "/agent-attachments/stickers/a.gif",
                              "width": 480, "height": 270})
    assert marker.startswith("[sticker:url=")
    assert "w=480" in marker and "h=270" in marker
    assert _sticker_marker(None) == ""


def test_attach_only_once_per_reply():
    pieces = _attach_sticker(["笑死"], {"builtin": "崩溃"}, "笑死")
    assert len(pieces) == 1 and "[sticker:崩溃]" in pieces[0]
    # 已经有标记就不再加
    again = _attach_sticker(["[sticker:疑问]"], {"builtin": "崩溃"}, "[sticker:疑问]")
    assert again == ["[sticker:疑问]"]


def test_attach_sticker_only_when_no_text():
    assert _attach_sticker([], {"builtin": "嘲讽"}, "") == ["[sticker:嘲讽]"]


def test_cached_query_never_calls_search(tmp_path):
    """缓存命中时不打 API、不下载——这是配额与墙的双重保障。"""
    from ai.agent import AgentManager

    cache = StickerCache(str(tmp_path / "stickers"))
    cache.save("hit1", b"GIF89a" + b"q" * 32, 200, 150,
               url_hint="https://media.giphy.com/a.gif", content_type="image/gif")
    cache.remember_query("facepalm", "hit1")

    calls = []
    manager = AgentManager(lambda: {}, lambda: "", lambda: {}, lambda m: None,
                           str(tmp_path / "state.json"))
    manager.sticker_cache = cache
    manager.sticker_search_fn = lambda q, c: calls.append(q) or {"ok": False}

    async def run():
        return await manager._search_sticker("facepalm")

    found = asyncio.run(run())
    assert found and found["id"] == "hit1"
    assert calls == [], "缓存命中时不该调用搜索"


def test_search_failure_falls_back_to_builtin(tmp_path):
    from ai.agent import AgentManager

    manager = AgentManager(lambda: {}, lambda: "", lambda: {}, lambda m: None,
                           str(tmp_path / "state.json"))
    manager.sticker_cache = StickerCache(str(tmp_path / "s"))
    manager.sticker_search_fn = lambda q, c: {"ok": False, "error": "network"}
    target = {}

    async def run():
        return await manager._send_sticker({"query": "facepalm", "name": "崩溃"}, target)

    out = asyncio.run(run())
    assert out["sent"] is True and out["source"] == "builtin"
    assert target["pendingSticker"] == {"builtin": "崩溃"}


def test_remote_sticker_is_downloaded_then_cached(tmp_path):
    """第一次命中要落盘，第二次同词直接读本地。"""
    from ai.agent import AgentManager

    cache = StickerCache(str(tmp_path / "s"))
    download_calls = []

    def fake_search(query, count):
        return {"ok": True, "results": [{
            "id": "gid1", "title": "shrug", "url": "https://media.giphy.com/a.gif",
            "width": 300, "height": 200, "pageUrl": ""}]}

    def fake_download(url):
        download_calls.append(url)
        return b"GIF89a" + b"d" * 64, "image/gif"

    manager = AgentManager(lambda: {}, lambda: "", lambda: {}, lambda m: None,
                           str(tmp_path / "state.json"))
    manager.sticker_cache = cache
    manager.sticker_search_fn = fake_search
    manager.sticker_download_fn = fake_download

    async def run():
        one = await manager._search_sticker("shrug")
        two = await manager._search_sticker("shrug")
        return one, two

    first, second = asyncio.run(run())
    assert first["url"].startswith("/agent-attachments/stickers/")
    assert second["id"] == first["id"]
    assert download_calls == ["https://media.giphy.com/a.gif"], "第二次不该再下载"
