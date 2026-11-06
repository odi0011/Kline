"""OpenClaw 侧的出站推送：主动把消息（和表情包）发到微信。

## 为什么不能只靠"回复"

OpenClaw 只在**收到微信消息**时才会来问 ODDD（我们的 /v1 接口）。
系统事件（形态命中、开平仓、数据异常）是我们自己发起的，没有外部请求
可以回——必须由我们主动调 OpenClaw 的发送接口。

## 用 CLI 而不是直接打 ilinkai 接口

`openclaw message send` 是官方入口，它负责账号上下文、contextToken、
消息去重和重试。自己拼 weixin 的 HTTP 请求要复刻这一整套状态，
而且协议没有文档——升级就废。

代价是每次推送要起一个 node 进程（约 1~2 秒）。事件驱动、几小时才有
一条，这个成本可以接受；高频场景才值得去接网关的 WS 协议。

## 表情包

`--media` 支持本地路径，所以 `[sticker:崩溃]` 会以**真正的图片消息**
发出去，不是一串链接——这正是微信里该有的样子。
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time

# OpenClaw 日志里记录过对方的 wechat id：`to=o9cq...@im.wechat`
# 主动推送必须指定 target，所以从这里学；没有别的可靠来源。
TARGET_RE = re.compile(r"to=([A-Za-z0-9_\-]+@im\.wechat)")
LOG_GLOB_DIR = "/tmp/openclaw"


class OpenClawPush:
    def __init__(self, openclaw_bin: str, openclaw_home: str,
                 node_bin_dir: str, channel: str = "openclaw-weixin",
                 log_dir: str = LOG_GLOB_DIR, timeout: float = 45.0):
        self.openclaw_bin = openclaw_bin
        self.openclaw_home = openclaw_home
        self.node_bin_dir = node_bin_dir
        self.channel = channel
        self.log_dir = log_dir
        self.timeout = timeout
        self._lock = threading.Lock()
        self._targets: list[str] = []
        self._targets_at = 0.0
        self._sent = 0
        self._errors = 0
        self._last_error = ""
        self._last_ok_at = 0

    def describe(self) -> dict:
        return {
            "available": bool(self.openclaw_bin and os.path.exists(self.openclaw_bin)),
            "targets": list(self.targets()),
            "sent": self._sent,
            "errors": self._errors,
            "lastError": self._last_error,
            "lastOkAt": self._last_ok_at,
        }

    # ---------- 收件人 ----------

    def targets(self) -> list[str]:
        """最近和这个 bot 聊过的微信会话 id。缓存 60 秒，避免频繁扫日志。"""
        now = time.time()
        with self._lock:
            if self._targets and now - self._targets_at < 60:
                return list(self._targets)
        found: list[str] = []
        try:
            for name in sorted(os.listdir(self.log_dir), reverse=True)[:3]:
                path = os.path.join(self.log_dir, name)
                if not os.path.isfile(path):
                    continue
                try:
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        # 只看尾部：日志可能几十 MB，整读既慢又费内存
                        fh.seek(0, os.SEEK_END)
                        size = fh.tell()
                        fh.seek(max(0, size - 2_000_000))
                        text = fh.read()
                except OSError:
                    continue
                for match in TARGET_RE.finditer(text):
                    tid = match.group(1)
                    if tid not in found:
                        found.append(tid)
        except OSError:
            found = []
        with self._lock:
            if found:
                self._targets = found[:5]
                self._targets_at = now
            return list(self._targets)

    # ---------- 发送 ----------

    def _run(self, args: list[str]) -> tuple[int, str]:
        env = dict(os.environ)
        env["PATH"] = self.node_bin_dir + os.pathsep + env.get("PATH", "")
        env["OPENCLAW_HOME"] = self.openclaw_home
        try:
            proc = subprocess.run(
                [self.openclaw_bin, *args], capture_output=True, text=True,
                timeout=self.timeout, env=env,
                cwd=os.path.dirname(os.path.dirname(self.openclaw_bin)))
            return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired:
            return 124, "发送超时"
        except Exception as exc:
            return 1, f"{type(exc).__name__}: {exc}"

    def send(self, text: str, media_path: str = "", dry_run: bool = False) -> dict:
        """发一条消息到微信。media_path 非空时作为图片附件一起发。"""
        targets = self.targets()
        if not targets:
            return {"ok": False,
                    "error": "还没有微信会话记录；先在微信里给 bot 发一句话"}
        body = str(text or "").strip()
        if not body and not media_path:
            return {"ok": False, "error": "没有内容可发"}
        results = []
        for target in targets[:3]:
            args = ["message", "send", "--channel", self.channel,
                    "--target", target]
            if body:
                args += ["--message", body]
            if media_path:
                args += ["--media", media_path]
            if dry_run:
                args.append("--dry-run")
            code, out = self._run(args)
            ok = code == 0 and "error" not in out.lower()[:400]
            results.append({"target": target, "ok": ok, "code": code,
                            "out": out[-300:]})
            if ok:
                self._sent += 1
                self._last_ok_at = int(time.time())
            else:
                self._errors += 1
                self._last_error = out.strip()[-200:]
        return {"ok": any(r["ok"] for r in results), "results": results}

    def send_sticker(self, text: str, ref, public_dir: str = "",
                     dist_dir: str = "") -> dict:
        """带表情包推送：把图片作为媒体附件发，正文去掉标记。

        微信收图片用 --media；`[sticker:xxx]` 标记留在正文里只会显示成
        一串没人看得懂的代码。
        """
        from ai.stickers_media import locate
        path = ""
        if ref is not None:
            if getattr(ref, "local_path", "") and os.path.isfile(ref.local_path):
                path = ref.local_path
            elif getattr(ref, "inner", ""):
                path = locate(ref.inner, public_dir, "", dist_dir)
        return self.send(text, media_path=path)
