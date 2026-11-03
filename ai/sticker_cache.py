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
    """线程安全的贴纸落盘缓存（搜索/下载在 to_thread 里跑，必须加锁）。"""

    def __init__(self, root: str):
        self.root = root
        self._lock = threading.RLock()
        self._index: dict = {"stickers": {}, "queries": {}}
        self._load()

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

    def remember_query(self, query, sticker_id) -> None:
        key = _norm_query(query)
        sid = _safe_id(sticker_id)
        if not key or not sid:
            return
        with self._lock:
            self._index["queries"][key] = {"id": sid, "ts": int(time.time())}
            self._save()

    # ---------- 写入 ----------

    def save(self, sticker_id, data: bytes, width: int, height: int,
             title: str = "", url_hint: str = "", content_type: str = "") -> dict | None:
        """把下载好的图片存进缓存目录，返回可对外提供的本地记录。

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
                      "url": f"/agent-attachments/stickers/{filename}",
                      "bytes": len(data), "ts": int(time.time())}
            self._index["stickers"][sid] = record
            self._prune_locked()
            self._save()
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
