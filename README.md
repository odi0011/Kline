# K线形态实时识别器

币安 USDT-M 合约（BTC/ETH/BNB/SOL 等）逐笔行情 → 实时形态识别 → 网页宫格展示 + 信号推送。

**线上地址：http://<你的服务器IP>:8000**（东京服务器，直连币安撮合引擎，实测推送延迟 ~6ms）

## 存储（MySQL kline 库）

- `candles` 表：K线滚动保留最近 30 分钟（`storage.minutes` 可调），供悬停查询
- `hits` 表：形态命中完整记录，保留 7 天；悬停某根K线可查当时记录（含胜负）
- **胜率判定**：确认信号出现后 `storage.eval_minutes`（默认 10 分钟）的收盘价，如形态方向所预测即为 win；前端卡片上的"胜xx%"徽标由此统计

## 自动部署（已配置）

- GitHub main 分支每次更新后，服务器上的 `kline-autodeploy.timer`（每 2 分钟检查）自动完成：拉取代码 → 构建前端（独立 node20，位于 /opt/kline-node，不影响系统 node）→ 重启服务，全流程 1~2 分钟。
- 服务器侧永不覆盖：`config.yaml`（密钥）、`kline.db`、`venv/`。
- 手动触发：`systemctl start kline-autodeploy.service`；日志：`journalctl -u kline-autodeploy -f`。
- 前端开发改完代码：本地构建验证后直接 push，线上 2 分钟内自动更新。

## 设计约定

- 页面里所有出现横向/纵向滚动的容器，一律用 uiarc 的 `ScrollArea`（`@/components/arc/scroll-area/scroll-area`）包裹；需要"完全隐藏滚动条"的容器（如左侧币种列表）加 `.left-scroll` 类（内部 CSS 隐藏 `.track` 覆盖层）。

## 线上部署（东京服务器）

- 目录 `/opt/kline-monitor`，独立 venv，systemd 服务 `kline-monitor`，与机器上其他项目完全隔离。
- 常用命令：`systemctl status/restart kline-monitor`、日志 `journalctl -u kline-monitor -f`。
- 防火墙已放行 8000（网页）/ 8765（行情WS）。注意：页面为只读行情、无鉴权，对公网开放。

## 架构与延迟

```
币安 fstream.binance.com/market/stream (aggTrade 逐笔推送, 免key, 非REST无限频)
  → feed/source.py      WS长连接，自动重连，记录推送延迟
  → core/aggregator.py  事件驱动聚合：每笔成交即更新K线，桶切换瞬间判定收线（不等定时器）
  → core/engine.py      形态引擎：盘中预警(live) + 收线确认(confirmed) 两种模式，同根同形态去重
  → server/hub.py       WS广播给浏览器 + Windows toast/钉钉/飞书(可选) + signals.csv
```

- 识别本身（纯 Python，60 根窗口 × 19 形态）单次 <1ms，延迟瓶颈只在网络。
- `aggTrade` 是逐笔实时推送；本地聚合计算的"收线"发生在下一桶第一笔成交到达的瞬间，比等 K 线流推送更快。
- **重要**：币安 2026-03 起 WS 分路由（`/public` `/market` `/private`），aggTrade 属 `/market`——**不带路由前缀的旧连接不会推送该流**，症状是"握手成功但永远没数据"（2026-10 排查实录）。因此 `feed/source.py` 使用 `wss://fstream.binance.com/market/stream`。
- 东京机房（撮合引擎所在地）延迟 1~10ms；本地直连约 50~150ms。

## 本地运行（可选）

```bash
pip install -r requirements.txt
python main.py            # http://127.0.0.1:8000
```

前端改代码后重新构建：`cd web && npm install && npm run build`，然后重新部署到服务器（upload web/dist）。开发模式 `npm run dev`。

测试：`python -m pytest tests/ -q`

## 配置（config.yaml）

| 项 | 说明 |
|---|---|
| `symbols` / `intervals` | 监控合约与周期（1s~1d 任意组合） |
| `feed.mode` | `binance`=只连真数据 / `auto`=连不上自动降级演示数据 / `demo` |
| `feed.proxy` | 代理（websockets≥14 支持 socks5/http） |
| `patterns.enabled` | `all` 或 key 列表 |
| `patterns.live_preview` | 盘中预警开关；收线后仍满足会再发一次"收线确认" |
| `notify.toast` | Windows 桌面弹窗（信号较频繁，默认关闭，网页信号面板不受影响） |
| `web_port` / `ws_port` | 网页 / 行情 WS 端口 |

## 内置形态（19 种，patterns/builtin.py）

- 单根：锤子线、上吊线、倒锤子、流星线、十字星、蜻蜓十字、墓碑十字
- 双根：看涨/看跌吞没、刺透线、乌云盖顶、看涨/看跌孕线、平头底/顶
- 三根：启明星、黄昏星、三白兵、三只乌鸦

### 新增你自己的形态

在 `patterns/builtin.py`（或新建文件并在 `main.py` 导入）里：

```python
from .base import Pattern, register, body, is_bull, is_bear, rng, trend_of

@register
class MyPattern(Pattern):
    key, name_zh, direction = "my_pattern", "我的形态", "bullish"
    min_bars = 2                      # 形态占几根K线
    def check(self, cs):
        p, c = cs[-2], cs[-1]         # cs[-1] 是形态最后一根
        return is_bear(p) and is_bull(c) and c.close > p.open
```

`base.py` 提供 `body / upper_wick / lower_wick / rng / is_bull / is_bear / body_top / body_bottom / trend_of` 原语。趋势类形态在历史不足时会静默（返回 flat），启动回补 300 根后即正常。

## 排查

- 页面右上角出现"演示"小角标 = 数据源降级成了模拟数据（服务器配置为 `mode: binance` 不会出现）。
- 图表空白但价格在跳 = 标签页在后台被浏览器冻结渲染，切到前台即恢复。
- 启动后看 `[latency]` 日志行：p50 即逐笔成交从交易所到程序的延迟。
- 通知时间戳准确性：Windows 执行 `w32tm /resync` 对时。
