"""形态命中通知：只保留控制台 / CSV / Webhook，桌面弹窗已移除。

原来还有一条 Windows toast 通道（winotify），线上是 Linux 弹不出来、
本地信号太密也一直关着，用户确认「没啥意义」后删除。这里锁住三件事：
  1. 老配置里残留的 toast/sound 键会被忽略，不会让 Notifier 崩；
  2. webhook 仍然是唯一的外部推送通道，且真能发出请求；
  3. 源码里不再引用 winotify（防止有人从历史里把这行加回来）。
"""
import json
import pathlib

import pytest

import notify.notifier as notifier_mod
from notify.notifier import Notifier, fmt_hit
from patterns.base import PatternHit


def _hit(**kw):
    base = dict(symbol="btcusdt", interval="1m", pattern="hammer", name_zh="锤子线",
                direction="bullish", mode="confirmed", price=77215.1,
                ts=1_800_000_000_000, bar_open_time=1_799_999_940_000)
    base.update(kw)
    return PatternHit(**base)


def test_legacy_toast_keys_are_ignored(tmp_path):
    """老 config 里还留着 toast/sound 时，不能报错也不会去弹窗。"""
    cfg = {"toast": True, "sound": True, "webhook": {"enabled": False}}
    n = Notifier(cfg, None)
    assert not hasattr(n, "toast"), "toast 配置项与属性都应已移除"
    assert not hasattr(n, "_toast"), "_toast 方法应已删除"
    n(_hit())          # 不抛异常即可


def test_webhook_is_still_the_external_channel(tmp_path, monkeypatch):
    """删除 toast 不能顺手把 webhook 也弄丢：它必须是唯一剩余的外部推送。"""
    sent = []

    class FakeResponse:
        def __init__(self, body):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

    def fake_urlopen(req, timeout=None):
        sent.append(json.loads(req.data.decode()))
        return FakeResponse(b"{}")

    monkeypatch.setattr(notifier_mod.urllib.request, "urlopen", fake_urlopen)
    cfg = {"webhook": {"enabled": True, "type": "dingtalk",
                       "url": "https://example.invalid/hook"}}
    n = Notifier(cfg, None)
    n(_hit())
    n._pool.shutdown(wait=True)
    assert sent and sent[0]["msgtype"] == "text"
    assert "锤子线" in sent[0]["text"]["content"]


def test_csv_row_still_written(tmp_path):
    path = tmp_path / "signals.csv"
    n = Notifier({}, str(path))
    n(_hit())
    n._pool.shutdown(wait=True)
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert lines[0].startswith("ts,symbol,interval")
    assert "btcusdt" in lines[1]


def test_no_winotify_reference_in_source():
    """弹窗通道已删除：源码里不该再有 winotify 依赖。"""
    root = pathlib.Path(notifier_mod.__file__).resolve().parent.parent
    offenders = [p.relative_to(root).as_posix()
                 for p in root.rglob("*.py")
                 if "__pycache__" not in p.parts
                 and p.name != pathlib.Path(__file__).name      # 本文件要把名字当检查词
                 and "winotify" in p.read_text(encoding="utf-8")]
    assert not offenders, f"这些文件还在引用 winotify: {offenders}"


def test_requirements_has_no_winotify():
    req = pathlib.Path(notifier_mod.__file__).resolve().parent.parent / "requirements.txt"
    assert "winotify" not in req.read_text(encoding="utf-8")


def test_fmt_hit_shape_unchanged():
    """通知文案格式不受本次删除影响（控制台/CSV/webhook 都靠它）。"""
    title, body = fmt_hit(_hit(), 120)
    assert title == "[BTCUSDT 1m] 锤子线 ↑ 看涨"
    assert "收线确认 @ 77215.1" in body and "延迟120ms" in body
