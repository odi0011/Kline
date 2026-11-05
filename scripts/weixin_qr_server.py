"""把微信扫码登录的最新二维码渲染成图片，持续更新。

## 为什么需要它

`openclaw channels login` 的二维码大约每 60~90 秒刷新一次（微信侧控制）。
把某一刻的二维码截成静态图片发给用户，等他看到时大概率已经失效——
扫码会提示"二维码已过期"。

所以这里起一个常驻循环：每隔几秒从登录日志里取**当前**的登录链接，
重新渲染成 PNG 写到静态目录。用户打开的那个地址永远是活的。

## 停止条件

登录成功 / 进程退出 / 超过最长等待时间（默认 10 分钟）→ 写入 done 标记
并退出，避免无限跑下去。
"""
from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import time

import qrcode
from qrcode.constants import ERROR_CORRECT_M

LOG = "/opt/openclaw-home/login.log"
OUT_PNG = "/opt/kline-monitor/web/dist/weixin-qr.png"
OUT_HTML = "/opt/kline-monitor/web/dist/weixin-qr.html"
INTERVAL = 5
MAX_SECONDS = 600

LINK_RE = re.compile(r"https://liteapp\.weixin\.qq\.com/\S+")
OK_MARKERS = ("登录成功", "已登录", "绑定成功", "login success", "logged in")
BAD_MARKERS = ("已过期", "二维码失效", "登录失败", "login failed")


def latest_link() -> str:
    try:
        with open(LOG, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return ""
    found = LINK_RE.findall(text)
    return found[-1] if found else ""


def login_alive() -> bool:
    try:
        out = subprocess.run(["pgrep", "-fc", "channels login"],
                             capture_output=True, text=True, timeout=5)
        return int((out.stdout or "0").strip() or 0) > 0
    except Exception:
        return False


def state() -> str:
    try:
        text = open(LOG, encoding="utf-8", errors="replace").read()
    except OSError:
        return "waiting"
    low = text.lower()
    if any(m in text or m in low for m in OK_MARKERS):
        return "ok"
    if any(m in text or m in low for m in BAD_MARKERS):
        return "failed"
    return "waiting" if login_alive() else "ended"


def render(link: str) -> bytes | None:
    """链接 → PNG 字节。用固定的高容错和大模块，手机才扫得动。"""
    if not link:
        return None
    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=12, border=4)
    qr.add_data(link)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


HTML = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="6">
<title>ODDD 微信绑定</title>
<style>
  body {{ margin:0; min-height:100vh; display:flex; flex-direction:column;
         align-items:center; justify-content:center; gap:14px;
         background:#0f1115; color:#e8ebf2;
         font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif; }}
  img {{ width:min(78vw,340px); height:auto; background:#fff;
         border-radius:14px; padding:12px; }}
  .tip {{ opacity:.72; font-size:13px; text-align:center; padding:0 24px; }}
  .bad {{ color:#f87171; }}
</style></head>
<body>
<img src="/weixin-qr.png?t={ts}" alt="微信扫码登录二维码">
<div class="tip">
  用微信扫一扫，或长按图片识别<br>
  <span style="opacity:.6">页面每 6 秒自动换最新码</span>
</div>
{extra}
</body></html>
"""


def write_outputs(link: str, st: str) -> None:
    png = render(link)
    if png:
        with open(OUT_PNG, "wb") as fh:
            fh.write(png)
    extra = ""
    if st == "ok":
        extra = '<div class="tip" style="color:#4ade80">已扫码，正在完成绑定…</div>'
    elif st == "failed":
        extra = '<div class="tip bad">二维码已失效，需要重新发起登录</div>'
    elif st == "ended":
        extra = '<div class="tip bad">登录进程已结束，请重新发起</div>'
    with open(OUT_HTML, "w", encoding="utf-8") as fh:
        fh.write(HTML.format(ts=int(time.time()), extra=extra))


def main() -> int:
    started = time.time()
    last_link = ""
    while time.time() - started < MAX_SECONDS:
        st = state()
        link = latest_link()
        if link and (link != last_link or st != "waiting"):
            write_outputs(link, st)
            last_link = link
            print(f"[{time.strftime('%H:%M:%S')}] 更新二维码 {link[-12:]} state={st}",
                  flush=True)
        if st in ("ok", "failed") or (st == "ended" and not login_alive()):
            write_outputs(link, st)
            print(f"[qr] 结束，state={st}", flush=True)
            return 0
        time.sleep(INTERVAL)
    print("[qr] 超时退出", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
