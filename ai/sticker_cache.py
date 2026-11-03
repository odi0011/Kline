"""表情包落盘缓存：在线贴纸用一次就存到服务器，之后直接读本地文件。

为什么不直接热链 GIPHY CDN（`images.original.url`）：

* **浏览器在国内可能打不开 giphy.com 的 CDN**。API 由服务器调用没问题，
  但 `<img src="https://media.giphy.com/...">` 是在用户浏览器里加载的，
  那一段照样会被墙——表现就是"表情包永远是破图"。
* **免费 key 只有 100 次/小时**。同一张图反复命中时不该再花配额。
* 本地静态文件比跨境拉图快一个数量级。

所以流程是「搜一次 → 下载落盘 → 记住 id 和 query → 之后命中直接返回本地地址」。
落盘位置在 `agent-attachments/stickers/`，由 main.py 的静态服务直接对外提供
（它已经把 gif/webp/png/jpg 列入白名单，无需额外路由）。

索引是一个 JSON：`{"stickers": {id: {...}}, "queries": {"情绪词": id}}`。
查询级映射让"同一个情绪词"完全不用再打 API——这就是配额最省的地方。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

INDEX_NAME = "index.json"
# 服务器上留多少张。单张贴纸通常 100KB~2MB，200MB 上限对服务器很轻，
# 按字节淘汰（而不是按张数）才不会因为混进几个大图就爆盘。
MAX_BYTES = 200 * 1024 * 1024
MAX_STICKERS = 4000
# 命中记录多久算过期：只是"这个词该用哪张图"的偏好，不用太久也不会太短
QUERY_TTL = 90 * 86400
_SAFE_EXT = (".gif", ".webp", ".png", ".jpg", ".jpeg")


def _norm_query(query) -> str:
    return re.sub(r"\s+", " ", str(query or "")).strip().lower()[:60]


def _ext_of(url: str, content_type: str = "") -> str:
    """从 content-type 或 URL 后缀判断扩展名；静态服务只认白名单里的这几种。"""
    ctype = str(content_type or "").lower()
    for mime, ext in (("image/gif", ".gif"), ("image/webp", ".webp"),
                      ("image/png", ".png"), ("image/jpeg", ".jpg")):
        if mime in ctype:
            return ext
    path = str(url or "").split("?", 1)[0].lower()
    for ext in _SAFE_EXT:
        if path.endswith(ext):
            return ext
    return ""     # 认不出来就不存（可能是 mp4，<img> 不支持）


def _safe_id(sticker_id: str) -> str:
    """GIPHY 的 id 只含字母数字；统一清洗后当文件名，防止路径穿越。"""
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", str(sticker_id or ""))[:64]
    return cleaned


class StickerCache:
    """线程安全的贴纸落盘缓存（搜索/下载在 to_thread 里跑，必须加锁）。

    **两处存储，各司其职**（不是重复）：
      · MySQL `stickers` 表 —— Agent 要用的真实数据：库里有哪些表情、各自的
        情绪词、尺寸、用过几次。模型能直接列出/按 id 发送。
      · 本地 index.json —— 文件级映射（id→文件名）+ 搜索结果的缓存，
        没连数据库时也能独立工作。

    表是权威来源：启动时会用表里的记录重建本地索引，文件丢了也补得回来。
    """

    def __init__(self, root: str, store=None):
        self.root = root
        # store 是 storage.Storage；None（或未连库）时只靠本地 JSON。
        self.store = store
        self._lock = threading.RLock()
        self._index: dict = {"stickers": {}, "queries": {}}
        self._load()
        self._sync_from_store()

    def _sync_from_store(self) -> None:
        """用数据库里的记录重建本地索引（文件在、索引丢的恢复路径）。"""
        store = self.store
        if store is None or not getattr(store, "enabled", False):
            return
        try:
            rows = store.stickers(limit=1000)
        except Exception:
            return
        changed = False
        with self._lock:
            for row in rows or []:
                sid = _safe_id(row.get("id"))
                file = str(row.get("file") or "")
                if not sid or not file:
                    continue
                if not os.path.exists(os.path.join(self.root, file)):
                    continue      # 文件真没了，交给下面的自愈/淘汰逻辑
                record = {
                    "id": sid, "file": file,
                    "width": int(row.get("width") or 0),
                    "height": int(row.get("height") or 0),
                    "title": str(row.get("title") or "")[:120],
                    "url": f"/agent-attachments/stickers/{file}",
                    "bytes": int(row.get("bytes") or 0),
                    "ts": int(row.get("createdTs") or 0) // 1000 or int(time.time()),
                }
                if self._index["stickers"].get(sid) != record:
                    self._index["stickers"][sid] = record
                    changed = True
                for word in str(row.get("tags") or "").split():
                    key = _norm_query(word)
                    if key and key not in self._index["queries"]:
                        self._index["queries"][key] = {"id": sid,
                                                       "ts": int(time.time())}
                        changed = True
        if changed:
            self._save()

    # ---------- 索引 ----------

    def _index_path(self) -> str:
        return os.path.join(self.root, INDEX_NAME)

    def _load(self) -> None:
        try:
            with open(self._index_path(), encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                stickers = data.get("stickers")
                queries = data.get("queries")
                if isinstance(stickers, dict):
                    self._index["stickers"] = {
                        k: v for k, v in stickers.items() if isinstance(v, dict)}
                if isinstance(queries, dict):
                    self._index["queries"] = {
                        k: v for k, v in queries.items() if isinstance(v, dict)}
        except (OSError, ValueError):
            # 索引坏了就当空缓存：贴纸是锦上添花，绝不能因此起不来
            pass

    def _save(self) -> None:
        """原子写索引：tmp → replace，避免半截 JSON 让下次启动读不出来。"""
        path = self._index_path()
        tmp = f"{path}.tmp.{os.getpid()}"
        try:
            os.makedirs(self.root, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self._index, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # ---------- 查询 ----------

    @staticmethod
    def _public(record: dict) -> dict:
        return {"url": record.get("url", ""), "width": int(record.get("width") or 0),
                "height": int(record.get("height") or 0),
                "title": record.get("title", ""), "id": record.get("id", "")}

    def lookup(self, query) -> dict | None:
        """按情绪词找已落盘的贴纸；命中直接返回本地地址（不打 API、不下载）。"""
        key = _norm_query(query)
        if not key:
            return None
        with self._lock:
            entry = self._index["queries"].get(key)
            if not isinstance(entry, dict):
                return None
            if time.time() - float(entry.get("ts") or 0) > QUERY_TTL:
                self._index["queries"].pop(key, None)
                return None
            record = self._index["stickers"].get(str(entry.get("id") or ""))
            if not isinstance(record, dict):
                return None
            # 文件被手工删了/构建覆盖了 → 当成未命中，重新去搜
            if not os.path.exists(os.path.join(self.root, str(record.get("file") or ""))):
                self._index["queries"].pop(key, None)
                return None
            return self._public(record)

    def lookup_id(self, sticker_id) -> dict | None:
        sid = _safe_id(sticker_id)
        if not sid:
            return None
        with self._lock:
            record = self._index["stickers"].get(sid)
            if not isinstance(record, dict):
                return None
            if not os.path.exists(os.path.join(self.root, str(record.get("file") or ""))):
                self._index["stickers"].pop(sid, None)
                return None
            return self._public(record)

    # ---------- 清单（给 Agent 看的真实数据） ----------

    def catalog(self, query: str = "", limit: int = 120) -> list[dict]:
        """表情包清单：库里有什么、各自对应什么情绪、用过几次。

        这是给模型看的：它可以先看清单再决定发哪张（按 id），
        而不是只能盲猜一个 emotion 词。有数据库时以表为准（含使用次数），
        没连库时退回本地索引，字段保持一致。
        """
        store = self.store
        rows: list[dict] = []
        if store is not None and getattr(store, "enabled", False):
            try:
                rows = store.stickers(query=query, limit=limit)
            except Exception:
                rows = []
        if rows:
            out = []
            for row in rows:
                sid = _safe_id(row.get("id"))
                file = str(row.get("file") or "")
                if not sid or not file:
                    continue
                if not os.path.exists(os.path.join(self.root, file)):
                    continue
                out.append({
                    "id": sid,
                    "name": str(row.get("name") or "")[:80],
                    "when": str(row.get("whenToUse") or "")[:200],
                    "tags": str(row.get("tags") or "")[:200],
                    "width": int(row.get("width") or 0),
                    "height": int(row.get("height") or 0),
                    "uses": int(row.get("useCount") or 0),
                })
            return out[:limit]
        # 没连库：用本地索引拼一份同样形状的
        want = _norm_query(query)
        with self._lock:
            words: dict[str, list[str]] = {}
            for key, entry in self._index["queries"].items():
                if isinstance(entry, dict):
                    words.setdefault(str(entry.get("id") or ""), []).append(key)
            out = []
            for sid, record in self._index["stickers"].items():
                if not isinstance(record, dict):
                    continue
                if not os.path.exists(os.path.join(self.root, str(record.get("file") or ""))):
                    continue
                tags = " ".join(sorted(words.get(sid, [])))[:200]
                name = str(record.get("name") or "")[:80]
                when = str(record.get("whenToUse") or "")[:200]
                if want and want not in f"{tags} {name} {when}".lower():
                    continue
                out.append({"id": sid, "name": name, "when": when, "tags": tags,
                            "width": int(record.get("width") or 0),
                            "height": int(record.get("height") or 0),
                            "uses": 0})
            out.sort(key=lambda x: x["id"])
            return out[:limit]

    def touch(self, sticker_id) -> None:
        """记一次使用（给"最近用过的排前面"和重复度判断用）。"""
        store = self.store
        if store is None or not getattr(store, "enabled", False):
            return
        try:
            store.touch_sticker(_safe_id(sticker_id))
        except Exception:
            pass

    # ---------- 本地挑选（配额用尽 / 断网时的退路） ----------

    def all_items(self) -> list[dict]:
        """所有已落盘的贴纸（带它记住的情绪词），供挑选时排序。"""
        with self._lock:
            out = []
            for sid, record in self._index["stickers"].items():
                if not isinstance(record, dict):
                    continue
                item = self._public(record)
                if not os.path.exists(os.path.join(self.root, str(record.get("file") or ""))):
                    continue
                item["savedAt"] = int(record.get("ts") or 0)
                out.append(item)
            return out

    def pick_for(self, query, avoid_id: str = "") -> dict | None:
        """在已存的表情里挑一张最贴近 query 的；一张都没有时返回 None。

        为什么要有这个：GIPHY 免费 key 只有 100 次/小时，用完了或者断网时，
        "明明该发表情却发不出去"是最差的结果。库里已经有图，就该从里面选。

        排序依据（命中优先级从高到低）：
          1) 记住的情绪词与 query 有词重叠（"dead inside" ↔ "inside dead"）
          2) 互为子串（"facepalm" ↔ "face palm"）
          3) 越新保存的越优先
        avoid_id 用于避开"上一次刚发过的那张"，免得同一张连发两次。
        """
        want = _norm_query(query)
        tokens = {t for t in re.split(r"[^a-z0-9]+", want) if len(t) > 1}
        with self._lock:
            # 先建立 贴纸 id -> 记住的情绪词 的反向表
            words: dict[str, list[str]] = {}
            for key, entry in self._index["queries"].items():
                if isinstance(entry, dict):
                    words.setdefault(str(entry.get("id") or ""), []).append(str(key))
            ranked = []
            for sid, record in self._index["stickers"].items():
                if not isinstance(record, dict):
                    continue
                if not os.path.exists(os.path.join(self.root, str(record.get("file") or ""))):
                    continue
                keys = words.get(sid, [])
                score = 0
                for key in keys:
                    key_tokens = {t for t in re.split(r"[^a-z0-9]+", key) if len(t) > 1}
                    score += 3 * len(tokens & key_tokens)
                    if want and (want in key or key in want):
                        score += 5
                if sid == _safe_id(avoid_id):
                    score -= 2          # 刚发过，除非没有别的选择
                ranked.append((score, int(record.get("ts") or 0), sid, record))
            if not ranked:
                return None
            # 先看有没有真正相关的；完全没有时退回最新一张（总比不发强）
            related = [row for row in ranked if row[0] > 0]
            pool = related or ranked
            pool.sort(key=lambda row: (row[0], row[1]), reverse=True)
            return self._public(pool[0][3])

    def remember_query(self, query, sticker_id) -> None:
        """记住"这个情绪词对应哪张图"。

        同时写本地索引和数据库——表里的 tags 就是靠这里累积起来的，
        Agent 之后看到的就是"这张图会用在 dead inside / 麻了 这类情绪上"。
        """
        key = _norm_query(query)
        sid = _safe_id(sticker_id)
        if not key or not sid:
            return
        with self._lock:
            self._index["queries"][key] = {"id": sid, "ts": int(time.time())}
            self._save()
            record = self._index["stickers"].get(sid)
            tags = sorted({k for k, v in self._index["queries"].items()
                           if isinstance(v, dict) and str(v.get("id")) == sid})
        store = self.store
        if store is not None and getattr(store, "enabled", False) and isinstance(record, dict):
            try:
                store.save_sticker({
                    "id": sid, "file": record.get("file"),
                    "title": record.get("title"),
                    "tags": " ".join(tags),
                    "width": record.get("width"), "height": record.get("height"),
                    "bytes": record.get("bytes"),
                    "ts": int(record.get("ts") or 0) * 1000,
                })
            except Exception:
                pass

    # ---------- 写入 ----------

    def save(self, sticker_id, data: bytes, width: int, height: int,
             title: str = "", url_hint: str = "", content_type: str = "",
             name: str = "", when_to_use: str = "") -> dict | None:
        """把下载好的图片存进缓存目录，返回可对外提供的本地记录。

        name / when_to_use 是 **Agent 决策出来的**（这张图叫什么、什么时候用），
        不是 GIPHY 的英文标题。存下来之后 list_stickers 会原样返回，
        下次 Agent 就能按"名字 + 使用场景"直接挑，不用再猜情绪词。

        扩展名认不出来（比如 GIPHY 返回 mp4）就放弃——`<img>` 放不了视频。
        """
        sid = _safe_id(sticker_id)
        ext = _ext_of(url_hint, content_type)
        if not sid or not ext or not data:
            return None
        if int(width or 0) <= 0 or int(height or 0) <= 0:
            return None
        filename = f"{sid}{ext}"
        path = os.path.join(self.root, filename)
        with self._lock:
            try:
                os.makedirs(self.root, exist_ok=True)
                tmp = f"{path}.tmp.{os.getpid()}"
                with open(tmp, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, path)
            except OSError:
                try:
                    os.unlink(f"{path}.tmp.{os.getpid()}")
                except OSError:
                    pass
                return None
            record = {"id": sid, "file": filename, "width": int(width),
                      "height": int(height), "title": str(title or "")[:120],
                      "name": str(name or "")[:80],
                      "whenToUse": str(when_to_use or "")[:300],
                      "url": f"/agent-attachments/stickers/{filename}",
                      "bytes": len(data), "ts": int(time.time())}
            self._index["stickers"][sid] = record
            self._prune_locked()
            self._save()
        # 同步进表：Agent 的清单要能看到这张图，以及它自己定的名字和使用场景。
        store = self.store
        if store is not None and getattr(store, "enabled", False):
            try:
                store.save_sticker({
                    "id": sid, "file": filename, "title": record["title"],
                    "name": record.get("name") or "",
                    "whenToUse": record.get("whenToUse") or "",
                    "tags": "", "width": record["width"], "height": record["height"],
                    "bytes": record["bytes"], "ts": record["ts"] * 1000,
                })
            except Exception:
                pass
        return self._public(record)

    def _prune_locked(self) -> None:
        """超量时按"最久没用到"淘汰。调用方已持锁。"""
        stickers = self._index["stickers"]
        if not stickers:
            return
        total = sum(int(v.get("bytes") or 0) for v in stickers.values() if isinstance(v, dict))
        if len(stickers) <= MAX_STICKERS and total <= MAX_BYTES:
            return
        ordered = sorted(stickers.items(), key=lambda kv: int(kv[1].get("ts") or 0))
        for sid, record in ordered:
            if len(stickers) <= MAX_STICKERS and total <= MAX_BYTES:
                break
            file = str(record.get("file") or "")
            if file:
                try:
                    os.unlink(os.path.join(self.root, file))
                except OSError:
                    pass
            total -= int(record.get("bytes") or 0)
            stickers.pop(sid, None)
        # 指向被删贴纸的查询映射也要清掉，否则永远命中不到
        alive = set(stickers)
        for key in [k for k, v in self._index["queries"].items()
                    if str(v.get("id") or "") not in alive]:
            self._index["queries"].pop(key, None)

    def stats(self) -> dict:
        with self._lock:
            return {"stickers": len(self._index["stickers"]),
                    "queries": len(self._index["queries"]),
                    "bytes": sum(int(v.get("bytes") or 0)
                                 for v in self._index["stickers"].values()
                                 if isinstance(v, dict))}

    def clear(self) -> None:
        """清空缓存（排障用）。"""
        with self._lock:
            for record in self._index["stickers"].values():
                file = str(record.get("file") or "")
                if file:
                    try:
                        os.unlink(os.path.join(self.root, file))
                    except OSError:
                        pass
            self._index = {"stickers": {}, "queries": {}}
            self._save()
