#!/bin/bash
# 微信扫码登录：后台常驻，把二维码链接写到文件里等用户扫。
# 前台跑会被 60 秒超时打断，二维码随即失效。
set -u
export PATH=/opt/node26/bin:$PATH
export OPENCLAW_HOME=/opt/openclaw-home
cd /opt/openclaw || exit 1
OUT=/opt/openclaw-home/login.log
: > "$OUT"
setsid ./node_modules/.bin/openclaw channels login --channel openclaw-weixin \
  > "$OUT" 2>&1 < /dev/null &
echo "pid=$!" >> "$OUT"
