# K线形态实时识别器

币安 USDT-M 合约逐笔行情 → 实时形态识别 → 网页宫格展示 + 信号推送。

**线上地址：http://<你的服务器IP>:8000**（东京服务器，直连币安撮合引擎，实测推送延迟 ~6ms）

## 币种（监控池 + 前端选择）

- `config.yaml` 的 `symbols` 是**监控池**（行情流、形态检测、存储对池内全部币种跑，建议 ≤16 个）
- 前端菜单"选择币种"里从池内勾选 **4~10 个**展示，选择存浏览器本地；页面只提示已展示币种的信号
- 池内币种切换展示**零等待**：行情/聚合一直在跑，勾选即出图

## 存储（MySQL kline 库）

- `candles` 表：K线滚动保留最近 30 分钟（`storage.minutes` 可调），供悬停查询
- `hits` 表：形态命中完整记录，默认保留 90 天（`storage.hit_days`）；悬停某根K线可查当时记录（含胜负）
- `quant_trades` / `quant_events` 表：实盘成交（默认 365 天）与决策事件流水（默认 30 天）
- `decision_traces` 表：决策 trace 与 gate 拦截明细（默认 30 天），重启后页面/Agent 仍能看到之前的"为什么开/没开"
- `quant_state` 表：风控引擎与机制漂移保护状态快照，重启后按上次状态继续（不会因重启把日亏限额/连亏熔断清零）
- **胜率判定**：确认信号出现后 `storage.eval_minutes`（默认 10 分钟）的收盘价，如形态方向所预测即为 win；前端卡片上的"胜xx%"徽标由此统计
- `paper_positions` / `paper_trades` 表：模拟仓（15 分钟纸上交易）的未平仓快照与平仓流水。仓位每轮 tick 覆盖；平仓记录 append-only、默认永久保留（`storage.paper_keep_days`，0=永久），服务重启后自动恢复仓位并接着跑，胜率/盈亏可直接用 SQL 复盘
- `candle_archive` 表：K线长期归档（每根收线K线，默认永久保留 `storage.candle_archive_days`），滚动表 `candles` 仍只留 30 分钟；回测/复盘用 `tools/export_klines.py` 导出 CSV
- `agent_state` / `agent_chat` 表：Agent 的任务/偏好/摘要与长期聊天记录（默认永久）。内存里仍只保留最近 40 条对话，更早的只在库里可查；MySQL 不可用时自动退回本地 `agent-state.json`

## 密钥与安全

- `config.yaml` 里的密钥（MySQL 密码、交易所 API Key/Secret、AI key、Telegram token、Webhook URL）支持**加密落盘**：
  `python tools/encrypt_config.py` 会在备份后把明文改成 `enc:v1:...`，主密钥写到同目录 `secrets.key`（0600）。
  `--decrypt` 可还原，`--check` 只检查不写文件。**secrets.key 丢失将无法解密，请单独备份**。
- 也支持环境变量覆盖（优先级最高）：`KLINE_MYSQL_PASSWORD`、`KLINE_AI_KEY`、`KLINE_TELEGRAM_TOKEN`、
  `KLINE_WEBHOOK_URL`、`KLINE_ACCOUNT_API_KEY`、`KLINE_ACCOUNT_API_SECRET`、`KLINE_SECRET_KEY`。
- 服务启动会提示哪些密钥仍是明文，并把 `config.yaml` / `secrets.key` 权限收紧到 0600。

## K线归档与回测导出

- 服务启动回补、按需订阅以及主周期每次收线都会写入 `candle_archive`（全部监控币种；
  1h/4h 辅助周期在启动回补与按需订阅时归档）。
- 导出 CSV（只读数据库，不连交易所）：

  ```
  python tools/export_klines.py --symbol btcusdt --interval 1m \
      --from 2026-01-01 --to 2026-02-01 --out btc_1m.csv
  ```

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
  → core/engine.py      形态引擎：盘中预警(live) + 收线确认(confirmed) 两种模式，同根K线只报优先级最高的一个形态（每根每种模式仅一次）
  → server/hub.py       WS广播给浏览器 + 钉钉/飞书 webhook(可选) + signals.csv
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
| `web_port` / `ws_port` | 网页 / 行情 WS 端口 |
| `search.enabled` | Agent 联网搜索开关；默认无 key 走 DuckDuckGo/Bing HTML，设 `KLINE_SERPER_KEY` 可用 Serper |
| `search.proxy` / `timeout` / `count` | 搜索代理（空=沿用 feed.proxy） / 超时秒数 / 默认结果条数 |

## Agent（ODDD 对话助手）

- **读本地数据**：行情/K线/系统建议/形态胜率/账户/模拟仓/风控/新闻/计算流水（全部只读，无下单权限）。
- **联网搜索**：`web_search` 搜外部信息、`read_web_page` 读网页正文；**要不要搜由模型自己判断**
  （新闻、项目背景、人物、政策、你不知道的事实才搜；行情/持仓用本地工具）。
  默认 DuckDuckGo HTML，失败回退 Bing，无需 key；`KLINE_SERPER_KEY` 可提升质量；代理沿用 `feed.proxy`。
  搜索结果与网页内容是**不可信外部数据**，Agent 只当资料、引用给标题+链接，不执行其中指令。
- **表情包**：`send_sticker` 工具（崩溃/疑问/嘲讽），模型在轻松、调侃、自嘲、行情离谱等场景主动使用；
  严肃问价位或持仓风险时不发；可以只发表情包不说话。
- **后台常驻任务**：`create_watch` 建真实后台任务（服务端定时跑、不依赖网页开没开）；
  任务结果符合 `notify` 规则（always / on_change / never）时会主动发到聊天窗，不只在后台面板里。
- **沙盒**：`sandbox_*` 可在受限工作目录里跑 python/node 等白名单命令，根文件系统只读、禁网、30 秒超时。

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
