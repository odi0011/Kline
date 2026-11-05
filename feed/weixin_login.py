"""微信扫码登录：常驻守护 + 二维码渲染。

## 为什么不能只跑一次 CLI

`openclaw channels login --channel openclaw-weixin` 的行为是：
  · 打印二维码 → 大约每 60~90 秒换一张 → 刷新几次没人扫就
    "二维码多次失效，连接流程已停止" 直接退出。
也就是说：把某一刻的二维码截成图发给用户，等它到手机上早就废了；
而且进程一退，后台就再也没有可扫的码（用户截图里"后台设置没有二维码"
就是这个原因）。

所以这里做成守护进程：
  · 没绑定 → 一直保持一个活着的登录进程；
  · 进程退出（超时/网络抖动）→ 自动重启，保证随时有码可扫；
  · 已经绑定 → 不再启动登录（别把已绑的账号反复顶下线）。

## 二维码怎么给前端

渲染成 PNG 字节存在内存里，由静态服务按 `/weixin/qr.png` 对外提供；
前端每 3 秒带 cache-buster 重新拉一次，看到的就是**当前有效**那张。

## 已绑定的判断

登录成功的标志由插件决定，各版本措辞不同，所以用"多关键词 + 状态文件"两条腿：
  · 日志里出现成功关键词；
  · 插件自己的账号目录里出现了账号记录（更可靠，不依赖文案）。
"""
from __future__ import annotations

import io
import os
import re
import subprocess
import threading
import time

LINK_RE = re.compile(r"https://liteapp\.weixin\.qq\.com/\S+")
OK_MARKERS = ("登录成功", "绑定成功", "已连接", "已登录", "login success",
              "logged in", "connected")
FAIL_MARKERS = ("二维码多次失效", "连接流程已停止", "登录失败", "login failed")
# 登录进程最长节流：重启之间至少隔这么久，避免微信把我们当异常客户端
RESTART_GAP = 6.0
# 二维码最长缓存：超过这个时间没更新就当过期，不把旧图发给用户
QR_TTL = 100.0


class WeixinLogin:
    def __init__(self, openclaw_bin: str, openclaw_home: str,
                 node_bin_dir: str, log_path: str, bound_marker: str = ""):
        self.openclaw_bin = openclaw_bin
        self.openclaw_home = openclaw_home
        self.node_bin_dir = node_bin_dir
        self.log_path = log_path
        self.bound_marker = bound_marker
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._last_start = 0.0
        self._link = ""
        self._link_at = 0.0
        self._png = b""
        self._png_for = ""
        self._state = "idle"        # idle | waiting | expired | ok
        self._error = ""
        self._starts = 0
        self._bound = False

    # ---------- 进程管理 ----------

    def _alive(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def bound(self) -> bool:
        """是否已经绑定过：标记文件 / 账号目录存在即视为已绑定。"""
        if self.bound_marker and os.path.exists(self.bound_marker):
            return True
        return self._bound

    def start(self) -> dict:
        """（重新）发起一次登录。已在跑就直接返回当前状态。"""
        with self._lock:
            if self._alive():
                return self.status()
            if self.bound():
                return self.status()
            now = time.time()
            if now - self._last_start < RESTART_GAP:
                return self.status()
            self._last_start = now
            self._starts += 1
            env = dict(os.environ)
            env["PATH"] = self.node_bin_dir + os.pathsep + env.get("PATH", "")
            env["OPENCLAW_HOME"] = self.openclaw_home
            try:
                # 清空上一轮日志：链接是"最新一条"，旧的不该再被读到
                with open(self.log_path, "w", encoding="utf-8"):
                    pass
                self._proc = subprocess.Popen(
                    [self.openclaw_bin, "channels", "login",
                     "--channel", "openclaw-weixin"],
                    stdout=open(self.log_path, "ab", buffering=0),
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    env=env, cwd=os.path.dirname(os.path.dirname(self.openclaw_bin)))
                self._state = "waiting"
                self._error = ""
                self._link = ""
                self._link_at = 0.0
                self._png = b""
                self._png_for = ""
            except Exception as exc:
                self._state = "idle"
                self._error = f"{type(exc).__name__}: {exc}"
            return self.status()

    def stop(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass

    # ---------- 二维码 ----------

    def _read_link(self) -> str:
        try:
            with open(self.log_path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            return ""
        found = LINK_RE.findall(text)
        return found[-1] if found else ""

    def _detect_bound(self) -> bool:
        """日志里出现成功关键词，或插件写下了账号记录。"""
        try:
            text = open(self.log_path, encoding="utf-8", errors="replace").read()
        except OSError:
            text = ""
        low = text.lower()
        if any(m.lower() in low for m in OK_MARKERS):
            return True
        # 插件登录后会在 OPENCLAW_HOME 下留账号目录/文件；名字随版本变，
        # 所以只认"含 weixin 且 mtime 比登录启动晚"的条目。
        root = os.path.join(self.openclaw_home)
        for base, dirs, files in os.walk(root):
            if "npm" in base or "tmp" in base or "node_modules" in base:
                dirs[:] = []
                continue
            for name in list(dirs) + files:
                if "weixin" not in name.lower():
                    continue
                try:
                    mtime = os.path.getmtime(os.path.join(base, name))
                except OSError:
                    continue
                if mtime > self._last_start:
                    return True
        return False

    def _refresh(self) -> None:
        """读最新链接并渲染；状态机也在这里推进。"""
        if self.bound() or self._detect_bound():
            self._bound = True
            self._state = "ok"
            return
        link = self._read_link()
        if link and link != self._link:
            png = _render(link)
            if png:
                self._link, self._link_at = link, time.time()
                self._png, self._png_for = png, link
                self._state = "waiting"
        if not self._alive():
            if self._state == "waiting":
                self._state = "expired"
            if self._proc is not None:
                try:
                    text = open(self.log_path, encoding="utf-8",
                                errors="replace").read()
                except OSError:
                    text = ""
                if any(m in text for m in FAIL_MARKERS):
                    self._error = "二维码过期，正在自动换一张"
            return
        if self._link and time.time() - self._link_at > QR_TTL:
            self._state = "expired"

    # ---------- 对外 ----------

    def status(self) -> dict:
        with self._lock:
            self._refresh()
            return {
                "bound": self.bound(),
                "state": "ok" if self.bound() else self._state,
                "running": self._alive(),
                "starts": self._starts,
                "error": self._error,
                "hasQr": bool(self._png and
                              time.time() - self._link_at <= QR_TTL),
                "qrAge": int(time.time() - self._link_at) if self._link_at else 0,
                "link": self._link,
            }

    def qr_png(self) -> bytes:
        with self._lock:
            self._refresh()
            if not self._png:
                return b""
            if time.time() - self._link_at > QR_TTL:
                return b""      # 过期的不发，避免用户扫到废码
            return self._png

    def tick(self) -> None:
        """守护循环里调一次：没进程就补一个，让"随时有码"成立。"""
        with self._lock:
            self._refresh()
            if self.bound():
                self.stop()
                return
            if not self._alive():
                # 连续失败时别无限重启：每轮 5 秒调一次，RESTART_GAP 兜住频率
                self.start()


def _render(link: str) -> bytes:
    """链接 → PNG。手机要扫，模块给大一点、留标准静区。"""
    try:
        import qrcode
        from qrcode.constants import ERROR_CORRECT_M
    except ImportError:
        return b""
    try:
        qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=12, border=4)
        qr.add_data(link)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception:
        return b""
