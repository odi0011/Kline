
# 审计修复独立验证报告（推送前终检）

- 验证者：audit-verifier（独立于全部实现者；只验证，未修改任何实现文件）
- 验证时间：2026-11-03
- 验证对象：工作区未提交改动（HEAD = 5fc1ff0，分支 main → origin/main，本地无未推送 commit）；
  包含 P2 修复后的二次复验（Lead 只改 storage.py 一处 + 移除测试 xfail 标记）
- 环境：Windows / python 3.12.6 / pytest 9.1.1；所有 pytest 命令均加 -p no:cacheprovider
- 验证边界：未连接生产 MySQL（storage 用隔离 SQLite 适配器跑真实 SQL）；未启动 main.py 服务；未 commit/push；未读/改 config.yaml（含密钥）
- 本次产物：tests/test_audit_e2e.py（新增，586 行 / 14 个用例）、reports/audit-fixes-verification.md（本报告）

## 一、结论

**PASS —— 可以推送。**

- 本轮审计修复的 6 项验收点（P0 trace/gate 落库恢复、P1 风控/漂移状态持久化、JSON 原子写、
  P0 保留期延长、降级安全、lag_p50 死字段）全部通过独立端到端验证。
- 全量回归 500 passed，失败集合与改动前基线一致（仅 1 个既有环境失败 test_sandbox，原因已核实），
  无新增回归。
- 首轮发现的 **1 个 P2 数据质量 bug**（gate 拦截记录无 code 时 status 列落成字符串 "None"，
  storage.py:461-462）已由 Lead 修复，本轮已完成二次复验：无 code 的 gate 记录 status 落库为空串，
  有 code 时仍回落 code、有 status 时 status 优先、trace 分支不受影响；修复只动那一行，无副作用。
  **P2 已关闭，不阻塞推送。**
- 推送前检查清单全部通过：config.yaml 仍被 ignore 且未被跟踪、无密钥/调试残留/大文件、
  11 个实现改动与 9 个新增文件均为预期内容。

## 二、独立证据（命令 + 数字，均在最终工作区执行的最后一次结果）

### 2.1 新增独立 e2e 测试

~~~
python -m pytest tests/test_audit_e2e.py -q -p no:cacheprovider
=> 14 passed in 0.53s
~~~

- 14 个用例全部是独立编写的验收（真实 storage.Storage + SQLite 适配器 + 真实 QuantEngine/RiskEngine），
  未使用 FakeStore，未照抄实现者测试断言。
- 12 个首轮验收 + 1 个 P2 回归用例（gate 无 code → status 空串，修复后由 xfail 转真实通过）
  + 1 个本轮新增的 status 回落矩阵用例（gate 有 code 回落 code / status 优先 / trace 不受影响），
  用于证明修复无副作用。

### 2.2 全量回归

~~~
python -m pytest tests -q -p no:cacheprovider
    --ignore=tests/test_structure.py --ignore=tests/test_account_heatmap.py
=> 1 failed, 500 passed, 3 skipped, 51 subtests passed in 20.35s
~~~

失败明细（唯一失败，既有环境问题）：

~~~
tests/test_sandbox.py::test_allowed_command_runs
    sandbox.run(root, "echo hello", isolate=False) -> ok == False
~~~

核实为环境问题、与本次改动无关：

- ai/sandbox.py 不在 git 改动列表（git diff 为空）；
- 本机没有 echo 可执行文件：Get-Command echo 只是 PowerShell 别名，where.exe echo 找不到任何文件，
  所以 subprocess 调 echo 失败；
- 3 个 skipped 全部来自 tests/test_sandbox.py 的平台跳过（当前平台不支持 / 仅 Linux），该文件未改动。

### 2.3 与改动前基线对比

| 指标 | 改动前基线（任务给定） | 本次最终 | 差值 |
| --- | --- | --- | --- |
| passed | 463 | 500 | +37 |
| 已知环境失败 | 1（test_sandbox echo） | 1（同一用例，同一原因） | 0 |
| 失败集合变化 | — | 无新增失败 | 0 |
| skipped | 未记录 | 3（test_sandbox 平台跳过，文件未改） | 既有 |
| xfailed | 0 | 0（P2 修复后原 xfail 转 pass） | 0 |

新增的 37 个通过用例来自本轮及上一轮新增测试文件：test_paper_store.py、test_paper_persist.py、
test_paper_e2e.py、test_quant_state.py、test_storage_retention.py、test_audit_e2e.py（其中本文件 14 通过）。

### 2.4 被 ignore 的两个测试文件说明（按要求）

- tests/test_structure.py：脚本式测试，第 87 行 sys.exit(...)，import 时即退出，必须 ignore；该文件未改动。
- tests/test_account_heatmap.py：第 17 行 pytest.importorskip("playwright.sync_api")，
  第 23-25 行在缺 node / web/node_modules/esbuild 时 skip；依赖本机 Playwright+node 环境且该文件未改动
  （git status 为空），ignore 不构成回归证据缺口。

### 2.5 复验期间工作区变化核对（P2 修复）

- storage.py：与首轮验证版本做“累计 diff 的 diff”逐行比对，差异**仅**为 save_decision_trace 的
  code/status 两行（新增 code 变量 + or "" 兜底 + 注释），其余 hunk 完全一致；其余 10 个实现文件
  SHA256 与首轮基线完全相同（见第八节）。
- tests/test_audit_e2e.py：移除 test_gate_without_code_stores_empty_status 的 strict xfail 装饰器
  （现真实通过），并新增 test_gate_status_fallback_matrix 覆盖修复副作用矩阵。
- config.example.yaml：复验期间被追加 1 行注释（macro_events 示例说明，无功能键变化）；
  yaml.safe_load 通过、quant.decision 各值不变、唯一读取该文件的测试 test_decision_extras.py 全量通过。
- 最终 git status 仍为 11 modified + 9 untracked，无范围外文件。

## 三、逐项验收表（独立证据）

| # | 验收项 | 独立验证方法（真实 Storage + 真实引擎，非 FakeStore） | 结果 |
| --- | --- | --- | --- |
| 1 | P0 决策 trace 落库 | QuantEngine 真实 _trace_start/_trace_stage/_finish_trace，注入 trace_storage_fn=store.save_decision_trace；断言 decision_traces 行数=3、列 ts/kind/symbol/status 正确、payload=json.loads 后与 trace 全量相等（含 stages/factors/execution）、中文 ensure_ascii=False 原样入库 | PASS |
| 2 | P0 决策 trace 重启恢复 | 新引擎注入 trace_read_fn=store.decision_traces + state_read_fn=store.quant_state + restore_persistent_state()；断言 decision_traces 内部时间升序（NOW/NOW+1s/NOW+2s）、latest_traces 同币只留最新、payload 完整、元数据 kind/savedTs 补齐；24 条上限（30 条→最新 24 条升序） | PASS |
| 3 | P0 gate 拦截落库/恢复 | 真实 _append_gate 两次（带 code / 不带 code）；断言 kind=gate、status 回落 code（"score_gate"）、payload 完整；新引擎恢复后 gate_log 升序、40 条上限（45 条→最新 40 条升序）；**P2 修复后**再覆盖 6 种 status 组合矩阵 | PASS（P2 已修复并复验，见第五节） |
| 4 | P1 风控状态持久化 | RiskEngine 真实 record_trade 造出 3 类状态：4 连亏暂停、单亏后同币冷却、当日 -4.5% 日亏限额；save_persistent_state(force=True) 写 quant_state.risk；新引擎 restore 后 risk.snapshot() 与旧引擎逐字段相等，同一 RiskInput evaluate 的 allowed/code/reason/cooldown_sec 完全一致（loss_streak_pause / cooldown / daily_loss_limit） | PASS |
| 5 | P1 漂移 caution/shadow 恢复 | 真实 _handle_drift 连续触发 caution、shadow（清限流后二次触发），force 落库；断言 quant_state.drift 数值等于内存值；新引擎 restore 后 _drift_caution_until/_drift_shadow_until 完全一致 | PASS |
| 6 | P0 风险保留期 90/365/30/7/30 | 默认 Storage 构造后断言属性 (hit_days, trade_keep_days, quant_event_days, news_days, trace_days, paper_keep_days) == (90,365,30,7,30,0)；构造旧/新行后 prune，断言 hits 200d 删/1d 留、quant_trades 400d 删/300d 留、quant_events 40d 删/20d 留、news 10d 删/5d 留、decision_traces 40d 删/20d 留 | PASS |
| 7 | 0=永久语义 | 全 0 配置 store：5 张业务表 500 天前旧行 prune 后全部保留；单表 0（只 hits 永久）时其他表按默认值照删（hits 留、news/quant_events 删） | PASS |
| 8 | candles/equity/paper 逻辑不变 | 默认配置下 candles 2h 前删/10min 留、equity 3d 前删/12h 留；paper_keep_days=0 时 500 天平仓流水保留、未平仓 paper_positions 永不删；paper_keep_days=7 时 9d 删/1d 留且 paper_positions 保留 | PASS |
| 9 | JSON 原子写成功路径 | _atomic_write_json 写中文数据返回 True、文件是合法 JSON、中文非转义、无 .tmp 残留；引擎真实 _save_positions 路径同样合法且无 .tmp | PASS |
| 10 | JSON 原子写失败路径 | monkeypatch quant.os.replace 抛 OSError：返回 False、不抛、旧文件字节不变、.tmp 被清理；数据不可 JSON 序列化同样安全失败；目标目录不存在也返回 False 不抛 | PASS |
| 11 | 降级（storage.enabled=False） | Storage({}) 不连库：save_decision_trace/decision_traces/save_quant_state/quant_state/prune 全部安全 no-op；引擎注入禁用 storage 后 _finish_trace/_append_gate/save_persistent_state 不抛且内存逻辑照常；新引擎 restore 返回 {"traces":0,"gates":0,"risk":False,"drift":False} | PASS |
| 12 | 小修 lag_p50 死字段 | 静态守卫：main.py:977-985 每轮 sweep 用 feed.lag_stats()["p50"] 赋值 hub.lag_p50；server/hub.py:49 初始化、:438 随 stats 广播；main.py:762 进 Agent health；config.example.yaml 删除无任何 Python 读取的 latency_report_sec（全仓 grep 仅 audit 报告提及） | PASS（注：web/src 目前无 lag_p50 消费点，字段已进 /stats 广播与 Agent health，不阻塞） |
| 13 | main.py 注入顺序/函数名 | 静态核对 main.py:481-484 注入的 4 个函数 trace_storage_fn=storage.save_decision_trace、trace_read_fn=storage.decision_traces、state_storage_fn=storage.save_quant_state、state_read_fn=storage.quant_state 与 quant.py:171-174 属性名/签名逐一匹配；main.py:486 restore_persistent_state() 在 4 个注入之后、在所有 storage 创建之后 | PASS |
| 14 | storage 新方法 SQL/列宽/容错/limit/迁移 | 静态审查 + 真实 SQL 执行：kind<=12/symbol<=16/status<=24 截断；decision_traces limit 夹 1..1000；坏 payload 不抛仍返回元数据；quant_state upsert/坏 JSON 返回 None；INSERT IGNORE + trade_key 唯一幂等；hits 索引迁移在 SQLite 方言下探测失败静默跳过、MySQL 分支用 SHOW INDEX/ALTER 静态审查正确；MySQL DDL（MEDIUMTEXT/UNIQUE/ALTER ADD UNIQUE）静态审查无语法问题 | PASS（无法连生产库，见剩余风险） |
| 15 | save 节流/force/manage 位置 | 静态核对 quant.py:391-412 60s 节流；force 调用点 _record_trade（:507-508）在 risk.record_trade 之后、_handle_drift（:473-474）在 caution/shadow 设定之后；manage 末尾 :1908-1911 无条件调用（在 _refresh_drawdown 可能提前 return 的情况下仍执行） | PASS |
| 16 | 异常吞噬/线程锁 | 所有落库点（_finish_trace/_append_gate/save_persistent_state x2、restore 四段、_event/trade 落库）均独立 try/except，DB 异常不影响交易主流程；storage._q/_qall/_qmany 统一 storage._lock，quant 写状态均在 _op_lock 串行化的 on_signal/manage/_record_trade 内，无嵌套锁/反序加锁 | PASS |
| 17 | 保留期配置一致性 | main.py:202-208 传 hit_days/paper_keep_days/quant_trade_days/quant_event_days/news_days/trace_days 默认 90/0/365/30/7/30，与 config.example.yaml:70-77 完全一致；yaml.safe_load 解析通过 | PASS |
| 18 | README/.gitignore | README.md:15-21 数据表说明与实现一致；.gitignore 新增量化运行时 JSON/quant-*.json.tmp/sandbox/ 共 9 行，config.yaml 仍在第 9 行被 ignore；git check-ignore -v config.yaml => .gitignore:9:config.yaml | PASS |
| 19 | 密钥/调试残留/大文件 | 对 git diff + 全部未跟踪文件做正则扫描（AWS key/OpenAI sk-/64hex/base64 长串/password|secret|token 赋值/mysql URI/Telegram token）=> 0 命中；改动文件中 TODO/FIXME/pdb/breakpoint/console.log => 0；>1MB 文件（排除 .git/node_modules/web/dist/__pycache__）=> 0；main.py 新增仅 3 条用户可见启动日志 print | PASS |

## 四、代码审阅要点（除上面验收表外的补充核对）

1. 数据流完整：trace/gate 持久化是"内存 deque + 可选落库"，storage 为 None 或 disabled 时行为与改造前一致
   （纯内存）；main.py 恢复前已先完成 4 个注入，且恢复异常被外层 try/except 兜住，不会影响服务启动。
2. 恢复顺序正确：storage.decision_traces 返回 newest-first，quant.restore_persistent_state 对 trace/gate
   都先 reversed 再 append，deque 追加后即内部时间升序；latest_traces 按升序覆盖，最终保留每个币最新一条。
   已用 30 条 trace + 45 条 gate 实测 24/40 上限与顺序。
3. 落库数据形状正确：trace payload 原样存、原样取；gate 的 status 列回落 code（无 code 时曾落成 "None"，
   已修复并复验，见第五节）；decision_traces 的 id/kind/savedTs 用 setdefault 补齐，不覆盖 payload 原键
   （payload 的 trace id "tr-..." 优先保留）。
4. 幂等与迁移：paper_trades 用 INSERT IGNORE + UNIQUE(trade_key=symbol:entryTs) 防网络重试导致统计翻倍；
   recovered/trade_key 老库渐进迁移（SHOW COLUMNS 探测→ALTER），fresh 库直接建全列；hits 索引迁移失败
   静默跳过不影响启动。
5. 节流与 force 的语义合理：manage 每 5 秒一轮、非 force 60 秒一次；风控真正会变的时刻（成交、漂移事件）
   都 force 落库，所以 DB 快照滞后最多 60 秒且仅影响 order_times 这类非关键字段（连亏/暂停/日亏/冷却均由
   成交 force 保存）。
6. 锁与异常：storage._lock 为普通 Lock，但所有落库调用点不存在 storage 方法嵌套调用，无自锁风险；
   quant 侧状态写入均处于 _op_lock 内，无并发写 deque 的迭代崩溃路径；落库异常全部被吞掉且不改变内存状态。
7. 保留期变更的安全面：hits 90 天只影响"删得晚"，不会删更多数据；pattern_stats 统计窗口仍由
   decision_cfg.pattern_window_days（默认 7 天）控制，未被误扩大；decision_traces 30 天首次上线时表为空；
   quant_trades 365 / news 7 / quant_events 30 相比旧的 7/3/3 都是放宽，不会误删历史。
8. 降级一致性：main.py 用 paper_store=storage if storage.enabled else None 显式降级，存储层 disabled 时
   所有新方法 return 安全默认值，与旧行为一致。

## 五、发现的问题（P2 已由 Lead 修复并复验；P3 只报告未修）

### P2：gate 拦截记录无 code 时 status 落成字符串 "None" —— 已修复并复验（关闭）

- 位置：storage.py::save_decision_trace（首轮版本 :461-462）
- 首轮问题：kind="gate" 且记录既无 status 也无 code 时，None or None -> None -> str(None) == "None"。
  quant.py 的 4 类真实 gate 写入中 3 类没有 code（quant.py:1362 评分门、:1395 环境门、:1485 多周期门；
  :1592 统一风控门有 code），即绝大多数 gate 记录 status 列会是 "None"。
- 首轮定级：仅 decision_traces 冗余列数据质量，payload/恢复/交易主流程均不受影响 -> P2、不阻塞推送。
- 修复（Lead，仅改 storage.py 一处）：
  ~~~
  # 注意 or 链最后必须再 or ""：gate 记录常常没有 status/code，
  # 否则 str(None) 会把冗余列写成字符串 "None"。
  code = trace.get("code") if kind == "gate" else ""
  status = str(trace.get("status") or code or "")[:24]
  ~~~
- 本轮复验（修复后，全部真实跑通过）：
  1. tests/test_audit_e2e.py 去掉 strict xfail 后原用例真实通过：无 code 的 gate -> status == ""；
  2. 新增 test_gate_status_fallback_matrix 覆盖副作用矩阵：gate 无 code -> ""；gate 有 code 无 status
     -> 回落 code；gate 有 status -> status；gate 有 status+code -> status 优先（与修复前语义一致）；
     trace 有 status -> status；trace 无 status -> ""；6 种组合的 payload 均原样完整；
  3. 既有 gate 往返/状态用例 tests/test_storage_retention.py + tests/test_quant_state.py
     + tests/test_decision_extras.py 共 30 passed；
  4. 与首轮基线做“累计 diff 的 diff”比对：storage.py 变化仅这两行，其余 hunk 与 10 个实现文件哈希均未变。
- 结论：P2 关闭，修复无副作用，不改变 payload/恢复/交易行为。

### P3（建议后续优化，不阻塞）

1. quant.py:391-412 save_persistent_state 在两次落库都抛异常时仍更新 _last_state_save，
   DB 抖动后状态重试被推迟 60 秒。
2. quant.py:343-389 restore 的 risk 快照会覆盖 QuantEngine.__init__ 里 _seed_risk_history（:423-439）从本地
   quant-trades.json 种子的状态：若 DB 快照比本地 JSON 滞后（最长 60 秒，且成交都 force 落库，窗口极小），
   重启后可能丢失极少量非成交型状态（主要是 order_times）。风险有界。
3. quant.py:379-386 drift 只要 state_read_fn("drift") 读到 dict 就置 restored["drift"]=True，即使两个时间戳
   都是 0，启动日志可能出现"已恢复漂移保护状态"的误导性提示（仅日志）。
4. manual_long.py:94-101 新原子写没有 try/except：写临时文件/替换失败会抛异常并可能留 .tmp；
   该文件是独立手动脚本、不在服务主流程，且原来也是直接写，风险可接受。
5. main.py:977-985 lag_p50 每 0.5 秒调用 feed.lag_stats()，其内部会对最多 5000 条 lags 排序；
   实测开销可忽略，但如需极致可改为 5 秒节流。另：web/src 当前没有任何 lag_p50 消费点，
   审计报告所说"前端状态条读取"目前不存在（字段已进 /stats 广播与 Agent health，属后端修复到位）。

## 六、剩余风险与验证边界

1. 未连生产 MySQL：DDL/DML 用真实 SQL（SQLite 方言适配）+ MySQL 语法静态审查；REPLACE INTO /
   INSERT IGNORE / UNIQUE(trade_key) / SHOW INDEX WHERE / ALTER TABLE ADD INDEX / MEDIUMTEXT 用法均
   符合 MySQL 8 语法，但真实执行差异（字符集、锁等待、权限）无法在本机验证。
2. 未启动 main.py 服务、未做真实进程重启演练：恢复路径是用真实引擎 + 真实 Storage 在同一进程内
   "写→新引擎→恢复"验证；启动顺序为静态核对（注入先于恢复）。生产首次启动会新建/迁移表与索引，
   预计为一次性动作。
3. Playwright 浏览器测试（test_account_heatmap / dashboard.browser.mjs）未跑：本机缺 node/esbuild/
   Playwright 依赖，相关文件本次未改动。
4. 线上首次启动若 MySQL 账号没有 ALTER 权限，paper_trades 补列与 hits 索引会静默跳过（不影响 fresh 表，
   但老库不会补列/索引）；建议推送后首次启动检查日志有无 "[storage] 建表失败" 及确认
   decision_traces/quant_state 两表存在。
5. 保留期放宽后，hits / quant_trades 表会随时间增长（业务有意为之）；后续如需控体积可调小
   storage.*_days（0=永久，注意不要把 0 当无限增长误配）。

## 七、推送前检查清单（全部实测通过）

| 检查项 | 结果 | 证据 |
| --- | --- | --- |
| config.yaml 未入库 | PASS | git ls-files 无 config.yaml；git check-ignore -v config.yaml => .gitignore:9；git status 不含 config.yaml |
| 无密钥泄露 | PASS | 对 git diff + 全部未跟踪文件 7 类密钥正则扫描 0 命中（10 个来源 = 11 个 tracked 改动的 diff + 9 个未跟踪文件） |
| 无调试残留 | PASS | 改动文件 TODO/FIXME/pdb/breakpoint/console.log 0 命中；main.py 仅 3 条启动日志 print |
| 无大文件进 git | PASS | 工作区 >1MB 文件（排除 .git/node_modules/web/dist/__pycache__）0 个；待提交文件合计 181,158 字节（约 177KB） |
| 运行时不落库文件已 ignore | PASS | .gitignore:20-28 覆盖 quant-positions/trades/unsupported/manual.json、quant-*.json.tmp、sandbox/；quant.log 原有 ignore |
| 改动范围符合预期 | PASS | git status：11 modified + 9 untracked（8 个新增报告/测试 + 本报告），无意外文件；git diff --stat 11 files, +1172/−64（P2 修复 +2 行、config.example 复验期 +1 行注释） |
| P2 修复范围 | PASS | 与首轮基线比对：仅 storage.py save_decision_trace 两行 + 测试 xfail 移除；其余 10 个实现文件哈希未变（见第八节） |
| 新增报告/测试将入库 | 待 Lead 提交 | tests/test_audit_e2e.py、reports/audit-fixes-verification.md、reports/inmemory-audit.md、reports/paper-persistence-verification.md 及上一轮 5 个测试文件当前均为 ?? 未跟踪，提交时需 git add |
| 分支/远端 | PASS | git rev-parse --abbrev-ref '@{u}' => origin/main；git log origin/main..HEAD 为空（当前改动尚未提交，由 Lead 提交） |

## 八、复验后基线哈希（P2 修复后最终工作区，推送前请比对）

| 文件 | SHA256(16) | 文件 | SHA256(16) |
| --- | --- | --- | --- |
| storage.py（P2 修复后） | A736B9A48F58B871 | quant.py | BE7AB47C12A026E1 |
| decision/risk.py | F43CAC741C69D944 | decision/paper.py | C615A08373DFCAC8 |
| main.py | 3F8B22F4E161DF8B | manual_long.py | C84CB0E2695225EE |
| config.example.yaml（注释改动） | 668C879FE9F14BBA | README.md | 37D0EADE32C22EFD |
| .gitignore | 9413EC6029CC3652 | ai/agent.py | 6BC507F089BD4521 |
| ai/agent_tools.py | 7B8137A4E081644F | tests/test_audit_e2e.py（复验后） | AB792A99B4FDC374 |

**tracked 改动内容哈希（推送前基线，只覆盖 11 个 tracked 修改文件，不含未跟踪的 reports/tests）：**

~~~
cmd /c "git diff | git hash-object --stdin"
=> 62954a95bdd782ee5ca4736400fe79e165e5fc2b
~~~

注意：PowerShell 管道会改变文本编码，得到的是另一个值；必须用 cmd 字节管道（或 git diff --output 后再
hash-object 文件）。若推送前该哈希或上表任一文件哈希变化，本报告的测试数字需重跑（两条 pytest 命令
分别约 0.5s / 20s）。

## 九、最终判定

**可以推送（PASS）。** P2 已由 Lead 修复并完成二次复验：独立 e2e 14/14 全过（含修复回归与副作用矩阵）、
全量 500 passed + 0 xfailed（唯一失败为既有 test_sandbox echo 环境问题）、既有 gate 往返用例全过、
修复仅动 storage.py 一处且其余实现文件哈希未变。剩余 P3/边界风险均不阻塞上线。
建议 Lead 直接 git add -A、commit、push origin main（main 自动部署）；推送后首次启动确认日志无
"[storage] 建表失败"，并确认 decision_traces/quant_state 两表已创建。若提交前工作区再有任何改动
（对照第八节哈希），请重跑第二节两条命令后更新本报告。
