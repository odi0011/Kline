#!/bin/bash
# OpenClaw 网关常驻启动脚本。
#
# 为什么不直接 `openclaw gateway run`：
#  · 需要固定 PATH（Node 26 是隔离装的）和 OPENCLAW_HOME；
#  · 需要一个能被 systemd 管理的稳定前台进程，崩溃后自动拉起；
#  · 网关起来后微信频道才会真正连上。
set -u
export PATH=/opt/node26/bin:/usr/local/bin:/usr/bin:/bin
export OPENCLAW_HOME=/opt/openclaw-home
cd /opt/openclaw || exit 1
exec ./node_modules/.bin/openclaw gateway run --port 18789
