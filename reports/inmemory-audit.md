# 全仓内存/非数据库状态审计报告

> 审计时间：2026-11-03 ｜ 仓库根：`C:\Users\yd\Desktop\齐梓\齐梓\K线`
> 基线与口径：git HEAD `5fc1ff0` + **工作区未提交改动**（Lead 正在把模拟仓改 MySQL，见 `git status`）。
> 方法：逐个 Python 模块读源码，每条结论附 `file:line`；**未启动服务、未连 MySQL 做运行时核验**（只读审计）。
> 注意：`decision/paper.py`、`storage.py`、`quant.py`、`main.py` 正在被修改，模拟仓相关行号以审计时工作区为准。

## 一、结论摘要

1. **模拟仓是唯一确认"纯内存账本"的 P0 业务数据，正在修复。** 旧版 `PaperBook` 只有 `self.positions / self.history / self.stats` 三个内存字段（旧版 decision/paper.py:166-171）；当前工作区已改为 `PaperBook(store=...)`（decision/paper.py:192-206），存储层新增 `paper_positions` / `paper_trades` 表（storage.py:190-206），启动恢复与停机补结算（decision/paper.py:210-249,585+），main/quant 已接线（main.py:452-462、quant.py:152）。本报告标记为 **P0-修复中**，验收点见第六节。
2. **除模拟仓外，真正"重启归零且无法重建"的业务数据只剩决策明细**：页面上的决策 trace（signal→features→score→watch→risk→execution 分阶段记录，quant.py:188-189,236-287）与 gate 拦截的结构化记录（quant.py:143,1248,1281,1371,1478）只在内存 deque（24 条 / 40 条）；MySQL 只有摘要事件 `quant_events`（quant.py:223-225、storage.py:182）。→ **P0：建表落库**。
3. **实盘核心记录其实已经入库，问题变成"保留期太短"**：hits / equity / quant_trades / quant_events / news 都在 MySQL（storage.py:105-206），但 prune 只留 hits 7 天、quant_trades 7 天、quant_events 3 天、news 3 天、candles 30 分钟（storage.py:930-949）。**要做长期策略分析，仍必须延长保留或归档，否则"进库"等于"7 天后照样归零"（P0-风险）**。
4. **本地文件持久化分两类**：Agent 状态 `agent-state.json` 是**安全落盘**（tmp+fsync+replace+chmod，ai/agent.py:687-706），重启不丢；四个量化状态文件 `quant-positions.json` / `quant-trades.json` / `quant-unsupported.json` / `quant-manual.json` 是**普通 open("w")+json.dump，不原子、无 fsync**（quant.py:397-404,358-364,616-626），是本地持久化里最脆的一环。`config.yaml` 明文存 API Key/Secret（config.yaml:41-45）。
5. **大量"内存状态"其实是派生缓存，可留内存**：K线（交易所回补，main.py:853-867）、实盘持仓（交易所 positionRisk + 5 分钟 audit，quant.py:1942-2028）、权益（DB equity/equity_daily，storage.py:118-122）、深度/衍生品/新闻（实时拉取）。真正需要补持久化的是**风控引擎状态（decision/risk.py:119,92-101）与机制漂移保护状态（quant.py:194-195）**——它们决定"还能不能开仓"，重启后被重置会放大风险（P1）。

## 二、定级口径

| 定级 | 含义 |
| --- | --- |
| P0 | 业务数据，必须进数据库 / 必须有可靠持久化 |
| P1 | 应有持久化或恢复机制（可暂用本地文件，但要补原子写/恢复/迁移） |
| P2 | 派生数据或缓存，可留内存，丢了能重算/重取 |
| P3 | 临时运行时状态，重启丢失可接受 |

"现状"分四类：**内存**、**本地文件（JSON/CSV/log）**、**MySQL**、**交易所实时可重建**。

## 三、明细表

### 3.1 实盘交易与量化引擎（quant.py、decision/*）

| 数据 | 位置(file:line) | 现状 | 重启后果 | 是否可分析 | 定级 | 建议 |
| --- | --- | --- | --- | --- | --- | --- |
| 实盘持仓 state.positions（含开仓依据/sl/tp/peak/保护单号） | quant.py:130,1554-1582；落盘 171,397-404；恢复 406-420 | 内存 + 本地 JSON（非原子写） | JSON 正常则原样恢复；丢失/损坏时 ≤5 分钟由 audit 从交易所 positionRisk 补记（quant.py:1942-2028），但开仓依据、peak、保护单号丢失 | 部分：开仓摘要进 quant_events；本地快照无历史 | P1 | 持仓快照入 DB；JSON 改 tmp+fsync+replace；丢失时从 DB 恢复 |
| 同币冷却 state.cooldown / state.seq | quant.py:130,1501-1511,1580 | 内存（`_save_positions` 只写 positions，不含 cooldown：quant.py:400） | 冷却清零，可能立刻重开同币；风控层"上一笔出场后冷却"仍生效（decision/risk.py:284-292） | 否 | P3 | 可留内存 |
| 实盘成交 trades / quant-trades.json / quant_trades 表 | quant.py:290,358-364；storage.py:130,942 | 内存(500) + 本地 JSON + MySQL(7 天) | 内存清空；JSON 最多 500 条；DB 7 天内可查 | DB 仅 7 天，长期不足 | P1 | 实盘成交永久保留/归档；风控 seed 改读 DB |
| 风控引擎状态（日初权益/当日盈亏/连亏/暂停/下单次数/冷却） | decision/risk.py:119,92-101,136-180；quant.py:298-314 | 内存 | 仅从 trades JSON 重放（≤500 笔）：日初权益用重启时权益、order_times 清空、pause_until 丢失 → 日亏限额/连亏熔断判断变松 | 否 | P1 | 状态日快照入 DB，启动恢复 |
| 机制漂移保护 caution/shadow 期限 | quant.py:194-195,336-340,1337,1343 | 内存 | 清零 → 漂移保护失效，可立即恢复真实开仓 | 漂移事件在 DB（3 天） | P1 | cautionUntil/shadowUntil 落 DB，启动恢复 |
| **决策 trace**（分阶段决策明细） | quant.py:188-189,236-287,2201-2202 | 内存 deque(24) / dict | 重启清零，只剩 DB 摘要事件 | 结构化 stages/factors/risk/watch 不可回看 | **P0** | 建 `decision_traces` 表（payload 用 JSON 列），保留 ≥30 天 |
| **gate 拦截记录** | quant.py:143,1248,1281,1371,1478,2200 | 内存 deque(40) | 重启清零；DB quant_events 有 skip 摘要（3 天） | 摘要可查，breakdown/metrics/多周期明细不可 | P1 | 结构化落 DB（并入 trace 表或独立表） |
| 事件流 events | quant.py:131,209；本地 quant.log:169,218；DB 223-225 | 内存(60) + 本地 JSONL + MySQL(3 天) | 不丢（DB/日志可查） | DB 3 天；日志永久但无轮转 | P2 | quant.log 轮转；quant_events 延长保留 |
| 统计计数 stats（signals/decisions/orders/wins…） | quant.py:132,581,1157,1305 | 内存 | 清零 | 可从 DB 重算（当前未实现） | P2 | 启动时由 DB 聚合重建 |
| 指标/实时因子/评估/评分轨迹 | quant.py:139-142,668-725 | 内存派生 | ≤5 秒重建 | 否（评分轨迹仅内存 24 点） | P2 | 留内存 |
| 回撤熔断 dd_state | quant.py:155,836-856；数据源 main.py:567-578、storage.py:586/593 | 内存派生 | 首轮从 DB 权益重建 | 是（equity_daily 永久：storage.py:122） | P2 | 留内存 |
| 计算流水 calc_stream | quant.py:147,827,2068-2073；DB calc_trace storage.py:154,317（只留 1 分钟） | 内存(3000) + MySQL(1 分钟) | 冷启动从 DB 补最近 20 秒 | 运算轨迹非业务 | P2 | 保持（1 分钟够用） |
| 错误去重/API 冷却/审计时刻/告警去重/波动检测器 | quant.py:159,186-187,198,211-216,588-593 | 内存 | 重启后重复错误日志、限频/权限冷却丢失、audit 立即跑一次 | 否 | P3 | 可接受 |
| 下单/行情/余额等缓存 | quant.py:160,167-168,183-185,190-198 | 内存缓存 | 重建（交易所/行情） | 否 | P2 | 留内存 |
| **手动仓豁免 _manual / quant-manual.json** | quant.py:174,461-478,616-620；manual_long.py:87-94 | 本地 JSON（非原子、相对 cwd 硬编码） | 文件在→保留；丢失或路径不一致→引擎按自身策略管理手动仓，曾误平（manual_long.py:4-7 血泪注释） | 是（登记表） | P1 | 入 DB；原子写；引擎与脚本统一路径 |
| 未开通合约跳过列表 _unsupported | quant.py:161,623-626,630-638 | 本地 JSON | 保留；丢失→重复尝试失败刷日志 | 否 | P3 | 保留 JSON 即可 |

### 3.2 模拟仓（修复中）

| 数据 | 位置(file:line) | 现状 | 重启后果 | 是否可分析 | 定级 | 建议 |
| --- | --- | --- | --- | --- | --- | --- |
| 未平仓纸仓位 positions | decision/paper.py:192-249,269-277,492,583；storage.py:190,381；main.py:452-462 | 修复中：内存 + MySQL paper_positions（每次 tick UPSERT） | 正常→原样恢复；DB 不可用→降级纯内存，重启清零（summary 明示 source=memory） | 是 | **P0-修复中** | 验收重启恢复/停机补结算/降级提示 |
| 平仓流水与统计 history / stats | decision/paper.py:195-196,289-297,318-427,690-735；storage.py:194,414,491,509 | 修复中：内存缓存(200) + MySQL paper_trades（全量聚合） | 从 DB 恢复并按全量聚合，重启不清零 | 是（可直接 SQL 复盘） | **P0-修复中** | 验证 session/bySymbol 同口径 |
| 保留期 | decision/paper.py:56；config.example.yaml:69-71；storage.py:948 | 修复中：paper_keep_days=0 永久，>0 才 prune | 0=永久 | 是 | **P0-修复中** | 生产 config 确认 0 或按需调整 |

### 3.3 Agent（ai/agent.py）

| 数据 | 位置(file:line) | 现状 | 重启后果 | 是否可分析 | 定级 | 建议 |
| --- | --- | --- | --- | --- | --- | --- |
| 后台任务 tasks（含 steps/runs/nextRunAt/结果） | ai/agent.py:549,1316,863-886,687-706；main.py:764 | 本地 JSON 原子写（agent-state.json） | 不丢；running 任务重新排队（queued），周期任务重排下次执行；正在跑的那轮记 interrupted | 是，但最多 100 个任务、每个 10 次 run、24 条 steps | P1（已落盘，建议进库） | 任务/对话表入 DB，支持检索与长期保留 |
| 对话 chat / 摘要 chatSummary | ai/agent.py:550-552,619,695,845 | 本地 JSON 原子写，只留最近 40 条 + 4000 字摘要 | 不丢最近 40 条；更早被压缩/丢弃；进行中的回复重启标 failed | 仅最近 40 条+摘要 | P1 | 重要对话归档；容量上限告知用户 |
| 用户偏好 memory | ai/agent.py:551,671-672；ai/agent_tools.py:9,125 | 本地 JSON（4 个白名单键） | 不丢 | 是 | P2 | 可留 JSON |
| runners / chat_runners / wakeups / _chat_tasks 等 | ai/agent.py:553-557 | 内存 asyncio 句柄 | 重启中断；任务重新排队，回复标记 failed/cancelled | 否 | P3 | 留内存 |
| 聊天图片附件 | ai/agent.py:548,576-606；静态服务 main.py:97-117 | 本地文件 agent-attachments/（消息只存 url） | 不丢；无清理、无备份 | 否 | P2 | 清理/备份策略 |
| Agent 沙盒工作区 | ai/sandbox.py:31,71-73,237-247 | 本地文件 sandbox/（未在 .gitignore） | 不丢 | 否 | P3 | 加忽略/定期清理 |

### 3.4 行情/新闻/账户/广播（server/hub.py、main.py、feed/*）

| 数据 | 位置(file:line) | 现状 | 重启后果 | 是否可分析 | 定级 | 建议 |
| --- | --- | --- | --- | --- | --- | --- |
| hub.news 新闻列表 | server/hub.py:77；启动从 DB 恢复 main.py:1064-1070；DB storage.py:161,944 | 内存 + MySQL（正文/摘要/AI 分析，保留 3 天） | 从 DB 恢复最近 300 条 | 是（仅 3 天） | P2 | 要新闻回溯分析需延长保留 |
| hub.depths 订单簿 | server/hub.py:73；写入 main.py:884-885（feed/depth.py 实时流） | 内存（交易所 WS） | 重启后重新累积 | 否 | P2 | 留内存 |
| hub.last_accounts 账户卡 | server/hub.py:56；main.py:1010（30 秒轮询 accounts.py:98） | 内存 + 交易所实时 | 30 秒内重取 | 权益历史已入 DB，长期可分析 | P2 | 留内存 |
| hub.structures 结构分析 | server/hub.py:78,89-94；main.py:864,1050 | 内存派生（K线） | 重启回收线/回补后重算 | 否 | P2 | 留内存；要结构历史另建表 |
| hub.derivatives 衍生品指标 | server/hub.py:79-81；main.py:1240-1289 | 内存（交易所） | 60 秒内重取 | 否 | P2 | 留内存 |
| hub.market 全市场币种 | server/hub.py:42；main.py:1224-1236 | 内存（交易所） | 1 小时内重取 | 否 | P2 | 留内存 |
| hit_queue / dirty / latest / clients / lag_p50 | server/hub.py:44-49 | 内存临时 | 丢（无影响）；lag_p50 见第八节 | 否 | P3 | 留内存 |
| feed.source lags / last_trade_ms | feed/source.py:35,38,47-56 | 内存统计 | 丢（仅延迟统计/健康判断） | 否 | P2 | 留内存 |
| feed.liquidation events | feed/liquidation.py:27,90 | 内存 deque(200) | 级联统计清零，短时少一个否决因子 | 否 | P2 | 留内存 |
| feed.market_data._series_cache | feed/market_data.py:192,199,215 | 模块级内存缓存 | 重取 | 否 | P2 | 留内存 |
| Telegram 更新 offset | feed/telegram.py:41,184,194 | 内存 | 重启 offset=0 → 可能重复处理最近更新（重复回复；建任务有幂等，重复回复无提示） | 否 | P2 | offset 落盘/DB |
| DemoSource 价格 | feed/demo.py:17 | 内存 | 重新随机初始化 | 否 | P3 | 留内存 |
| PatternEngine 去重表 | core/engine.py:33-41 | 内存 | 重启可能重复通知同一根K线（概率低） | 否 | P3 | 留内存 |
| main 侧指标/杠杆缓存、池审计结果 | main.py:504,524,548,1215 | 内存缓存 | 重取/下次审计覆盖 | 否 | P2 | 留内存 |

### 3.5 配置与本地文件（config.yaml、csv、log、浏览器）

| 数据 | 位置(file:line) | 现状 | 重启后果 | 是否可分析 | 定级 | 建议 |
| --- | --- | --- | --- | --- | --- | --- |
| config.yaml（账户 API Key/Secret、MySQL 密码、AI key、Telegram token、UI/量化开关） | config.yaml:31,41-45；写入 main.py:254-258,297-405；.gitignore:9 | 本地 YAML 明文 | 文件在则不丢；删除/写坏→账户与密钥需重填 | 无版本/历史 | P1 | 密钥入密钥库/DB 加密；配置备份；收紧文件权限 |
| signals.csv 形态信号 | config.yaml:46；main.py:149；notify/notifier.py:27-42,54-61；.gitignore:4 | 本地 CSV 追加（无轮转/无锁） | 保留；丢失=长期命中样本丢（DB hits 只 7 天） | 是（目前唯一长期命中档案） | P1 | 归档/轮转；更优是延长 hits 保留 |
| quant.log 事件日志 | quant.py:169,218；.gitignore:18 | 本地 JSONL 追加 | 保留；无轮转→无限增长 | 是（永久事件轨迹） | P2 | 轮转/压缩 |
| quant-positions.json | quant.py:171,397-404,406-420 | 本地 JSON（非原子） | 文件在→恢复；损坏→空，≤5 分钟 audit 重建 | 否 | P1 | 原子写 + DB |
| quant-trades.json | quant.py:170,289-296,358-364 | 本地 JSON（非原子，500 条） | 文件在→恢复；损坏→风控历史清零 | 部分 | P1 | 建议废弃（DB 为准）+ 原子写 |
| quant-unsupported.json | quant.py:172,623-626,630-638 | 本地 JSON | 保留 | 否 | P3 | 保留 |
| quant-manual.json | quant.py:173,616-620；manual_long.py:87-94 | 本地 JSON（非原子） | 见 3.1 手动仓行 | 是 | P1 | 入 DB + 原子写 + 统一路径 |
| agent-state.json | main.py:764；ai/agent.py:608-708；.gitignore:10 | 本地 JSON **原子写**（tmp+fsync+replace+chmod） | 不丢；损坏→拒绝写入（readOnly，agent.py:677-683） | 是（容量受限） | P1 | 进 DB/备份 |
| agent-attachments/ | ai/agent.py:548,576-606；.gitignore:17 | 本地图片文件 | 不丢；无清理/备份 | 否 | P2 | 清理/备份 |
| 前端设置（展示币种/周期/外观） | web/src/App.jsx:40-52,59-70,584-600；web/src/live.js:49-53,242-247 | 浏览器 localStorage + 连接后下发 | 服务端无记录；换设备/清缓存丢失；服务重启未连接时交易范围为空（不过滤） | 否 | P3 | 如需跨设备一致，服务端保存 |

## 四、用户最可能追问的 6 项（直接回答）

1. **模拟仓账本**：正在修（P0-修复中）。工作区已见 `paper_positions`（未平仓快照）、`paper_trades`（全量平仓流水）与启动恢复/停机到期补结算逻辑（decision/paper.py:210-249,585+；storage.py:190-206,381-457；main.py:452-462）。数据库不可用时会显式降级为内存并在 Agent 口径里标注 `source=memory`（decision/paper.py:364-381、ai/agent_tools.py:284-291）。**上线前需按第六节验收，不要只看代码已改。**
2. **Agent 对话/任务**：不是内存，已安全落盘 `agent-state.json`（main.py:764、原子写 ai/agent.py:687-706）。重启不会丢：最多 100 个任务、最近 40 条对话、4000 字摘要、4 个偏好；超出容量旧数据会被裁掉。真正只在内存的是"正在执行的 asyncio 句柄"（ai/agent.py:553-557），重启时运行中的任务会重新排队、运行中的回复标 failed/interrupted（ai/agent.py:863-886）。
3. **后台 watch 任务**：属于 Agent 任务的一种（create_watch → create_task，ai/agent.py:1484-1517,1308-1337），任务本体与 nextRunAt/结果/历史都在 agent-state.json 里，重启后 `start()` 会重新排队 running→queued、recurring 重新调度（ai/agent.py:863-886）。**结论：重启会"接着盯"，但正在跑的那一轮作废，不会丢任务。**
4. **实盘持仓**：权威来源是交易所，本地 `quant-positions.json` 只是镜像（quant.py:171,397-420）。JSON 在→原样恢复；丢失/损坏→5 分钟内由 audit 从交易所 `positionRisk` 补记（quant.py:1942-2028）。**风险不是"仓位没了"，而是开仓依据/peak/保护单号等分析字段丢了**，且本地 JSON 写入不原子（P1）。
5. **决策流水**：事件摘要已进 MySQL `quant_events`（重启不清空、但 3 天后 prune：storage.py:182,940）；页面上的"决策 trace"（为什么开/为什么没开，分阶段因子、评分、风控、执行）只在内存 deque(24)，**重启清零，这是当前最值得补的 P0 业务数据**（quant.py:188-189,236-287,2201-2202）。
6. **gate 拦截记录**：`gate_log` 只内存 deque(40)（quant.py:143），重启清零；每次拦截同时会写一条 skip 事件进 `quant_events`（3 天，quant.py:1251-1257 等），能查摘要原因，但 breakdown/metrics/多周期指标只存在于内存 → P1，建议随决策 trace 一起落库。

## 五、应该进库 vs 可以留内存 vs 已安全落盘

**应该进库（业务记录、可分析）**
- P0：模拟仓账本（修复中）；决策 trace；gate 拦截的完整结构化记录。
- P1：实盘持仓/成交（长期化，尤其成交不要 7 天就删）；风控引擎状态与漂移保护状态；手动仓豁免；Agent 任务/对话（已落盘，进库是为了检索/多端/长期保留）；配置与密钥（至少加密/备份）。
- 保留期（P0-风险）：`quant_trades` / `hits` 建议永久或至少 1 年；`quant_events` 30 天起；`news` 7-30 天；`candles` 30 分钟可以保持（可从交易所回补）。

**属于派生/缓存/临时，可以留内存（P2/P3）**
- 指标与状态：`indicators`、`live_features`、`live_evaluations`、`live_score_history`、`calc_stream`、`dd_state`、`stats`（quant.py:132,139-142,147,155）。
- 行情类：深度、衍生品指标、全市场清单、结构分析、新闻列表（重启后从交易所/DB 重取或恢复：server/hub.py:42,73,77-81；main.py:1240-1289,1064-1070）。
- 账户卡缓存：`hub.last_accounts`（30 秒轮询，accounts.py:98、main.py:1010）。
- 各类短期缓存：step/tick size、mark/balance、HMM、行情延迟、强平窗口、去重表（quant.py:160,167-168,183-198；feed/source.py:35；feed/liquidation.py:27；core/engine.py:33）。
- 广播临时量：`clients/hit_queue/dirty/latest`（server/hub.py:44-49）、asyncio 运行句柄（ai/agent.py:553-557）。

**不在 MySQL 但已落盘本地文件（需说明路径/原子写/丢失后果）**

| 文件 | 写入点 | 原子写 | 容量/保留 | 丢失后果 | 定级 |
| --- | --- | --- | --- | --- | --- |
| agent-state.json（配置目录，main.py:764） | ai/agent.py:687-706 | 是：tmp+fsync+os.replace+chmod 0600 | 100 任务 / 40 对话 / 4000 字摘要 | 任务与对话丢失；损坏时只读拒绝写入 | P1 |
| quant-positions.json（配置目录） | quant.py:397-404 | 否（直接覆盖写） | 当前所有未平仓 | ≤5 分钟 audit 从交易所重建仓位，但分析字段丢 | P1 |
| quant-trades.json（配置目录） | quant.py:358-364 | 否 | 最近 500 笔 | 风控历史 seed 归零、最近成交列表丢失（DB 7 天内仍有） | P1 |
| quant-unsupported.json | quant.py:623-626 | 否 | 少量币种 | 重复尝试不支持合约，刷错误日志 | P3 |
| quant-manual.json（manual_long.py 用相对 cwd 路径） | quant.py:616-620；manual_long.py:87-94 | 否 | 少量条目 | 引擎可能把手动仓当自己的仓平掉（曾发生） | P1 |
| signals.csv（配置 csv_log） | notify/notifier.py:54-61 | 追加（无锁） | 无上限、无轮转 | 长期形态命中样本丢失（DB hits 只 7 天） | P1 |
| quant.log | quant.py:218 | 追加（无锁） | 无上限、无轮转 | 失去永久事件轨迹（DB quant_events 只 3 天） | P2 |
| config.yaml | main.py:254-258,297-405 | 否 | 单文件 | 账户/密钥/设置丢失，需重填 | P1 |
| agent-attachments/img/*（配置目录） | ai/agent.py:576-606 | 单文件写 | 无清理 | 聊天图片断链 | P2 |
| sandbox/（配置目录） | ai/sandbox.py:237-247 | 单文件写 | 无清理/未忽略 | 分析脚本产物丢失（可重跑） | P3 |

## 六、模拟仓修复验收清单（给 Lead）

1. **重启恢复**：开几个纸仓 → 杀进程 → 重启，未平仓数量/entry/sl/peak/ticks 与 `source=mysql` 的 summary 都应一致（decision/paper.py:210-249、quant.py:152、main.py:459-462）。
2. **停机到期补结算**：停 ≥15 分钟再启动，恢复的仓位应按停机期间K线补算 tp/sl/timeout，不能拿重启现价硬平（decision/paper.py:585-620、quant.py:2078+ 传 bars）。
3. **数据库降级**：停 MySQL 时引擎照常 tick（内存账本），summary 显式 `source=memory` 并提示重启清零；MySQL 恢复后新数据继续进库（decision/paper.py:251-297,364-381）。
4. **写入顺序**：平仓先 INSERT `paper_trades`、再 DELETE `paper_positions`，崩在中间最多多一条仓，不能丢成交（decision/paper.py:733-735）。
5. **待核实/建议补**：
   - `recovered` 标志未入库：paper.py:720 的 `recovered` 字段不在 storage.py:414-457 的 INSERT 列里 → 重启补结算的交易在 DB 里无法区分。
   - `drop_missing` 未被调用（decision/paper.py:496）：已移出监控的币在 paper_positions 的残行重启后会被恢复但永不 tick，建议 restore 后按当前监控清理，或接上 drop_missing。
   - `opened` 统计是"累计平仓数 + 当前未平仓数"的近似（decision/paper.py:238-240），不是真实开仓数。
   - 生产 `config.yaml` 未显式设 `storage.paper_keep_days` 时按 0 = 永久（main.py:202），确认是否符合预期。
6. **测试**：新增的 `tests/test_paper_persist.py`（FakeStore，覆盖落库/恢复/补结算/降级）与 `tests/test_paper_store.py`（SQL 适配）都未提交，验收时一并跑。

## 七、建议修复顺序

1. **模拟仓验收**（task-3/task-4），这是用户已明确不满的点，先闭环。
2. **决策 trace + gate 拦截落库**（P0）：新增 `decision_traces` 表（ts/account/symbol/pattern/status/payload JSON），保留 ≥30 天；gate 记录并入或用独立表。
3. **保留期调整/归档**（P0-风险）：`quant_trades`、`hits` 永久或 1 年；`quant_events` 30 天+；`news` 7-30 天；`paper_trades` 维持 0=永久（config.example.yaml:69-71）。
4. **风控状态 + 漂移保护状态落库**（P1）：日初权益/当日盈亏/连亏/暂停/下单次数、cautionUntil/shadowUntil，启动恢复。
5. **四个 quant-*.json 改造**（P1）：原子写 + 手动登记与持仓快照入 DB + manual_long.py 与引擎路径统一；顺带把 `quant-*.json` 加进 .gitignore。
6. **Agent 状态进库/备份 + Telegram offset 落盘 + 附件清理**（P1/P2）。
7. 可选：signals.csv 归档、quant.log 轮转、前端展示选择服务端化（当前仅浏览器 localStorage，web/src/App.jsx:40-52,584-600）。

## 八、审计边界与顺带发现

**边界**
- 未启动服务、未连 MySQL：`storage.enabled` 实际状态、各表真实行数、prune 是否实际执行未做运行时核验；本报告只对代码路径下结论。
- 工作区正在变更：`git status` 显示 M `README.md`、`ai/agent.py`、`ai/agent_tools.py`、`config.example.yaml`、`decision/paper.py`、`main.py`、`quant.py`、`storage.py`；新增未跟踪 `tests/test_paper_persist.py`、`tests/test_paper_store.py`。模拟仓相关行号以审计时工作区为准，提交后可能偏移，以 task-3/task-4 的验证结论为准。
- 未审计 `web/node_modules`、`tests`（运行时业务）与 `__pycache__`。

**顺带发现（非持久化缺陷，但影响可分析性/可信度，标"需核实"）**
1. `hub.lag_p50` 全仓只有初始化和读取、从未赋值（server/hub.py:49,438；main.py:737）→ 前端"延迟 p50"永远为空；`latency_report_sec`（config.example.yaml:175）没有任何 Python 读取，开关名存实亡。
2. `hub._article_cache` 定义后从未使用（server/hub.py:67）。
3. `quant.state["seq"]` 从未使用（quant.py:130,645）。
4. `paper.drop_missing` 从未被调用（decision/paper.py:496）。
5. `quant-positions.json` / `quant-trades.json` / `quant-unsupported.json` / `quant-manual.json` 不在 .gitignore（.gitignore:1-18 未列），运行时会被 git 看到，建议忽略。
6. `candles` 只保留 30 分钟：K线可从交易所回补，但**历史K线不能从本地数据库回溯**；若要做长期回测，需要另建K线归档（当前属于合理取舍，但应让用户知道）。
