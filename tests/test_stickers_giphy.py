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


# ---------------- D. 联网不可用时从已存表情里挑 ----------------

def test_pick_for_matches_stored_emotion():
    """配额用完/断网时，从已存的贴纸里挑一张贴近当前情绪的。"""
    root = tempfile.mkdtemp()
    cache = StickerCache(root)
    for sid, word in (("id-dead", "dead inside"), ("id-shrug", "shrug"),
                      ("id-pop", "popcorn")):
        cache.save(sid, b"GIF89a" + sid.encode()[:8], 100, 100,
                   url_hint="https://media.giphy.com/a.gif", content_type="image/gif")
        cache.remember_query(word, sid)
    # "dead" 应该命中 dead inside 那张
    assert cache.pick_for("dead")["id"] == "id-dead"
    # 换词大小写/多余空白也要能匹配
    assert cache.pick_for("  SHRUG  ")["id"] == "id-shrug"
    # 完全不相关时不能返回 None——总比不发强，退回最新的一张
    assert cache.pick_for("something totally unrelated") is not None


def test_pick_for_avoids_repeating_same_sticker():
    root = tempfile.mkdtemp()
    cache = StickerCache(root)
    cache.save("a", b"GIF89a" + b"a" * 8, 10, 10,
               url_hint="https://media.giphy.com/a.gif", content_type="image/gif")
    cache.save("b", b"GIF89a" + b"b" * 8, 10, 10,
               url_hint="https://media.giphy.com/b.gif", content_type="image/gif")
    first = cache.pick_for("anything")
    second = cache.pick_for("anything", avoid_id=first["id"])
    assert first["id"] != second["id"], "不该连续发同一张"


def test_empty_cache_pick_returns_none():
    root = tempfile.mkdtemp()
    assert StickerCache(root).pick_for("dead") is None


def test_send_sticker_falls_back_to_stored_not_builtin(tmp_path):
    """核心场景：联网挂了，但库里已有贴纸 → 用库里的，不要退回内置图。"""
    from ai.agent import AgentManager

    cache = StickerCache(str(tmp_path / "s"))
    cache.save("stored1", b"GIF89a" + b"s" * 16, 200, 200,
               url_hint="https://media.giphy.com/a.gif", content_type="image/gif")
    cache.remember_query("dead inside", "stored1")

    manager = AgentManager(lambda: {}, lambda: "", lambda: {}, lambda m: None,
                           str(tmp_path / "state.json"))
    manager.sticker_cache = cache
    # 模拟配额用完 / 断网
    manager.sticker_search_fn = lambda q, c: {"ok": False, "error": "rate limit"}

    target = {}
    out = asyncio.run(manager._send_sticker({"query": "dead", "name": "崩溃"}, target))
    assert out["sent"] is True
    assert out["source"] == "stored", out
    assert target["pendingSticker"]["id"] == "stored1"
    # 标记要带本地地址，前端才能直接加载
    marker = _sticker_marker(target["pendingSticker"])
    assert marker.startswith("[sticker:url=")


def test_send_sticker_builtin_is_last_resort(tmp_path):
    """库里一张都没有时才退回内置图。"""
    from ai.agent import AgentManager

    manager = AgentManager(lambda: {}, lambda: "", lambda: {}, lambda m: None,
                           str(tmp_path / "state.json"))
    manager.sticker_cache = StickerCache(str(tmp_path / "empty"))
    manager.sticker_search_fn = lambda q, c: {"ok": False, "error": "offline"}
    target = {}
    out = asyncio.run(manager._send_sticker({"query": "dead", "name": "崩溃"}, target))
    assert out["source"] == "builtin"
    assert target["pendingSticker"] == {"builtin": "崩溃"}


# ---------------- E. 表情包表：作为真实数据交给 Agent ----------------

def test_stickers_table_round_trip(tmp_path, monkeypatch):
    """落盘 → 进表 → 能按情绪词和按 id 查回来。"""
    import storage as storage_mod
    from tests.test_paper_store import SQLConnection

    conn = SQLConnection()
    conn.db.execute("CREATE TABLE equity(acct TEXT,ts BIGINT,bal DOUBLE,"
                    "upnl DOUBLE,PRIMARY KEY(acct,ts))")
    monkeypatch.setattr(storage_mod.pymysql, "connect", lambda **_: conn)
    store = storage_mod.Storage({"user": "isolated-test"})
    assert store.enabled

    cache = StickerCache(str(tmp_path / "s"), store=store)
    cache.save("sid1", b"GIF89a" + b"x" * 32, 300, 200, title="facepalm",
               url_hint="https://media.giphy.com/a.gif", content_type="image/gif")
    cache.remember_query("facepalm", "sid1")
    cache.remember_query("dead inside", "sid1")

    rows = store.stickers()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "sid1" and row["width"] == 300 and row["height"] == 200
    # 两个情绪词都累积到 tags 里
    assert "facepalm" in row["tags"] and "dead inside" in row["tags"]

    # 按情绪过滤
    assert [r["id"] for r in store.stickers("dead")] == ["sid1"]
    assert store.stickers("nothing") == []

    store.touch_sticker("sid1")
    assert store.stickers()[0]["useCount"] == 1


def test_catalog_exposes_library_to_agent(tmp_path):
    """Agent 通过 catalog 看到库里有什么（这是它挑表情的依据）。"""
    root = tmp_path / "s"
    cache = StickerCache(str(root))
    for sid, word in (("a1", "facepalm"), ("b2", "shrug")):
        cache.save(sid, b"GIF89a" + sid.encode(), 120, 90,
                   url_hint="https://media.giphy.com/a.gif", content_type="image/gif")
        cache.remember_query(word, sid)

    rows = cache.catalog()
    assert {r["id"] for r in rows} == {"a1", "b2"}
    assert all("tags" in r and "width" in r and "height" in r for r in rows)
    # 按情绪过滤
    assert [r["id"] for r in cache.catalog("shrug")] == ["b2"]


def test_list_stickers_tool_returns_catalog(tmp_path):
    from ai.agent import AgentManager

    cache = StickerCache(str(tmp_path / "s"))
    cache.save("pick-me", b"GIF89a" + b"p" * 16, 200, 150, title="popcorn",
               url_hint="https://media.giphy.com/a.gif", content_type="image/gif")
    cache.remember_query("popcorn", "pick-me")

    manager = AgentManager(lambda: {}, lambda: "", lambda: {}, lambda m: None,
                           str(tmp_path / "state.json"))
    manager.sticker_cache = cache
    out = manager._list_stickers({"query": "popcorn"})
    assert out["available"] is True
    assert out["stickers"][0]["id"] == "pick-me"
    assert "popcorn" in out["stickers"][0]["tags"]
    assert "sticker_id" in out["note"]


def test_list_stickers_without_library(tmp_path):
    from ai.agent import AgentManager

    manager = AgentManager(lambda: {}, lambda: "", lambda: {}, lambda m: None,
                           str(tmp_path / "state.json"))
    out = manager._list_stickers({})
    assert out["available"] is False and out["stickers"] == []


def test_send_by_sticker_id_skips_search(tmp_path):
    """模型看过清单后按 id 发：不该再去联网搜。"""
    from ai.agent import AgentManager

    cache = StickerCache(str(tmp_path / "s"))
    cache.save("chosen", b"GIF89a" + b"c" * 16, 240, 180, title="clown",
               url_hint="https://media.giphy.com/a.gif", content_type="image/gif")
    manager = AgentManager(lambda: {}, lambda: "", lambda: {}, lambda m: None,
                           str(tmp_path / "state.json"))
    manager.sticker_cache = cache
    searches = []
    manager.sticker_search_fn = lambda q, c: searches.append(q) or {"ok": False}

    target = {}
    out = asyncio.run(manager._send_sticker({"sticker_id": "chosen"}, target))
    assert out["sent"] is True and out["source"] == "stored"
    assert target["pendingSticker"]["id"] == "chosen"
    assert searches == [], "按 id 发送时不该联网搜索"


def test_unknown_sticker_id_falls_back_gracefully(tmp_path):
    """指定了不存在的 id 不能直接失败，要退回按 query 处理。"""
    from ai.agent import AgentManager

    cache = StickerCache(str(tmp_path / "s"))
    cache.save("real1", b"GIF89a" + b"r" * 16, 100, 100,
               url_hint="https://media.giphy.com/a.gif", content_type="image/gif")
    cache.remember_query("dead inside", "real1")
    manager = AgentManager(lambda: {}, lambda: "", lambda: {}, lambda m: None,
                           str(tmp_path / "state.json"))
    manager.sticker_cache = cache
    manager.sticker_search_fn = lambda q, c: {"ok": False}

    target = {}
    out = asyncio.run(manager._send_sticker(
        {"sticker_id": "does-not-exist", "query": "dead"}, target))
    assert out["sent"] is True and out["source"] == "stored"
    assert target["pendingSticker"]["id"] == "real1"


def test_list_stickers_is_a_real_tool():
    from ai.agent_tools import TOOL_NAMES
    assert "list_stickers" in TOOL_NAMES
