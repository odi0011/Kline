"""Telegram Bot 接入：把对话接到 ODDD Agent。

关键设计：
- 长轮询（getUpdates），不需要公网回调地址；
- 与网页端共用同一份 AgentManager 会话，消息双向可见；
- 只接受白名单内的 chat id，未授权的会话一律拒绝并提示；
- token 存在 config.yaml 的 telegram 段，可在设置页修改后热重启轮询。
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import json
import os
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT = 35          # 长轮询本身的超时（秒）
POLL_TIMEOUT = 25     # getUpdates 在服务端挂起的时长
MAX_TEXT = 4000       # Telegram 单条消息上限是 4096，留余量
# 与网页端一致：单张图 2MB。Telegram 照片本来就压到 1MB 以内，
# 大文件（视频等）不进入对话链路，避免把状态和 WS 撑爆。
MAX_MEDIA_BYTES = 2_000_000


def _mime_from_path(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "webp": "image/webp", "gif": "image/gif"}.get(ext, "application/octet-stream")


class TelegramBot:
    def __init__(self, token: str = "", allowed: list[str] | None = None,
                 on_message=None, proxy: str | None = None,
                 sticker_dir: str = "", attach_dir: str = ""):
        self.token = (token or "").strip()
        self.allowed = {str(x).strip() for x in (allowed or []) if str(x).strip()}
        self.on_message = on_message          # async (chat_id, text) -> str
        self.proxy = proxy
        # 表情包：内置图目录（web/public/stickers）+ 落盘图目录（agent-attachments）
        self.sticker_dir = sticker_dir
        self.attach_dir = attach_dir
        self._offset = 0
        self._task: asyncio.Task | None = None
        self._running = False
        self.last_error = ""
        self.bot_info: dict = {}
        self.media_sent = 0        # 成功发出去的表情包数量（状态页可查）
        # 主动推送要发到哪：记住最近来消息的会话。
        # 白名单为空时 Telegram 不做限制，但"推给谁"仍需要具体 chat_id——
        # 只能从来信里学，没有别的来源。
        self.last_chats: list[str] = []

    # ---------- HTTP ----------

    def _request(self, method: str, params: dict | None = None) -> dict:
        if not self.token:
            return {"ok": False, "error": "未配置 token"}
        url = API.format(token=self.token, method=method)
        body = None
        if params:
            body = json.dumps(params).encode()
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"})
        opener = (urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy}))
            if self.proxy else urllib.request.build_opener())
        try:
            with opener.open(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            # 错误码在 body 里（-1004 会话不存在、-403 被用户拉黑、
            # -400 参数不对）。只回 "HTTP Error 400" 的话排查只能靠猜。
            try:
                raw = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                raw = ""
            return {"ok": False, "error": f"HTTP {exc.code}: {raw or exc.reason}"}
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def describe(self) -> dict:
        """给设置页显示的状态。"""
        return {
            "configured": bool(self.token),
            "running": self._running,
            "bot": self.bot_info,
            "allowed": sorted(self.allowed),
            "error": self.last_error,
        }

    def verify(self) -> dict:
        """校验 token，返回 bot 信息（设置页"测试连接"用）。"""
        res = self._request("getMe")
        if not res.get("ok"):
            self.last_error = str(res.get("error") or "token 无效")
            return {"ok": False, "error": self.last_error}
        self.bot_info = res.get("result") or {}
        self.last_error = ""
        return {"ok": True, "bot": self.bot_info}

    def send(self, chat_id, text: str) -> dict:
        text = str(text or "").strip() or "（空回复）"
        out = []
        # Telegram 单条 4096 字上限，超长按段发
        for start in range(0, len(text), MAX_TEXT):
            res = self._request("sendMessage", {
                "chat_id": chat_id,
                "text": text[start:start + MAX_TEXT],
                "disable_web_page_preview": True,
            })
            if not res.get("ok"):
                return res
            out.append(res)
        return {"ok": True, "sent": len(out)}

    # ---------- 出站表情包 ----------

    def remember_chat(self, chat_id) -> None:
        """记下最近来过信的会话（主动推送用）。最多留 5 个，最近的排前。"""
        key = str(chat_id)
        if not key:
            return
        rest = [c for c in self.last_chats if c != key]
        self.last_chats = [key] + rest[:4]

    def push(self, text: str) -> dict:
        """主动推进会话 —— 不是回复，是"我找你说句话"。

        发给谁：配置了白名单就发给白名单里的每个会话；否则发给
        **最近来消息的那几个**。Telegram 不允许 bot 主动给陌生人发消息，
        所以从来没交互过的会话这里也发不出去，这是平台限制。
        """
        targets = sorted(self.allowed) or list(self.last_chats)
        if not targets:
            return {"ok": False, "error": "还没有任何会话来过消息，无法主动推送"}
        results = []
        for chat_id in targets[:5]:
            res = self.send_reply(chat_id, text)
            results.append({"chat": chat_id, "ok": bool(res.get("ok"))})
        return {"ok": any(r["ok"] for r in results), "results": results}

    def _upload(self, method: str, field: str, chat_id, path: str,
                mime: str) -> dict:
        """multipart 上传本地文件（sendPhoto / sendAnimation）。

        为什么不用 URL：Telegram 服务器要能公网访问到那个地址。内置图在
        web/dist 下是有公网地址的，但落盘的 GIPHY 图在 agent-attachments，
        走本站静态服务也能取——可一旦部署在内网/换了域名就全挂。
        直接传字节最稳，也不依赖静态服务是否对外。
        """
        try:
            with open(path, "rb") as fh:
                raw = fh.read(MAX_MEDIA_BYTES + 1)
        except OSError as exc:
            return {"ok": False, "error": f"读不到文件: {exc}"}
        if len(raw) > MAX_MEDIA_BYTES:
            return {"ok": False, "error": "文件超过 2MB"}
        boundary = "----oddd" + os.urandom(8).hex()
        name = os.path.basename(path)
        chunks = []
        chunks.append(
            (f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\""
             f"\r\n\r\n{chat_id}\r\n").encode())
        chunks.append(
            (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\";"
             f" filename=\"{name}\"\r\nContent-Type: {mime}\r\n\r\n").encode())
        chunks.append(raw)
        chunks.append(f"\r\n--{boundary}--\r\n".encode())
        data = b"".join(chunks)
        url = API.format(token=self.token, method=method)
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        opener = (urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy}))
            if self.proxy else urllib.request.build_opener())
        try:
            with opener.open(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            # **必须读响应体**：Telegram 把错误码放在 body 里
            # （-1004 会话不存在 / -400 文件格式 / -413 文件太大）。
            # 只回 "HTTP Error 400" 的话，上层根本没法区分是"图有问题"
            # 还是"chat_id 不对"，排查只能靠猜。
            try:
                body = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                body = ""
            return {"ok": False, "error": f"HTTP {exc.code}: {body or exc.reason}"}
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def send_sticker(self, chat_id, ref) -> dict:
        """发一张表情包。本地文件走上传，远程 GIPHY 直链让 Telegram 自己取。"""
        if not ref or not getattr(ref, "available", False):
            return {"ok": False, "error": "表情不可用"}
        method = "sendAnimation" if ref.kind == "animation" else "sendPhoto"
        field = "animation" if method == "sendAnimation" else "photo"
        if ref.local_path:
            mime = _mime_from_path(ref.local_path)
            res = self._upload(method, field, chat_id, ref.local_path, mime)
        else:
            res = self._request(method, {"chat_id": chat_id, field: ref.remote_url})
        if res.get("ok"):
            self.media_sent += 1
        else:
            self.last_error = str(res.get("error") or res.get("description") or "发送表情失败")
        return res

    def send_reply(self, chat_id, text: str) -> dict:
        """发一条完整回复：正文 + 表情包。

        必须先把标记从正文里剥掉再发文字——否则用户看到的就是
        `[sticker:崩溃]` 这串代码（线上实测就是这样）。
        只有表情没有正文时不再发一条"（空回复）"，直接发图。
        """
        from ai.stickers_media import split_stickers
        body, refs = split_stickers(text, self.sticker_dir, self.attach_dir)
        sent_text = {"ok": True}
        if body:
            sent_text = self.send(chat_id, body)
            if not sent_text.get("ok"):
                return sent_text
        errors = []
        for ref in refs:
            res = self.send_sticker(chat_id, ref)
            if not res.get("ok"):
                errors.append(str(res.get("error") or "发送失败")[:80])
        if errors and not body:
            # 正文和表情都没发出去：至少让用户知道为什么，别静默吞掉
            self.send(chat_id, "表情发送失败：" + "；".join(errors[:2]))
        return {"ok": True, "text": bool(body), "stickers": len(refs),
                "errors": errors}

    # ---------- 入站媒体 ----------

    def download(self, file_id: str) -> tuple[bytes, str] | None:
        """按 file_id 取回文件内容，返回 (原始字节, mime)。失败返回 None。

        两步：getFile 拿临时路径，再从 /file/bot<token>/<path> 下载。
        下载地址里带 token，所以只在内存里拼，不落日志。
        """
        meta = self._request("getFile", {"file_id": file_id})
        if not meta.get("ok"):
            self.last_error = str(meta.get("error") or "取文件失败")
            return None
        path = str(((meta.get("result") or {}).get("file_path")) or "")
        if not path:
            return None
        url = f"https://api.telegram.org/file/bot{self.token}/{path}"
        opener = (urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy}))
            if self.proxy else urllib.request.build_opener())
        try:
            with opener.open(url, timeout=TIMEOUT) as resp:
                raw = resp.read(MAX_MEDIA_BYTES + 1)
                if len(raw) > MAX_MEDIA_BYTES:
                    return None
                ctype = str(resp.headers.get("Content-Type") or "").split(";")[0].strip()
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None
        if not ctype or ctype == "application/octet-stream":
            ctype = _mime_from_path(path)
        return raw, ctype

    def read_incoming(self, message: dict) -> tuple[str, list[dict]]:
        """从一条 update 里取出文字与附件，结构对齐网页端。

        网页端发的是 {kind, name, mime, data: "data:...;base64,..."}，
        这里产出同样的形状，后续 Agent 链路完全不用分渠道。
        """
        text = str(message.get("text") or message.get("caption") or "").strip()
        # 单张图片：document 也可能是图片（用户以文件形式发送）
        candidates = []
        photos = message.get("photo") or []
        if photos:
            # 只取最大的一档，Telegram 会按尺寸给多个版本
            best = max(photos, key=lambda p: int(p.get("file_size") or 0))
            candidates.append((best.get("file_id"), f"photo_{best.get('file_unique_id', '')[:8]}.jpg",
                               "image/jpeg"))
        doc = message.get("document")
        if isinstance(doc, dict) and doc.get("file_id"):
            candidates.append((doc.get("file_id"), str(doc.get("file_name") or "document"),
                               str(doc.get("mime_type") or "")))
        for file_id, name, mime in candidates:
            if not file_id:
                continue
            got = self.download(file_id)
            if not got:
                continue
            raw, real_mime = got
            mime = real_mime or mime
            kind = "image" if mime.startswith("image/") else "text"
            if kind == "text" and not (
                    mime.startswith("text/") or name.lower().endswith((".txt", ".md", ".csv", ".json", ".log", ".yaml", ".yml"))):
                continue                      # 不支持的二进制类型：跳过，下方会提示
            data = "data:%s;base64,%s" % (mime or "application/octet-stream",
                                          base64.b64encode(raw).decode("ascii"))
            return text, [{"kind": kind, "name": name[:80], "mime": mime, "data": data}]
        return text, []

    # ---------- 轮询 ----------

    async def _poll_loop(self) -> None:
        self._running = True
        while True:
            try:
                res = await asyncio.to_thread(self._get_updates)
                if not res.get("ok"):
                    self.last_error = str(res.get("error") or "拉取失败")
                    # token 错误时不要空转，退避久一点
                    await asyncio.sleep(10 if "401" in self.last_error else 3)
                    continue
                self.last_error = ""
                for update in res.get("result") or []:
                    self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
                    asyncio.create_task(self._handle_update(update))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(3)

    def _get_updates(self) -> dict:
        return self._request("getUpdates", {
            "offset": self._offset,
            "timeout": POLL_TIMEOUT,
            "allowed_updates": ["message"],
        })

    async def _handle_update(self, update: dict) -> None:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return
        # 记住这个会话：主动推送（形态命中/开平仓）只能发到"来过信的会话"，
        # Telegram bot 无法主动给陌生人发消息。
        self.remember_chat(chat_id)
        # 文字、图片、文件都接：只读 text 会让用户发的图直接被丢掉，
        # 界面上表现就是"带媒体的发不出去"。
        text, attachments = await asyncio.to_thread(self.read_incoming, message)
        if not text and not attachments and not self._looks_like_media(message):
            return                        # 真正空的消息（贴纸、系统事件等）：忽略
        if self.allowed and str(chat_id) not in self.allowed:
            self.send(chat_id, "此会话未授权。请先在网页端「设置 → Telegram」里把本会话 ID 加入白名单：\n"
                               f"{chat_id}")
            return
        if not attachments and self._looks_like_media(message):
            # 用户确实发了媒体，但类型/体积不在支持范围：要明确告诉他，
            # 不能让消息静默消失。
            self.send(chat_id, "这个附件我没法读：只支持图片（≤2MB）和文本文件。")
            return
        try:
            # 只发媒体时 text 为空，原样传空串：Agent 那边会补一句给模型看的说明，
            # 但会话记录里保留空 content，网页端就只渲染附件气泡。
            if not self.on_message:
                reply = ""
            elif self._accepts_attachments:
                reply = await self.on_message(chat_id, text, attachments)
            else:
                reply = await self.on_message(chat_id, text)
        except Exception as exc:
            reply = f"处理失败：{type(exc).__name__}"
        if reply:
            # 走 send_reply：正文和表情分开发。直接把整段塞 sendMessage 的话，
            # `[sticker:xxx]` 会原样出现在聊天里（线上实测就是这样）。
            await asyncio.to_thread(self.send_reply, chat_id, reply)

    @property
    def _accepts_attachments(self) -> bool:
        """回调是否接受第三个附件参数。

        不能用"先传三个、抛 TypeError 再传两个"来探测：回调内部自己抛的
        TypeError 会被误判成签名不匹配，同一条消息会被处理两次。
        """
        if self.on_message is None:
            return False
        try:
            params = inspect.signature(self.on_message).parameters.values()
        except (TypeError, ValueError):
            return False
        if any(p.kind is p.VAR_POSITIONAL for p in params):
            return True
        positional = [p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        return len(positional) >= 3

    @staticmethod
    def _looks_like_media(message: dict) -> bool:
        """这条消息里是不是带了附件（只是我们没取下来）。"""
        return bool(message.get("photo") or message.get("document") or message.get("caption")
                    or message.get("voice") or message.get("video") or message.get("audio"))

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        if not self.token:
            return
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        task, self._task = self._task, None
        self._running = False
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
