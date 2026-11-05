"""微信扫码登录守护：二维码要一直是「当前有效」的那张。

线上问题：把某一刻的二维码截成图发给用户，等它到手机上已经过期；
而且登录进程刷新几轮没人扫就退出，后台从此没有可扫的码。
"""
import os
import time

import pytest

from feed.weixin_login import QR_TTL, WeixinLogin, _render


def _login(tmp_path, log_text="", alive=True):
    log = tmp_path / "login.log"
    log.write_text(log_text, encoding="utf-8")
    obj = WeixinLogin(
        openclaw_bin=str(tmp_path / "fake-openclaw"),
        openclaw_home=str(tmp_path / "home"),
        node_bin_dir="/usr/bin",
        log_path=str(log),
    )
    # 不真的起进程：把存活判断换成测试可控的
    obj._proc = None
    obj._alive = lambda: alive
    (tmp_path / "home").mkdir(exist_ok=True)
    return obj, log


LINK = "https://liteapp.weixin.qq.com/q/7GiQu1?qrcode=abc123&bot_type=3"


def test_render_produces_real_png():
    png = _render(LINK)
    assert png.startswith(b"\x89PNG")
    assert len(png) > 500


def test_qr_appears_once_link_is_logged(tmp_path):
    obj, log = _login(tmp_path)
    log.write_text("用手机微信扫描以下二维码\n" + LINK + "\n正在等待操作...\n",
                   encoding="utf-8")
    st = obj.status()
    assert st["hasQr"] is True
    assert st["state"] == "waiting"
    assert obj.qr_png().startswith(b"\x89PNG")


def test_latest_link_wins_when_qr_refreshes(tmp_path):
    """微信会换码：必须取最后一条，否则给用户的是上一张废码。"""
    obj, log = _login(tmp_path)
    new = LINK.replace("abc123", "def456")
    log.write_text(LINK + "\n...\n" + new + "\n", encoding="utf-8")
    obj.status()
    assert obj._link == new


def test_qr_still_served_while_process_alive(tmp_path):
    """进程活着就一直给这张码。

    线上教训：微信拿到码后可能 100 秒内都不刷新，TTL 设太短会让
    /weixin/qr.png 返回空图，用户看到的是"没有二维码"。
    """
    obj, log = _login(tmp_path)
    log.write_text(LINK, encoding="utf-8")
    obj.status()
    assert obj.qr_png()
    obj._link_at = time.time() - 120      # 两分钟没刷新
    assert obj.qr_png()[:1] == bytes([0x89])


def test_expired_qr_not_served_after_process_died(tmp_path):
    """进程死了就别再发旧码——那时码肯定废了，等守护换新的。"""
    obj, log = _login(tmp_path)
    log.write_text(LINK, encoding="utf-8")
    obj.status()
    assert obj.qr_png()
    obj._alive = lambda: False
    assert obj.qr_png() == b""


def test_process_death_marks_expired(tmp_path):
    obj, log = _login(tmp_path, alive=False)
    log.write_text(LINK, encoding="utf-8")
    st = obj.status()
    assert st["state"] == "expired"
    assert st["running"] is False


def test_success_marker_marks_bound(tmp_path):
    obj, log = _login(tmp_path)
    log.write_text(LINK + "\n登录成功\n", encoding="utf-8")
    st = obj.status()
    assert st["bound"] is True and st["state"] == "ok"


def test_bound_marker_file(tmp_path):
    obj, _ = _login(tmp_path)
    marker = tmp_path / "bound"
    marker.write_text("1")
    obj.bound_marker = str(marker)
    assert obj.bound() is True


def test_tick_does_not_restart_when_bound(tmp_path):
    """已绑定就不要反复起登录：会把已连上的账号顶下线。"""
    obj, _ = _login(tmp_path, alive=False)
    obj._bound = True
    called = []
    obj.start = lambda: called.append(1) or {}
    obj.tick()
    assert called == []


def test_tick_restarts_when_process_died(tmp_path):
    """进程退出了要自动换新码，否则后台就没有可扫的了。"""
    obj, _ = _login(tmp_path, alive=False)
    called = []
    obj.start = lambda: called.append(1) or {}
    obj.tick()
    assert called == [1]
