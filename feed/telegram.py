"""Telegram Bot 接入：把对话接到 ODDD Agent。

关键设计：
- 长轮询（getUpdates），不需要公网回调地址；
- 与网页端共用同一份 AgentManager 会话，消息双向可见；
- 只接受白名单内的 chat id，未授权的会话一律拒绝并提示；
- token 存在 config.yaml 的 telegram 段，可在设置页修改后热重启轮询。
"""
from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT = 35          # 长轮询本身的超时（秒）
POLL_TIMEOUT = 25     # getUpdates 在服务端挂起的时长
MAX_TEXT = 4000       # Telegram 单条消息上限是 4096，留余量


class TelegramBot:
    def __init__(self, token: str = "", allowed: list[str] | None = None,
                 on_message=None, proxy: str | None = None):
        self.token = (token or "").strip()
        self.allowed = {str(x).strip() for x in (allowed or []) if str(x).strip()}
        self.on_message = on_message          # async (chat_id, text) -> str
        self.proxy = proxy
        self._offset = 0
        self._task: asyncio.Task | None = None
        self._running = False
        self.last_error = ""
        self.bot_info: dict = {}

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
        text = str(message.get("text") or "").strip()
        if chat_id is None or not text:
            return
        if self.allowed and str(chat_id) not in self.allowed:
            self.send(chat_id, "此会话未授权。请先在网页端「设置 → Telegram」里把本会话 ID 加入白名单：\n"
                               f"{chat_id}")
            return
        try:
            reply = await self.on_message(chat_id, text) if self.on_message else ""
        except Exception as exc:
            reply = f"处理失败：{type(exc).__name__}"
        if reply:
            await asyncio.to_thread(self.send, chat_id, reply)

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
