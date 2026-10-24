from __future__ import annotations

import asyncio
import csv
import json
import os
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from patterns.base import PatternHit

DIR_ARROW = {"bullish": "↑ 看涨", "bearish": "↓ 看跌", "neutral": "◆ 中性"}
MODE_LABEL = {"live": "盘中预警", "confirmed": "收线确认"}


def fmt_hit(hit: PatternHit, lag_ms: int | None = None) -> tuple[str, str]:
    title = f"[{hit.symbol.upper()} {hit.interval}] {hit.name_zh} {DIR_ARROW[hit.direction]}"
    ts = time.strftime("%H:%M:%S", time.localtime(hit.ts / 1000))
    lag = f" | 延迟{lag_ms}ms" if lag_ms is not None else ""
    body = (f"{MODE_LABEL[hit.mode]} @ {hit.price:g}\n"
            f"{ts}{lag}")
    return title, body


class Notifier:
    def __init__(self, cfg: dict, csv_path: str | None = None):
        self.toast = bool(cfg.get("toast", True))
        self.sound = bool(cfg.get("sound", True))
        wh = cfg.get("webhook") or {}
        self.webhook_enabled = bool(wh.get("enabled"))
        self.webhook_type = wh.get("type", "dingtalk")
        self.webhook_url = wh.get("url", "")
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="notify")
        self._csv_path = csv_path
        if csv_path:
            new = not os.path.exists(csv_path)
            with open(csv_path, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["ts", "symbol", "interval", "pattern", "name_zh",
                                "direction", "mode", "price", "bar_open_time", "lag_ms"])

    def __call__(self, hit: PatternHit, lag_ms: int | None = None) -> None:
        title, body = fmt_hit(hit, lag_ms)
        print(f"★ {title} | {body.replace(chr(10), ' ')}", flush=True)
        if self._csv_path:
            self._append_csv(hit, lag_ms)
        if self.toast:
            self._pool.submit(self._toast, title, body)
        if self.webhook_enabled and self.webhook_url:
            self._pool.submit(self._webhook, title, body)

    def _append_csv(self, hit: PatternHit, lag_ms: int | None) -> None:
        try:
            with open(self._csv_path, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow([
                    hit.ts, hit.symbol, hit.interval, hit.pattern, hit.name_zh,
                    hit.direction, hit.mode, hit.price, hit.bar_open_time, lag_ms or "",
                ])
        except OSError:
            pass

    def _toast(self, title: str, body: str) -> None:
        try:
            from winotify import Notification, audio
            n = Notification(app_id="K线形态识别", title=title, msg=body)
            if self.sound:
                n.set_audio(audio.Default, loop=False)
            else:
                n.set_audio(audio.Silent, loop=False)
            n.show()
        except Exception:
            pass

    def _webhook(self, title: str, body: str) -> None:
        text = f"{title}\n{body}"
        if self.webhook_type == "dingtalk":
            payload = {"msgtype": "text", "text": {"content": text}}
        elif self.webhook_type == "feishu":
            payload = {"msg_type": "text", "content": {"text": text}}
        else:
            payload = {"text": text}
        try:
            req = urllib.request.Request(
                self.webhook_url,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception as e:
            print(f"[notify] webhook 发送失败: {e}", flush=True)
