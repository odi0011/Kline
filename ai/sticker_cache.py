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


def _sniff_image(data: bytes) -> str:
    """按真实字节判断图片类型，返回扩展名；不是图片返回空串。

    不信 URL/Content-Type：GIPHY 的重定向没跟随时，返回的是 HTML 错误页，
    但静态服务仍会按 .gif 报 image/gif，浏览器就渲染成空白。
    """
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return ".gif"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    return ""


def _safe_id(sticker_id: str) -> str:
    """GIPHY 的 id 只含字母数字；统一清洗后当文件名，防止路径穿越。"""
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", str(sticker_id or ""))[:64]
    return cleaned


def _usable_image(data: bytes) -> bool:
    """这张图能不能当表情包用——内容上"有东西"才算。

    **线上真实事故**：GIPHY 用「无语」搜出来的第一张是一个**加载圈动画**
    （480×480、10 帧、99.6% 像素全透明，实际内容只有中间一个小圆圈）。
    它在白底上就是一片空白，用户看到的是"图片加载不出来"。
    从字节层面它完全合法 GIF，所以 `_sniff_image` 拦不住。

    判据：把首帧铺到白底上，看有多少像素**既不是透明也不是纯白**。
    低于 2% 就认为是一张"空图"——正常表情包不会这样。
    没有 Pillow 时一律放行（宁可少拦，也不能因为缺依赖让发表情挂掉）。
    """
    if not data:
        return False
    try:
        import io

        from PIL import Image
    except ImportError:
        return True
    try:
        im = Image.open(io.BytesIO(data))
        im.seek(0)
        rgba = im.convert("RGBA")
        # 大图缩到小尺寸再算，几十万像素全遍历太慢
        rgba.thumbnail((96, 96))
        pixels = list(rgba.getdata())
        if not pixels:
            return False
        solid = 0
        for r, g, b, a in pixels:
            if a < 16:
                continue                      # 透明：白底上看不见
            if r > 245 and g > 245 and b > 245:
                continue                      # 近白：白底上看不见
            solid += 1
        return solid / len(pixels) >= 0.02
    except Exception:
        # 解析失败不代表图坏了（可能是 Pillow 不认识的变体），放行
        return True


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
                if not self._file_ok({"file": file}):
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
                "title": record.get("title", ""), "id": record.get("id", ""),
                # 前端据此决定要不要显示 "Powered by GIPHY"（条款要求）
                "source": record.get("source") or "giphy"}

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
    def _file_ok(self, record: dict) -> bool:
        """这条记录指向的文件是否真是一张能显示的图片。

        只判断 exists 不够：线上曾经存进 HTML 错误页（GIPHY 302 没跟随），
        扩展名是 .gif、静态服务也报 image/gif，浏览器却渲染成空白。
        这里读前 16 字节按魔数确认；不合格的记录顺手清掉，避免反复踩。
        """
        filename = str(record.get("file") or "")
        if not filename:
            return False
        path = os.path.join(self.root, filename)
        try:
            with open(path, "rb") as handle:
                head = handle.read(16)
        except OSError:
            return False
        if not _sniff_image(head):
            return False
        # 内容层校验：线上有过"99.6% 透明的加载圈"被当表情缓存进库，
        # 白底上就是一片空白，用户看到的是"图加载不出来"。
        # 只校验一次（记录上打标记），别每次挑图都重新解码。
        if not record.get("usableChecked"):
            try:
                with open(path, "rb") as handle:
                    raw = handle.read(2_000_000)
                ok = _usable_image(raw)
            except OSError:
                return False
            if not ok:
                self._drop_record(str(record.get("id") or ""))
                return False
            record["usableChecked"] = True
        return True

    def _drop_record(self, sid: str) -> None:
        """删掉一条不可用的记录及其索引引用（调用方已持锁）。"""
        self._index["stickers"].pop(sid, None)
        for key in [k for k, v in self._index["queries"].items()
                    if isinstance(v, dict) and str(v.get("id")) == sid]:
            self._index["queries"].pop(key, None)
        self._save()

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
            sid = str(entry.get("id") or "")
            record = self._index["stickers"].get(sid)
            if not isinstance(record, dict):
                return None
            # 文件被删、或里面其实不是图片（旧版存进过 HTML 错误页）→ 当未命中
            if not self._file_ok(record):
                self._drop_record(sid)
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
            if not self._file_ok(record):
                self._drop_record(sid)
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
                if not self._file_ok({"file": file}):
                    continue
                out.append({
                    "id": sid,
                    "name": str(row.get("name") or "")[:80],
                    "when": str(row.get("whenToUse") or "")[:200],
                    "tags": str(row.get("tags") or "")[:200],
                    "width": int(row.get("width") or 0),
                    "height": int(row.get("height") or 0),
                    "uses": int(row.get("useCount") or 0),
                    "source": str(row.get("source") or "giphy"),
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
                if not self._file_ok(record):
                    continue
                tags = " ".join(sorted(words.get(sid, [])))[:200]
                name = str(record.get("name") or "")[:80]
                when = str(record.get("whenToUse") or "")[:200]
                if want and want not in f"{tags} {name} {when}".lower():
                    continue
                out.append({"id": sid, "name": name, "when": when, "tags": tags,
                            "width": int(record.get("width") or 0),
                            "height": int(record.get("height") or 0),
                            "uses": int(record.get("uses") or 0),
                            "source": record.get("source") or "giphy",
                            "_lastUsed": int(record.get("lastUsed") or 0)})
            # 没连库时也要按"最近用过"排：否则 touch() 的计数在清单里看不出效果
            out.sort(key=lambda x: (x.get("_lastUsed") or 0,
                                    x.get("uses") or 0,
                                    x["id"]), reverse=True)
            for row in out:
                row.pop("_lastUsed", None)
            return out[:limit]

    def touch(self, sticker_id) -> None:
        """记一次使用：最近发过的排在清单前面，重复度也靠它判断。

        本地索引和数据库都要记：
          · 数据库是权威来源（跨重启保留）
          · 本地索引是没连库时的唯一来源，不写它 use_count 会永远是 0
        """
        sid = _safe_id(sticker_id)
        if not sid:
            return
        now = int(time.time())
        with self._lock:
            record = self._index["stickers"].get(sid)
            if isinstance(record, dict):
                record["lastUsed"] = now
                record["uses"] = int(record.get("uses") or 0) + 1
                self._save()
        store = self.store
        if store is None or not getattr(store, "enabled", False):
            return
        try:
            store.touch_sticker(sid)
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
                if not self._file_ok(record):
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
                if not self._file_ok(record):
                    continue
                keys = words.get(sid, [])
                score = 0
                for key in keys:
                    key_tokens = {t for t in re.split(r"[^a-z0-9]+", key) if len(t) > 1}
                    score += 3 * len(tokens & key_tokens)
                    if want and (want in key or key in want):
                        score += 5
                last_used = int(record.get("lastUsed") or 0)
                # 刚用过 = 最近 10 分钟内发过。作为独立排序维度而不是减分：
                # 减固定分会被"相关性更高"直接盖过去，于是连发同一张。
                # 排序时先比相关性，相关性相同再让"刚用过的"靠后。
                used_recently = 1 if (last_used and time.time() - last_used < 600) else 0
                if sid == _safe_id(avoid_id):
                    used_recently = 1   # 这次明确要求避开，等同"刚用过"
                ranked.append((score, used_recently, int(record.get("ts") or 0), sid, record))
            if not ranked:
                return None
            # 先看有没有真正相关的；完全没有时退回最新一张（总比不发强）
            related = [row for row in ranked if row[0] > 0]
            pool = related or ranked
            # 相关性降序 → 同分时"刚用过的"靠后 → 再比新旧 → 最后按 id，
            # 让"完全不相关时退回哪张"是确定的：同秒保存的三张图如果
            # ts 相同，排序结果就取决于字典迭代顺序，测试和线上都会随机变
            # （线上表现是"同一句情绪，两次收到不同的图"）。
            # 注意是**同分才让位**：相关性明显更高的那张照样优先，
            # 否则会出现"明明有精确匹配却因为刚发过而选了不相关的"。
            pool.sort(key=lambda row: (row[0], -row[1], row[2], row[3]), reverse=True)
            best = pool[0]
            if best[1] and len(pool) > 1 and pool[1][0] == best[0]:
                best = pool[1]      # 同分且刚用过 → 换下一个
            return self._public(best[4])

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
                # 必须把 name/whenToUse 一起带上：save_sticker 是 REPLACE INTO，
                # 少传这两个字段会把 Agent 刚定好的名字和用途擦成空字符串。
                store.save_sticker({
                    "id": sid, "file": record.get("file"),
                    "title": record.get("title"),
                    "name": record.get("name") or "",
                    "whenToUse": record.get("whenToUse") or "",
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
             name: str = "", when_to_use: str = "",
             source: str = "giphy") -> dict | None:
        """把下载好的图片存进缓存目录，返回可对外提供的本地记录。

        name / when_to_use 是 **Agent 决策出来的**（这张图叫什么、什么时候用），
        不是 GIPHY 的英文标题。存下来之后 list_stickers 会原样返回，
        下次 Agent 就能按"名字 + 使用场景"直接挑，不用再猜情绪词。

        扩展名**按真实字节判断**，不信 URL/Content-Type：
        之前存进去的"GIF"实际是 HTML 错误页，静态服务照样按 .gif 返回
        image/gif，浏览器渲染成空白——正是这个坑。
        认不出是图片就直接拒绝，绝不把错误页写进库。
        """
        sid = _safe_id(sticker_id)
        ext = _ext_of(url_hint, content_type)
        if not sid or not ext or not data:
            return None
        if int(width or 0) <= 0 or int(height or 0) <= 0:
            return None
        # 字节层面确认真的是图；对不上的话按真实类型纠正扩展名
        real = _sniff_image(data)
        if not real:
            return None
        # 内容层面确认"有东西"：GIPHY 的加载圈/空白图在白底上等于没图
        if not _usable_image(data):
            return None
        ext = real
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
                      # 名字不能留空：库里出现无名表情时，清单里看不出它是干什么的。
                      # 没给名字就退回 GIPHY 的标题，至少能认。
                      "name": (str(name or "").strip()
                               or str(title or "").strip()[:80]),
                      "whenToUse": str(when_to_use or "")[:300],
                      "url": f"/agent-attachments/stickers/{filename}",
                      "bytes": len(data), "ts": int(time.time()),
                      # 来源：GIPHY 的内容按条款要署名，本地图不用
                      "source": str(source or "giphy")[:16]}
            self._index["stickers"][sid] = record
            self._prune_locked()
            self._save()
        # 同步进表：Agent 的清单要能看到这张图，以及它自己定的名字和使用场景。
        store = self.store
        if store is not None and getattr(store, "enabled", False):
            try:
                # tags 不能写死成空：重复保存同一张时会把已累积的情绪词清掉。
                # 从本地索引里取回这张图已有的所有情绪词。
                with self._lock:
                    tags = sorted({k for k, v in self._index["queries"].items()
                                   if isinstance(v, dict)
                                   and str(v.get("id")) == sid})
                store.save_sticker({
                    "id": sid, "file": filename, "title": record["title"],
                    "name": record.get("name") or "",
                    "whenToUse": record.get("whenToUse") or "",
                    "tags": " ".join(tags),
                    "width": record["width"], "height": record["height"],
                    "bytes": record["bytes"], "ts": record["ts"] * 1000,
                    "source": record.get("source") or "giphy",
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
