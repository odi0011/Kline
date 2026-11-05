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
import time

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

def test_pick_for_matches_stored_emotion(tmp_path):
    """配额用完/断网时，从已存的贴纸里挑一张贴近当前情绪的。

    用 tmp_path 而不是 tempfile.mkdtemp()：后者不清理，整套测试跑下来
    会留下一堆目录，而且互相干扰（这条曾经单跑通过、套跑失败）。
    """
    cache = StickerCache(str(tmp_path / "s"))
    for sid, word in (("id-dead", "无语"), ("id-shrug", "shrug"),
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
    cache.remember_query("无语", "stored1")

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


# ---------------- F. 闭环：看候选 → 命名/定用途 → 入库 → 发送 ----------------

def _manager_with_stickers(tmp_path, search_results=None, download_ok=True):
    from ai.agent import AgentManager

    cache = StickerCache(str(tmp_path / "s"))
    manager = AgentManager(lambda: {}, lambda: "", lambda: {}, lambda m: None,
                           str(tmp_path / "state.json"))
    manager.sticker_cache = cache
    rows = search_results if search_results is not None else [
        {"id": "cand-a", "title": "person facepalming", "url": "https://media.giphy.com/a.gif",
         "width": 300, "height": 200, "pageUrl": ""},
        {"id": "cand-b", "title": "shrugging man", "url": "https://media.giphy.com/b.gif",
         "width": 260, "height": 300, "pageUrl": ""},
    ]
    manager.sticker_search_fn = lambda q, c: {"ok": True, "results": rows}
    if download_ok:
        manager.sticker_download_fn = lambda url: (b"GIF89a" + b"d" * 32, "image/gif")
    return manager, cache


def test_search_returns_candidates_without_saving(tmp_path):
    """搜候选：只给模型看，不落盘、不入库。"""
    manager, cache = _manager_with_stickers(tmp_path)
    out = asyncio.run(manager._search_sticker_candidates({"query": "facepalm"}))
    assert out["available"] is True
    assert [c["id"] for c in out["candidates"]] == ["cand-a", "cand-b"]
    # 描述要回给模型，这是它判断语义的唯一依据
    assert out["candidates"][0]["caption"] == "person facepalming"
    assert cache.catalog() == [], "搜候选阶段不该入库"
    assert not (tmp_path / "s").exists() or cache.stats()["stickers"] == 0


def test_save_sticker_records_agent_chosen_name_and_when(tmp_path):
    """闭环核心：模型定名字和用途，落盘入库后能原样读回。"""
    manager, cache = _manager_with_stickers(tmp_path)
    asyncio.run(manager._search_sticker_candidates({"query": "facepalm"}))
    saved = asyncio.run(manager._save_sticker({
        "candidate_id": "cand-a", "name": "捂脸",
        "when": "用户吐槽自己操作失误、或者再说也没用的时候"}))
    assert saved["saved"] is True and saved["id"] == "cand-a"

    rows = cache.catalog()
    assert len(rows) == 1
    assert rows[0]["name"] == "捂脸"
    assert rows[0]["when"] == "用户吐槽自己操作失误、或者再说也没用的时候"
    # 不是抄 GIPHY 的英文标题
    assert rows[0]["name"] != "person facepalming"


def test_save_rejects_unknown_candidate(tmp_path):
    """没搜过的候选不能存——防止模型凭空编一个 id。"""
    manager, cache = _manager_with_stickers(tmp_path)
    out = asyncio.run(manager._save_sticker(
        {"candidate_id": "made-up-id", "name": "编的"}))
    assert out["saved"] is False and "search_stickers" in out["reason"]
    assert cache.catalog() == []


def test_save_requires_name(tmp_path):
    manager, _ = _manager_with_stickers(tmp_path)
    asyncio.run(manager._search_sticker_candidates({"query": "facepalm"}))
    with pytest.raises(ValueError):
        asyncio.run(manager._save_sticker({"candidate_id": "cand-a"}))


def test_save_requires_when(tmp_path):
    """when 必填：早期没传 when 存出的 3 张图，清单里只剩名字，用途不明。"""
    manager, cache = _manager_with_stickers(tmp_path)
    asyncio.run(manager._search_sticker_candidates({"query": "facepalm"}))
    with pytest.raises(ValueError, match="when 不能为空"):
        asyncio.run(manager._save_sticker({"candidate_id": "cand-a", "name": "捂脸"}))
    assert cache.catalog() == []


def test_catalog_flags_missing_when(tmp_path):
    """老数据没写 when 时，清单要标 whenMissing，别让模型当成万能图。"""
    manager, _ = _manager_with_stickers(tmp_path)
    asyncio.run(manager._search_sticker_candidates({"query": "facepalm"}))
    asyncio.run(manager._save_sticker({"candidate_id": "cand-a", "name": "捂脸",
                                      "when": "自嘲"}))
    out = manager._list_stickers({})
    # 内置图排在最前面，库里的要按名字找
    saved = next(x for x in out["stickers"] if x["name"] == "捂脸")
    assert saved["when"] == "自嘲"
    assert saved["whenMissing"] is False


def test_saved_sticker_can_be_sent_by_id(tmp_path):
    """存完之后按 id 发送：不再联网。"""
    manager, cache = _manager_with_stickers(tmp_path)
    asyncio.run(manager._search_sticker_candidates({"query": "facepalm"}))
    asyncio.run(manager._save_sticker({"candidate_id": "cand-a", "name": "捂脸",
                                      "when": "自嘲"}))
    searches = []
    manager.sticker_search_fn = lambda q, c: searches.append(q) or {"ok": False}
    target = {}
    out = asyncio.run(manager._send_sticker({"sticker_id": "cand-a"}, target))
    assert out["sent"] is True and out["source"] == "stored"
    assert target["pendingSticker"]["id"] == "cand-a"
    assert searches == []


def test_full_loop_list_then_send(tmp_path):
    """完整闭环：存过之后 list_stickers 能看到名字/用途，直接挑出来发。"""
    manager, cache = _manager_with_stickers(tmp_path)
    asyncio.run(manager._search_sticker_candidates({"query": "shrug"}))
    asyncio.run(manager._save_sticker({"candidate_id": "cand-b", "name": "摊手",
                                      "when": "被问不确定的事"}))
    listed = manager._list_stickers({})
    # 内置图（崩溃/疑问/嘲讽）排在前面，库里的按名字取
    picked = next(x for x in listed["stickers"] if x["name"] == "摊手")
    assert picked["when"] == "被问不确定的事"
    target = {}
    out = asyncio.run(manager._send_sticker(
        {"sticker_id": picked["id"]}, target))
    assert out["sent"] is True
    # 标记里带的是本地地址
    assert _sticker_marker(target["pendingSticker"]).startswith("[sticker:url=")


def test_search_and_save_tools_registered():
    from ai.agent_tools import TOOL_NAMES
    for name in ("search_stickers", "save_sticker", "list_stickers", "send_sticker"):
        assert name in TOOL_NAMES


# ---------------- G. 闭环不能断的两个坑 ----------------

def test_save_records_search_query_as_tag(tmp_path):
    """存图时要把"当初搜到它的情绪词"记成 tag，否则以后按情绪搜不到。"""
    manager, cache = _manager_with_stickers(tmp_path)
    asyncio.run(manager._search_sticker_candidates({"query": "dead inside"}))
    asyncio.run(manager._save_sticker({"candidate_id": "cand-a", "name": "麻了",
                                      "when": "心态崩了"}))
    # 按当初的搜索词能命中
    assert cache.lookup("dead inside") is not None
    rows = cache.catalog()
    assert "dead inside" in rows[0]["tags"]


def test_remember_query_does_not_wipe_name_and_when(tmp_path, monkeypatch):
    """REPLACE INTO 的坑：remember_query 少传 name/when 会把它们清空。"""
    import storage as storage_mod
    from tests.test_paper_store import SQLConnection

    conn = SQLConnection()
    conn.db.execute("CREATE TABLE equity(acct TEXT,ts BIGINT,bal DOUBLE,"
                    "upnl DOUBLE,PRIMARY KEY(acct,ts))")
    monkeypatch.setattr(storage_mod.pymysql, "connect", lambda **_: conn)
    store = storage_mod.Storage({"user": "isolated-test"})

    cache = StickerCache(str(tmp_path / "s"), store=store)
    cache.save("sid1", b"GIF89a" + b"x" * 32, 200, 150,
               url_hint="https://media.giphy.com/a.gif", content_type="image/gif",
               name="捂脸", when_to_use="自嘲的时候")
    assert store.stickers()[0]["name"] == "捂脸"

    # 之后才被情绪词命中
    cache.remember_query("facepalm", "sid1")
    row = store.stickers()[0]
    assert row["name"] == "捂脸", "记住情绪词不能把名字擦掉"
    assert row["whenToUse"] == "自嘲的时候", "也不能把用途擦掉"
    assert "facepalm" in row["tags"]


def test_resaving_keeps_accumulated_tags(tmp_path, monkeypatch):
    """重复保存同一张时，已累积的 tags 不能被清空。"""
    import storage as storage_mod
    from tests.test_paper_store import SQLConnection

    conn = SQLConnection()
    conn.db.execute("CREATE TABLE equity(acct TEXT,ts BIGINT,bal DOUBLE,"
                    "upnl DOUBLE,PRIMARY KEY(acct,ts))")
    monkeypatch.setattr(storage_mod.pymysql, "connect", lambda **_: conn)
    store = storage_mod.Storage({"user": "isolated-test"})

    cache = StickerCache(str(tmp_path / "s"), store=store)
    cache.save("sid1", b"GIF89a" + b"x" * 32, 100, 100,
               url_hint="https://media.giphy.com/a.gif", content_type="image/gif")
    cache.remember_query("shrug", "sid1")
    assert "shrug" in store.stickers()[0]["tags"]

    # 同一张再存一次（例如重新下载）
    cache.save("sid1", b"GIF89a" + b"y" * 32, 100, 100,
               url_hint="https://media.giphy.com/a.gif", content_type="image/gif")
    assert "shrug" in store.stickers()[0]["tags"], "重复保存不能清空 tags"


# ---------------- H. 存储的必须是真图片（线上踩过的坑） ----------------

def test_save_rejects_html_error_page():
    """GIPHY 重定向没跟随时返回 HTML，不能当 GIF 存进去。

    线上三个文件字节都以 <!DOCT 开头，静态服务仍按 .gif 报 image/gif，
    浏览器渲染成空白——只留一行 Powered by GIPHY。
    """
    root = tempfile.mkdtemp()
    cache = StickerCache(root)
    html = b"<!DOCTYPE html><html><body>Not found</body></html>"
    assert cache.save("bad1", html, 480, 480,
                      url_hint="https://media.giphy.com/a.gif",
                      content_type="image/gif") is None
    assert cache.stats()["stickers"] == 0


def test_save_corrects_extension_by_real_bytes():
    """扩展名按真实字节纠正：URL 说是 .gif、实际是 PNG 就存成 .png。"""
    root = tempfile.mkdtemp()
    cache = StickerCache(root)
    png = b"\x89PNG\r\n\x1a\n" + b"x" * 32
    saved = cache.save("p1", png, 100, 100,
                       url_hint="https://media.giphy.com/a.gif",
                       content_type="image/gif")
    assert saved and saved["url"].endswith(".png")
    # 文件本体也是 PNG
    data = pathlib.Path(root, "p1.png").read_bytes()
    assert data.startswith(b"\x89PNG")


def test_cached_corrupt_sticker_is_dropped():
    """索引里有、但文件内容是错误页：查出来要当不存在，别把空白图发出去。"""
    root = tempfile.mkdtemp()
    cache = StickerCache(root)
    # 手工伪造一条脏记录（模拟旧版本存进去的坏数据）
    pathlib.Path(root, "dirty.gif").write_bytes(b"<!DOCTYPE html><html></html>")
    cache._index["stickers"]["dirty"] = {
        "id": "dirty", "file": "dirty.gif", "width": 100, "height": 100,
        "url": "/agent-attachments/stickers/dirty.gif", "bytes": 30, "ts": 1}
    cache._index["queries"]["x"] = {"id": "dirty", "ts": 1}
    cache._save()

    fresh = StickerCache(root)
    assert fresh.lookup_id("dirty") is None, "坏文件不能当成可用贴纸"
    assert fresh.lookup("x") is None
    assert fresh.catalog() == []


def test_download_follows_redirect():
    """贴纸下载必须自己跟随 302，且每跳重新校验。

    不跟随时存下的是 302 响应体（HTML），这就是线上坏文件的来源。
    """
    calls = []

    class Resp:
        def __init__(self, status, headers, body):
            self.status, self.headers, self._body = status, headers, body

        def read(self, n=-1):
            return self._body[:n] if n and n > 0 else self._body

        def close(self):
            pass

    def fake_open(request, proxy="", timeout=0):
        calls.append(request.full_url)
        if request.full_url.endswith("/giphy.gif"):
            return Resp(302, {"Location": "https://media2.giphy.com/real.gif"}, b"")
        return Resp(200, {"Content-Type": "image/gif"}, b"GIF89a" + b"z" * 40)

    import feed.giphy as g
    original = g._open
    g._open = fake_open
    try:
        data, ctype = g.download_image("https://media.giphy.com/giphy.gif")
    finally:
        g._open = original
    assert data.startswith(b"GIF89a")
    assert len(calls) == 2, f"应该跟一次重定向: {calls}"


def test_download_rejects_redirect_to_non_giphy():
    """重定向到非 GIPHY 域名必须拒绝（防 SSRF 绕道）。"""
    class Resp:
        def __init__(self, status, headers, body):
            self.status, self.headers, self._body = status, headers, body

        def read(self, n=-1):
            return self._body

        def close(self):
            pass

    def fake_open(request, proxy="", timeout=0):
        return Resp(302, {"Location": "http://127.0.0.1/admin"}, b"")

    import feed.giphy as g
    original = g._open
    g._open = fake_open
    try:
        with pytest.raises(ValueError):
            g.download_image("https://media.giphy.com/giphy.gif")
    finally:
        g._open = original


def test_download_rejects_non_image_body():
    """200 + image/gif 但内容是 HTML：必须拒绝。"""
    class Resp:
        def __init__(self):
            self.status = 200
            self.headers = {"Content-Type": "image/gif"}

        def read(self, n=-1):
            return b"<!DOCTYPE html><html>blocked</html>"

        def close(self):
            pass

    import feed.giphy as g
    original = g._open
    g._open = lambda request, proxy="", timeout=0: Resp()
    try:
        with pytest.raises(ValueError):
            g.download_image("https://media.giphy.com/giphy.gif")
    finally:
        g._open = original


def test_download_uses_image_accept_header():
    """下载图片必须用图片 Accept。

    复用搜索用的 HEADERS（Accept: text/html,...）时，GIPHY CDN 会返回
    HTML 页面——线上三个"GIF"全是 <!DOCTYPE html，根因就在这。
    """
    seen = {}

    class Resp:
        status = 200
        headers = {"Content-Type": "image/gif"}

        def read(self, n=-1):
            return b"GIF89a" + b"z" * 40

        def close(self):
            pass

    def fake_open(request, proxy="", timeout=0):
        seen["accept"] = request.get_header("Accept") or ""
        return Resp()

    import feed.giphy as g
    original = g._open
    g._open = fake_open
    try:
        g.download_image("https://media.giphy.com/giphy.gif")
    finally:
        g._open = original
    assert "image/" in seen["accept"], f"Accept 必须是图片类型，实际: {seen['accept']!r}"
    assert "text/html" not in seen["accept"]


# ---------------- I. 已选好表情时，空正文不能报错 ----------------

def test_sticker_only_reply_survives_empty_second_round(tmp_path, monkeypatch):
    """选好表情包后模型给了空正文：必须正常结束，不能报"未返回文本或工具调用"。

    线上实测：'发个表情包' 那一次，模型调了 list_stickers + send_sticker，
    第二轮返回空，用户看到红色错误气泡 —— 图其实已经选好了。
    """
    from tests.test_agent import finished, make_manager, tool_call, tool_result

    calls = {"n": 0}

    def provider(*args, **kwargs):
        calls["n"] += 1
        if kwargs.get("tool_choice") == "required":
            return tool_result(tool_call("send_sticker", {"name": "崩溃"}, "s1"))
        # 第二轮：既无正文也无工具调用
        return {"ok": True, "reply": "", "usage": {"total_tokens": 5}, "model": "test-model"}

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch, provider=provider)
        manager.sticker_cache = StickerCache(str(tmp_path / "s"))
        await manager.ask("发个表情包", "test-model")
        assert await wait_for(lambda: not manager.chat_runners)
        answer = [m for m in manager.chat if m.get("role") == "assistant"][-1]
        assert answer["status"] == "succeeded", answer.get("error")
        assert answer["content"] == "[sticker:崩溃]"
        await manager.stop()

    from tests.test_agent import wait_for
    asyncio.run(scenario())


def test_list_stickers_note_points_at_single_sticker(tmp_path):
    """库里只有一张时，note 要直接给出那个 id，避免模型只回"库里一张"。"""
    manager, cache = _manager_with_stickers(tmp_path)
    asyncio.run(manager._search_sticker_candidates({"query": "shrug"}))
    asyncio.run(manager._save_sticker({"candidate_id": "cand-b", "name": "摊手",
                                      "when": "被问住"}))
    out = manager._list_stickers({})
    # 全部内置图 + 库里这 1 张
    from ai.agent_tools import BUILTIN_STICKERS
    assert out["count"] == len(BUILTIN_STICKERS) + 1, out["count"]
    assert any(x["id"] == "builtin:崩溃" for x in out["stickers"])


# ---------------- J. 使用计数：真正发出去才 +1 ----------------

def test_touch_increments_and_orders_catalog(tmp_path):
    """发过一次的应排在前面，且 uses 递增。"""
    root = tmp_path / "s"
    cache = StickerCache(str(root))
    for sid in ("a", "b", "c"):
        cache.save(sid, b"GIF89a" + sid.encode(), 10, 10,
                   url_hint="https://media.giphy.com/a.gif", content_type="image/gif")
    assert [r["uses"] for r in cache.catalog()] == [0, 0, 0]

    cache.touch("b")
    rows = cache.catalog()
    assert rows[0]["id"] == "b", f"用过的要排最前: {rows}"
    assert rows[0]["uses"] == 1
    cache.touch("b")
    assert cache.catalog()[0]["uses"] == 2


def test_touch_persists_locally_without_db(tmp_path):
    """没连数据库时也要能记住，否则重启后计数归零。"""
    root = tmp_path / "s"
    cache = StickerCache(str(root))
    cache.save("a", b"GIF89a" + b"a" * 8, 10, 10,
               url_hint="https://media.giphy.com/a.gif", content_type="image/gif")
    cache.touch("a")
    fresh = StickerCache(str(root))
    assert fresh.catalog()[0]["uses"] == 1


def test_sending_marks_sticker_used(tmp_path, monkeypatch):
    """端到端：回复里真的带上了表情，才记一次使用。"""
    from tests.test_agent import make_manager, tool_call, tool_result, wait_for

    def provider(*args, **kwargs):
        if kwargs.get("tool_choice") == "required":
            return tool_result(tool_call("send_sticker", {"name": "崩溃"}, "s1"))
        return {"ok": True, "reply": "", "usage": {"total_tokens": 5},
                "model": "test-model"}

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch, provider=provider)
        cache = StickerCache(str(tmp_path / "s"))
        cache.save("saved1", b"GIF89a" + b"z" * 16, 100, 100,
                   url_hint="https://media.giphy.com/a.gif", content_type="image/gif")
        manager.sticker_cache = cache
        # 让模型走"按 id 发送"，这样 touch 才有 id 可记
        manager.sticker_search_fn = None
        await manager.ask("发个表情包", "test-model")
        assert await wait_for(lambda: not manager.chat_runners)
        await manager.stop()

    asyncio.run(scenario())




def test_pick_for_avoid_id_switches_when_tied(tmp_path):
    """同分时 avoid_id 能让选中的那张让位（避免连发同一张）。

    一个情绪词通常只映射一张图，所以"两张同分"要靠同一句的等价写法
    （尾部多一个空格，词元和子串判断都一样）构造。
    """
    root = tmp_path / "s"
    cache = StickerCache(str(root))
    for sid in ("a", "b"):
        cache.save(sid, b"GIF89a" + sid.encode(), 10, 10,
                   url_hint="https://media.giphy.com/a.gif", content_type="image/gif")
    cache._index["queries"] = {
        "dead inside": {"id": "a", "ts": 1},
        "dead inside ": {"id": "b", "ts": 1},   # 等价写法，分数相同
    }
    cache._save()

    first = cache.pick_for("dead inside")
    assert first, "应该能选出一张"
    second = cache.pick_for("dead inside", avoid_id=first["id"])
    assert second["id"] != first["id"], \
        f"avoid_id={first['id']} 时应换成另一张，实际仍是 {second['id']}"


def test_sticker_always_has_a_name(tmp_path):
    """库里不能出现无名表情：清单里看不出它是干什么的。"""
    root = tmp_path / "s"
    cache = StickerCache(str(root))
    # 完全不给 name，只有 GIPHY 标题
    saved = cache.save("n1", b"GIF89a" + b"n" * 16, 10, 10,
                       title="Dead Inside Sticker", url_hint="https://media.giphy.com/a.gif",
                       content_type="image/gif")
    assert saved
    assert cache.catalog()[0]["name"] == "Dead Inside Sticker"


def test_auto_store_uses_query_as_name(tmp_path):
    """send_sticker(query=...) 自动入库时，用查询词兜底当名字。"""
    manager, cache = _manager_with_stickers(tmp_path)
    target = {}
    asyncio.run(manager._send_sticker({"query": "shrug"}, target))
    rows = cache.catalog()
    assert rows, "应该自动存了一张"
    assert rows[0]["name"], f"名字不能为空: {rows[0]}"
    assert "shrug" in rows[0]["name"].lower()


def test_marker_written_by_model_survives_split():
    """模型自己把标记写在第二行时，不能被"截断 + 跳过追加"两头丢掉。

    线上实测：模型返回 "摊手。\n[sticker:摊手]"，split_reply 按短答规则
    只留第一句，标记被截掉；而 _attach_sticker 又因为原始 reply 里含
    "[sticker:" 而跳过追加 —— 结果图没了，只剩一句"摊手。"。
    """
    from ai.agent import split_reply

    reply = "摊手。\n[sticker:摊手]"
    pieces = split_reply(reply, detailed=False, identity=False)
    assert pieces == ["摊手。"], "前提：split 只留第一句"

    sticker = {"url": "/agent-attachments/stickers/a.gif", "width": 480, "height": 480}
    out = _attach_sticker(pieces, sticker, reply)
    assert len(out) == 1
    assert "[sticker:" in out[0], "标记被截断后必须补回来"
    assert out[0].startswith("摊手。")


def test_attach_does_not_duplicate_when_marker_kept():
    """分段里已经有**有效**标记时不能重复追加。

    注意要用内置名（崩溃/疑问/嘲讽）或 url= 形式；
    "[sticker:摊手]" 这种库里中文名不算有效——见 test_invalid_handwritten_marker_is_replaced。
    """
    sticker = {"url": "/agent-attachments/stickers/a.gif", "width": 10, "height": 10}
    out = _attach_sticker(["[sticker:崩溃]"], sticker, "[sticker:崩溃]")
    assert out == ["[sticker:崩溃]"]


# ---------------- K. 服务端兜底：该发时必须真的发 ----------------

def test_emotion_detection_triggers_sticker():
    """情绪词/纯标点要能被识别成"该配图"。"""
    from ai.agent import _auto_sticker_query
    for text in ("？", "额", "笑死", "哈哈哈哈", "麻了", "又亏了草", "卧槽", "牛啊", "起飞"):
        assert _auto_sticker_query(text), f"{text!r} 应该触发表情包"


def test_serious_trading_question_never_gets_sticker():
    """认真问价位/仓位/风险时不发，哪怕带情绪词。"""
    from ai.agent import _auto_sticker_query
    for text in ("BTC能不能入？", "止损设多少", "现在该做多还是做空",
                 "分析一下BTC", "仓位多少", "我麻了，还能加仓吗",
                 "这波亏麻了，要不要平仓"):
        assert _auto_sticker_query(text) == "", f"{text!r} 不该发表情包"


def test_plain_statement_is_not_emotional():
    from ai.agent import _auto_sticker_query
    assert _auto_sticker_query("试了，还是多。") == ""
    assert _auto_sticker_query("") == ""


def test_auto_sticker_uses_library(tmp_path):
    """模型没发时，服务端从库里挑一张补上。"""
    manager, cache = _manager_with_stickers(tmp_path)
    cache.save("sad1", b"GIF89a" + b"s" * 16, 200, 150, name="麻了",
               url_hint="https://media.giphy.com/a.gif", content_type="image/gif")
    cache.remember_query("无语", "sad1")
    manager.chat = []
    picked = manager._auto_sticker("麻了")
    # 内置图优先（手挑的二次元图，风格比库里的 GIPHY 图更贴）
    assert picked == {"builtin": "无语"}, picked


def test_auto_sticker_respects_cooldown(tmp_path):
    """刚贴过图 → 短时间不再自动贴，避免刷屏。

    冷却用独立的时间戳列表，不翻 self.chat：并发 2 轮时上一条回复还在
    running、content 是空字符串，翻 chat 永远看不到上一张图（线上实测
    连发三条都贴了）。
    """
    import time as _time
    manager, cache = _manager_with_stickers(tmp_path)
    cache.save("sad1", b"GIF89a" + b"s" * 16, 200, 150,
               url_hint="https://media.giphy.com/a.gif", content_type="image/gif")
    cache.remember_query("无语", "sad1")
    manager._recent_sticker_ts = [int(_time.time() * 1000)]
    assert manager._auto_sticker("麻了") is None, "冷却期内不该再贴"


def test_auto_sticker_after_cooldown_expires(tmp_path):
    """冷却过期后可以再贴。"""
    import time as _time
    from ai.agent import STICKER_COOLDOWN_MS
    manager, cache = _manager_with_stickers(tmp_path)
    cache.save("sad1", b"GIF89a" + b"s" * 16, 200, 150,
               url_hint="https://media.giphy.com/a.gif", content_type="image/gif")
    cache.remember_query("无语", "sad1")
    manager._recent_sticker_ts = [int(_time.time() * 1000) - STICKER_COOLDOWN_MS - 1000]
    assert manager._auto_sticker("麻了") is not None


def test_auto_sticker_falls_back_to_builtin(tmp_path):
    """库里没有匹配的情绪 → 用内置图（现在内置图是首选，风格也最贴）。

    这条测试的前提变了：以前内置只有 3 张自绘图，和 GIPHY 贴纸风格差太远，
    所以"库里没匹配就不发"。现在内置有 13 张手挑的二次元梗图，
    风格反而是最对的，应该优先用它。
    """
    manager, cache = _manager_with_stickers(tmp_path)
    cache.save("other", b"GIF89a" + b"o" * 16, 100, 100,
               url_hint="https://media.giphy.com/a.gif", content_type="image/gif")
    cache.remember_query("popcorn", "other")
    manager.chat = []
    assert manager._auto_sticker("麻了") == {"builtin": "无语"}


def test_emotion_reply_gets_sticker_end_to_end(tmp_path, monkeypatch):
    """端到端：用户只说"麻了"，模型没调工具，服务端也要补一张图。"""
    from tests.test_agent import make_manager, tool_call, tool_result, wait_for

    def provider(*args, **kwargs):
        if kwargs.get("tool_choice") == "required":
            return tool_result(tool_call("read_market", {}, "c1"))
        return {"ok": True, "reply": "麻了就麻着吧。",
                "usage": {"total_tokens": 5}, "model": "test-model"}

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch, provider=provider)
        cache = StickerCache(str(tmp_path / "s"))
        cache.save("sad1", b"GIF89a" + b"s" * 16, 200, 150,
                   url_hint="https://media.giphy.com/a.gif", content_type="image/gif")
        cache.remember_query("无语", "sad1")
        manager.sticker_cache = cache
        await manager.ask("麻了", "test-model")
        assert await wait_for(lambda: not manager.chat_runners)
        answer = [m for m in manager.chat if m.get("role") == "assistant"][-1]
        assert "[sticker:" in (answer.get("content") or ""), answer.get("content")
        await manager.stop()

    asyncio.run(scenario())


# ---------------- L. read_situation：综合局势判断，而不是关键词 ----------------

def _snapshot(market=None, evals=None, positions=None, events=None):
    return {
        "ts": 1,
        "market": market or {},
        "evaluations": evals or {},
        "positions": positions or [],
        "recentEvents": events or [],
    }


def test_situation_reports_losing_positions():
    """有持仓在亏 → hint 要明确"别开玩笑"。"""
    from ai.agent_tools import _situation_view
    view = _situation_view(_snapshot(
        market={"btcusdt": {"roc": 0.002, "atrPct": 0.5}},
        positions=[{"symbol": "btcusdt", "side": "BUY", "upnl": -12.5},
                   {"symbol": "ethusdt", "side": "SELL", "upnl": -3.0}],
    ))
    assert view["positions"]["count"] == 2
    assert view["positions"]["losing"] == 2
    assert "别开玩笑" in view["hint"]


def test_situation_empty_position_is_relaxed():
    """空仓 + 无信号 → 可以放松。"""
    from ai.agent_tools import _situation_view
    view = _situation_view(_snapshot(
        market={"btcusdt": {"roc": 0.0001, "atrPct": 0.2}}))
    assert view["positions"]["count"] == 0
    assert "放松" in view["hint"]


def test_situation_detects_volatility():
    from ai.agent_tools import _situation_view
    view = _situation_view(_snapshot(
        market={"btcusdt": {"roc": 0.02, "atrPct": 1.4}}))
    assert view["marketTone"]["volatile"] is True
    assert "波动很大" in view["hint"]


def test_situation_lists_strong_signals_only():
    from ai.agent_tools import _situation_view
    view = _situation_view(_snapshot(
        market={"a": {}, "b": {}, "c": {}},
        evals={"a": {"direction": "bullish", "score": 75},
               "b": {"direction": "bearish", "score": 40},
               "c": {"direction": "bullish", "score": 58}}))
    assert [s["symbol"] for s in view["signals"]] == ["A"]
    assert view["signals"][0]["score"] == 75


def test_situation_surfaces_recent_issues():
    from ai.agent_tools import _situation_view
    view = _situation_view(_snapshot(events=[
        {"kind": "error", "reason": "API 限频"},
        {"kind": "system", "reason": "正常启动"},
        {"kind": "slippage", "reason": "滑点过大"},
    ]))
    assert "API 限频" in view["recentIssues"]
    assert all("正常启动" not in x for x in view["recentIssues"])


def test_read_situation_tool_registered():
    from ai.agent_tools import TOOL_NAMES
    assert "read_situation" in TOOL_NAMES


def test_read_situation_returns_view():
    from ai.agent_tools import read_snapshot
    out = read_snapshot("read_situation", {}, _snapshot(
        market={"btcusdt": {"roc": 0.01, "atrPct": 0.9}}))
    assert out["available"] is True
    assert out["data"]["marketTone"]["bias"] in ("偏强", "偏弱", "分化/横盘")


def test_model_consulted_situation_skips_keyword_fallback(tmp_path, monkeypatch):
    """模型已经看过局势 → 它决定不发就别用关键词硬塞。

    这正是"关键词触发"被推翻的核心：会不会该发要综合行情/持仓/信号，
    模型看过这些数据还没发，说明它判断此刻不该发。
    """
    from tests.test_agent import make_manager, tool_call, tool_result, wait_for

    def provider(*args, **kwargs):
        if kwargs.get("tool_choice") == "required":
            return tool_result(tool_call("read_situation", {}, "s1"))
        return {"ok": True, "reply": "麻了就先别看了。",
                "usage": {"total_tokens": 5}, "model": "test-model"}

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch, provider=provider,
                                  context=lambda: _snapshot(
                                      market={"btcusdt": {"roc": 0.01, "atrPct": 0.9}}))
        cache = StickerCache(str(tmp_path / "s"))
        cache.save("sad1", b"GIF89a" + b"s" * 16, 200, 150,
                   url_hint="https://media.giphy.com/a.gif", content_type="image/gif")
        cache.remember_query("无语", "sad1")
        manager.sticker_cache = cache
        await manager.ask("麻了", "test-model")
        assert await wait_for(lambda: not manager.chat_runners)
        answer = [m for m in manager.chat if m.get("role") == "assistant"][-1]
        assert "[sticker:" not in (answer.get("content") or ""), \
            f"模型看过局势且没发，不该被关键词兜底塞图: {answer.get('content')!r}"
        await manager.stop()

    asyncio.run(scenario())


def test_keyword_fallback_still_works_without_consultation(tmp_path, monkeypatch):
    """模型压根没管表情这件事 → 关键词兜底仍要生效（否则又回到"从不发"）。"""
    from tests.test_agent import make_manager, tool_call, tool_result, wait_for

    def provider(*args, **kwargs):
        if kwargs.get("tool_choice") == "required":
            return tool_result(tool_call("read_market", {}, "m1"))
        return {"ok": True, "reply": "麻了。",
                "usage": {"total_tokens": 5}, "model": "test-model"}

    async def scenario():
        manager, _ = make_manager(tmp_path, monkeypatch, provider=provider)
        cache = StickerCache(str(tmp_path / "s"))
        cache.save("sad1", b"GIF89a" + b"s" * 16, 200, 150,
                   url_hint="https://media.giphy.com/a.gif", content_type="image/gif")
        cache.remember_query("无语", "sad1")
        manager.sticker_cache = cache
        await manager.ask("麻了", "test-model")
        assert await wait_for(lambda: not manager.chat_runners)
        answer = [m for m in manager.chat if m.get("role") == "assistant"][-1]
        assert "[sticker:" in (answer.get("content") or ""), answer.get("content")
        await manager.stop()

    asyncio.run(scenario())


def test_marker_is_never_truncated():
    """表情包标记比 SHORT_REPLY_CHARS 长，截断后前端就认不出来、会显示成乱码。"""
    from ai.agent import _trim_bubble, _sticker_marker
    marker = _sticker_marker({"url": "/agent-attachments/stickers/abcdefgh.gif",
                              "width": 480, "height": 311})
    assert len(marker) > 60, "前提：标记确实比限制长"
    out = _trim_bubble(marker)
    assert out == marker, f"标记被截断了: {out!r}"
    # 正文超长时也要保住标记
    long = "很长的正文，" * 30 + "\n" + marker
    out2 = _trim_bubble(long)
    assert marker in out2
    assert len(out2) < len(long)


def test_invalid_handwritten_marker_is_replaced():
    """模型手写 [sticker:笑死]（库里的中文名）时，要换成真标记。

    前端只认内置名或 url= 形式。手写的中文名渲染不出来，
    用户看到的就是一串认不出的文本（线上截图里的 `[sticker:笑死]`）。
    """
    from ai.agent import split_reply
    sticker = {"url": "/agent-attachments/stickers/x1.gif",
               "width": 480, "height": 311, "id": "x1"}
    # 用一个确实不存在的名字（笑死 现在是合法内置名了）
    reply = "[sticker:瞎编的名字]"
    out = _attach_sticker(split_reply(reply, detailed=False), sticker, reply)
    assert len(out) == 1
    assert "url=" in out[0], out
    assert "瞎编的名字" not in out[0]


def test_valid_handwritten_marker_is_kept():
    """模型写的是有效标记（内置名）→ 不重复追加。"""
    from ai.agent import split_reply
    sticker = {"url": "/agent-attachments/stickers/x1.gif",
               "width": 480, "height": 311, "id": "x1"}
    reply = "[sticker:崩溃]"
    out = _attach_sticker(split_reply(reply, detailed=False), sticker, reply)
    assert out == ["[sticker:崩溃]"]


def test_marker_validity_check():
    from ai.agent import _has_valid_sticker_marker
    assert _has_valid_sticker_marker("[sticker:崩溃]")
    assert _has_valid_sticker_marker("[sticker:url=%2Fa.gif&w=1&h=1]")
    assert not _has_valid_sticker_marker("[sticker:瞎编的名字]")
    assert not _has_valid_sticker_marker("普通文字")


# ---------- 中文梗词 & 署名 ----------

def test_giphy_uses_chinese_lang_for_cjk_query():
    """中文查询词要用 zh-CN 标签体系。

    线上实测：一律 lang=en 时，搜 dead inside / shrug 拿到的是欧美 meme，
    跟中文对话完全不搭。用户要的是"趣味表情包"（中文二次元/梗图）。
    """
    import urllib.parse
    from feed import giphy

    captured = {}

    class _Resp:
        def read(self, n=-1):
            return b'{"data": []}'

        def close(self):
            pass

    monkeypatch_target = giphy._open
    try:
        giphy._open = lambda req, proxy, timeout: (captured.update(
            url=req.full_url), _Resp())[1]
        try:
            giphy._call_api("无语", 3, "k", "", 5.0)
        except Exception:
            pass
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(captured["url"]).query)
        assert qs["lang"] == ["zh-CN"], qs.get("lang")

        captured.clear()
        try:
            giphy._call_api("shrug", 3, "k", "", 5.0)
        except Exception:
            pass
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(captured["url"]).query)
        assert qs["lang"] == ["en"], qs.get("lang")
    finally:
        giphy._open = monkeypatch_target


def test_emotion_query_is_chinese():
    """兜底情绪词必须用中文梗词，不能再用 dead inside / shrug。"""
    from ai.agent import _EMOTION_QUERY
    queries = [q for _, q in _EMOTION_QUERY]
    assert "无语" in queries and "笑死" in queries
    for english in ("dead inside", "crying laughing", "shrug", "thumbs up"):
        assert english not in queries, f"还在用英文标签: {english}"


def test_sticker_source_marks_giphy_for_credit():
    """只有 GIPHY 来源的贴纸才需要署名。

    GIPHY 的 API 条款要求展示其内容时标注 Powered by GIPHY；
    本地/用户提供的图不需要——不区分来源就会每张图下面挂一行。
    """
    from ai.agent import _sticker_marker
    giphy = _sticker_marker({"url": "/agent-attachments/stickers/a.gif",
                             "width": 200, "height": 150, "source": "giphy"})
    assert "src=giphy" in giphy
    local = _sticker_marker({"url": "/agent-attachments/stickers/b.gif",
                             "width": 200, "height": 150, "source": "local"})
    assert "src=giphy" not in local


def test_cache_save_records_source(tmp_path):
    from ai.sticker_cache import StickerCache
    cache = StickerCache(str(tmp_path / "s"))
    rec = cache.save("local1", b"GIF89a" + b"x" * 16, 100, 80,
                     url_hint="https://media.giphy.com/a.gif",
                     content_type="image/gif", source="local")
    assert rec["source"] == "local"
    assert cache.catalog()[0]["source"] == "local"


# ---------- 内置图要能被看见、被发送 ----------

def test_list_includes_builtin_stickers(tmp_path):
    """内置图必须出现在清单里，排在库前面。

    线上实测：list_stickers 只列数据库里的 7 张（全是 dead inside /
    shrug 搜来的英文 meme），Agent 根本不知道 web/public/stickers/ 下
    还有三张手挑的二次元图——每次都被迫去 GIPHY 搜，风格全不对。
    """
    manager, _ = _manager_with_stickers(tmp_path)
    out = manager._list_stickers({})
    ids = [x["id"] for x in out["stickers"]]
    assert ids[:3] == ["builtin:崩溃", "builtin:疑问", "builtin:嘲讽"], ids
    for item in out["stickers"][:3]:
        assert item["source"] == "builtin"
        assert item["when"], "每张内置图都要有使用场景"
        assert item["whenMissing"] is False


def test_send_builtin_by_prefixed_id(tmp_path):
    """按 list 里给的 builtin:名字 发送要能成功。"""
    manager, _ = _manager_with_stickers(tmp_path)
    target = {}
    out = asyncio.run(manager._send_sticker({"sticker_id": "builtin:崩溃"}, target))
    assert out["sent"] is True and out["source"] == "builtin"
    assert target["pendingSticker"] == {"builtin": "崩溃"}


def test_builtin_catalog_filters_by_query():
    from ai.agent_tools import _builtin_sticker_catalog
    names = [x["name"] for x in _builtin_sticker_catalog("亏")]
    assert "崩溃" in names, names
    assert _builtin_sticker_catalog("完全不存在的词") == []
    # 不带 query 时全部都在
    from ai.agent_tools import BUILTIN_STICKERS
    assert len(_builtin_sticker_catalog()) == len(BUILTIN_STICKERS)


# ---------- 表情包触发规则：情绪优先于交易词 ----------

def test_outburst_beats_trading_words():
    """用户在骂人的时候提到"平掉/止损/强平"，那是情绪，不是在问价位。

    线上实测：用户连发
      「挂上啊，止损打到强平线旁边，就是空他，我不信了」
      「平掉了你重新开啊，我操你妈了」
    两句都被 _SERIOUS_TRADING（含"止损/平掉"）拦成"认真问行情"→ 一张图都不发。
    对方正在气头上，最该配图的时候反而沉默。
    """
    from ai.agent import _auto_sticker_query
    assert _auto_sticker_query("平掉了你重新开啊，我操你妈了") == "生气"
    assert _auto_sticker_query("挂上啊，止损打到强平线旁边，就是空他，我不信了") == "生气"
    assert _auto_sticker_query("妈的又亏了")  # 任意非空


def test_trading_questions_still_get_no_sticker():
    """正常问行情不能被情绪词误伤——这里曾经有个恒真的正则。"""
    from ai.agent import _auto_sticker_query
    for text in ("止损挂上了没", "BTC现在能做吗", "帮我看看BTC的4小时线怎么走",
                 "这个币止损放哪合适", "牛市怎么走", "分析一下ETH",
                 "BTC 4小时线怎么看", "好的"):
        assert _auto_sticker_query(text) == "", f"{text} 不该发图"


def test_social_cue_regex_has_no_empty_branch():
    """多行 `|` 拼接会生成 `||` 空分支，正则就恒真。

    原代码：
        r"...|"
        r"|..."
    拼起来是 "…||…" —— 空分支匹配一切，于是 _SOCIAL_CUE.search() 永远 True，
    连"BTC现在能做吗"都被判成要发表情包。
    """
    from ai.agent import _SOCIAL_CUE
    assert not _SOCIAL_CUE.search("zzz完全不相关的句子xyz")
    assert _SOCIAL_CUE.search("笑死")
    assert _SOCIAL_CUE.search("？")


def test_prompt_teaches_comfort_before_facts():
    """用户上火时要先接情绪，不能念日志。

    线上实测（用户截图）：他骂"平掉了你重新开啊，我操你妈了"，
    Agent 回了三段技术说明：
      「那个仓上一轮已经按 2420.34 平掉了，零盈亏。」
      「开不了。上一单的成交记录里写着原因：**保护单缺失且补挂失败**…」
      「再开一次还是同一个精度报错…」
    一句都没接住情绪，还编了个已经修好的旧 bug。
    """
    from ai.agent import SYSTEM_PROMPT
    for phrase in ("用户上火的时候，先接情绪，再讲事实",
                   "绝不能在别人骂你的时候",
                   "先接一句，短的、带态度的",
                   "那个仓上一轮已经按"):
        assert phrase in SYSTEM_PROMPT, f"缺少: {phrase}"
