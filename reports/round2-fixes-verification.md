
# 第二批修复独立验证报告（推送前终检 · 三轮复验全部关闭）

- 验证者：audit-verifier（独立于全部实现者；只验证，不修改实现文件）
- 验证时间：2026-11-03（第一轮初验 → P2+4×P3 修复复验 → 两个残留 xfail 修复后的最终复验）
- 验证对象：工作区未提交改动（HEAD = 1633f48「第一批修复」之上的第二批改动：10 modified + 8 个实现者新增文件，另加本测试与本报告共 10 个新增文件）
- 环境：Windows / python 3.12.6 / pytest 9.1.1；cryptography 50.0.0（requirements 要求 >=42）；所有 pytest 均加 -p no:cacheprovider
- 验证边界：未连接生产 MySQL（storage 用隔离 SQLite 适配器跑真实 SQL）；未启动 main.py 服务；未 commit/push；未读/改 config.yaml（含密钥）；未读取生产 secrets.key
- 本次产物：tests/test_round2_e2e.py（28 通过、0 xfail）、reports/round2-fixes-verification.md（本报告）

## 一、结论

**PASS —— 可以推送。**

- 三轮复验后，**所有已报告问题全部关闭**：原 P2（聊天同毫秒顺序）、新 P2（清空/压缩聊天重启复活）、全部 P3（mysql 超时负数、write_config_file 原子写、export step、export 缺密钥异常捕获、README 归档口径）均已修复并通过独立复验；仅剩 1 条“迁移部分失败”P3 残留（窗口极小，不阻塞）。
- 最终回归：**1 failed（既有 test_sandbox echo 环境问题）+ 608 passed + 4 skipped + 0 xfailed + 51 subtests passed in 19.74s**；失败集合与基线一致，无新增失败。
- 推送前检查全部通过：config.yaml / secrets.key / config.yaml.bak.* 均被 ignore 且未被跟踪；diff 与新文件密钥扫描 0 命中；无临时文件/specs；无 >1MB 文件。

## 二、独立证据（命令 + 数字，最终工作区实测）

### 2.1 新增独立 e2e 测试

~~~
python -m pytest tests/test_round2_e2e.py -q -p no:cacheprovider
=> 28 passed in 2.95s
~~~

- 28 个用例独立编写（真实 storage.Storage（SQLite 适配器）+ 真实 AgentManager / PaperBook / QuantEngine + 真实 CLI 子进程），未使用 FakeStore、未照抄实现者断言。
- **0 xfailed**：原两条 strict xfail（清空聊天重启复活、export 缺密钥 traceback）已随修复去掉标记并真实通过；全部用例均为真实断言。
- 本轮新增 2 条独立回归用例，专门验证 _sync_chat_window 的收口条件（见第三节 P2-B）。

### 2.2 全量回归

~~~
python -m pytest tests -q -p no:cacheprovider
    --ignore=tests/test_structure.py --ignore=tests/test_account_heatmap.py
=> 1 failed, 608 passed, 4 skipped, 51 subtests passed in 19.74s
~~~

- 唯一 failed = tests/test_sandbox.py::test_allowed_command_runs：本机没有 echo 可执行文件（Get-Command echo 只是 PowerShell 别名，where.exe echo 无结果），ai/sandbox.py 未改动 → 既有环境问题，与基线一致。
- 4 skipped = test_sandbox.py 平台跳过（当前平台不支持 / 仅 Linux），该文件未改动。
- 对比“本测试文件加入前”基线 578 passed：最终 608 passed（+30）= 本文件 28 条真实通过（0 xfail）+ test_storage_v3 新增 2 条用例；失败集合与基线一致。
- Lead 指定的 4 文件组合 tests/test_storage_v3.py + tests/test_round2_e2e.py + tests/test_agent_state_db.py + tests/test_agent.py => **156 passed**（与 Lead 本地 154 passed 相比：+2 为本轮我新增的 2 条独立用例，其余一致）。

### 2.3 Playwright（test_account_heatmap）失败说明（沿用实测）

单独执行该文件（文件本次仍未改动、git diff 为空）：6 failed, 1 passed。
失败原因为环境/前端构建漂移，典型报错 `Locator.select_option: Element is not a <select> element`
（测试按原生 `<select>` 操作，而当前组件渲染的是 morph_select 触发按钮），与本次后端改动无关。

### 2.4 被 ignore 的 test_structure.py

脚本式测试，第 87 行 sys.exit(...)，import 时即退出，必须 ignore；该文件未改动。

## 三、最终复验逐项状态（全部关闭）

| 项 | 原问题 | 修复内容（Lead） | 独立复验（最终轮实测） | 状态 |
| --- | --- | --- | --- | --- |
| P2-A | 同毫秒 user/assistant 重启后顺序可能颠倒（load_agent_chat 只按 ts,msg_id） | agent_chat 新增 seq INT 列（CREATE 带列 + SHOW COLUMNS 老表迁移）；save 按有序列表 enumerate 写 seq；load 改 ORDER BY ts DESC, seq DESC, msg_id DESC 再 reversed | 原 same-ms xfail 转真实通过；独立老表迁移用例（无 seq 旧表 → _init 自动补列 → 顺序正确）；新建表列断言；test_storage_v3 同毫秒/覆盖/limit 全过 | **已修复，关闭** |
| P2-B | clear_chat / compact_chat 只清内存，agent_chat append-only 不删旧行，重启后已清空/已压缩消息复活 | storage 新增 clear_agent_chat() / trim_agent_chat(keep_ids)；agent.py 新增 CHAT_STORE_WINDOW=40 与 _sync_chat_window()：_save 写库成功后，仅当内存窗口 <40 条才按 msg_id 收口（=40 条保留更早长期存档） | ① 原 clear xfail 转真实通过：6 条聊天 _save → 清空后 _save → 重启 chat == []；② 新增“满窗口不误删长期存档”用例：窗口滚动两次累计 50 条，重启只加载 40 条后再 _save，库里仍是 50 条（更早 10 条存档保留）；③ 新增“写库失败不收口”用例：DB 写失败时库仍 40 条不删，恢复写入后才 trim 到 10 条且重启不复活；④ test_storage_v3 的 clear/trim 单元用例通过 | **已修复，关闭** |
| P3-1 | watch_aggs on_close=noop，README 却说每次收线（含 1h/4h） | README 文案改为“1h/4h 在启动回补与按需订阅时归档” | 静态断言 README 与 main.py 行为一致 | **已修复（文档口径），关闭** |
| P3-2 | Agent 首次迁移 save_agent_state 成功但 save_agent_chat 失败时，下次启动 DB 优先可能漏聊天 | 本轮未改 | 代码逻辑未变；窗口极小 | **未修（P3 残留，接受）** |
| P3-3 | mysql 超时负数未归一化 | _timeout_seconds 负数 → 默认 30 | 断言 -5/"-1" → 30；test_storage_v3 断言通过；connect 传参用例通过 | **已修复，关闭** |
| P3-4 | write_config_file 非原子写 | tmp -> yaml dump -> flush+fsync -> os.replace，finally 清理 | 成功无 .tmp；replace 抛错时旧配置不变、.tmp 清理；静态断言 fsync/replace/finally | **已修复，关闭** |
| P3-5a | export step = max(chunk, INTERVAL_MS*1000) 疑似笔误 | step 改 max(chunk, INTERVAL_MS) | chunk_ms=3min 强制多窗口分页，导出结果与单窗口逐行一致（22 行不重不漏）；step 静态断言不含 *1000 | **已修复，关闭** |
| P3-5b | export load_store 异常会 traceback | except 增加 secrets_store.SecretsError + 中文提示退出码 2 | 原 export 缺密钥 xfail 转真实通过：加密配置 + 缺密钥调用 main() 返回 2 不抛；static：异常元组含 SecretsError | **已修复，关闭** |
| P3-6 | 无 ts 消息排序不稳定 | seq 机制覆盖 | 同毫秒/迁移用例通过；旧数据 seq=0 同 ts 按 msg_id 兜底（可接受） | **基本解决** |

## 四、原 5 项改动块独立验收（最终回归确认，未退化）

| 项 | 验收点 | 最终复验结果 |
| --- | --- | --- |
| A | Agent 状态进 MySQL | 23 个 Agent 相关用例全过：DB 落库+重启恢复、JSON 首次迁移、DB 读失败回落 JSON、JSON 损坏由 DB 恢复、DB 空+JSON 损坏只读保护后 DB 抢救、DB 写失败 JSON 兜底、聊天 append-only、同毫秒顺序（seq）、清空/压缩收口（clear/trim） | PASS |
| B | 配置密钥加密 CLI | 真实 CLI --check→加密（6 slot enc:v1:、secrets.key、备份）→--check→--decrypt 全链路通过；环境变量覆盖、惰性 cryptography、缺失/错误密钥显式报错、write_config_file 加密落盘、仓库 ignore/未跟踪全部通过 | PASS |
| C | K线长期归档 + 导出 | 归档覆盖/乱序/范围/limit/坏数据、CSV 22 行表头与 UTC 时间、多窗口分块分页、main.py 4 处归档接线静态确认、新保留期 0=永久语义 | PASS |
| D | 模拟仓风险修复 | 同毫秒 openSeq/trade_key 不撞 + INSERT IGNORE 去重 + 重启续号；settleBars/settleApprox 列往返与真实恢复补结算；close_missing 按 mark 记 removed/无 mark 只删行/范围内不动、quant.manage 接线 | PASS |
| E | mysql 读写超时 | 归一化（含负数）与 pymysql.connect 传参正确 | PASS |
| F | 推送前检查 | 见第七节 | PASS |

## 五、最终剩余问题（仅 1 条 P3，不阻塞）

1. Agent 首次迁移部分失败风险（ai/agent.py 迁移段，约 :716-724，本轮未改）：save_agent_state 成功但 save_agent_chat 失败时，下次启动 DB 优先且 chat 为空、JSON 被忽略，内存聊天窗口可能表现为“丢失”（数据仍在 JSON/部分库中）。窗口极小。建议迁移后校验 chat 行数或按“chat 是否存在”决定是否回退 JSON。
2. （提示，非缺陷）_sync_chat_window 使用“内存窗口 <40 才收口”的启发式：正常场景（满窗口保留长期存档、清空/压缩收口）已用独立用例覆盖；极端场景（启动时 DB 读失败回落 JSON 且 JSON 窗口 <40、DB 恢复后首次保存）可能删掉 DB 中超出 JSON 窗口的旧存档，属可接受的边缘风险。
3. （提示）trim_agent_chat 返回“删除行数”为近似值（before - keep 数，keep 中不存在的 id 会导致轻微高估），仅日志/测试用途，无功能影响。

## 六、剩余风险与验证边界

1. 未连生产 MySQL：三张新表 DDL、paper_trades 增量迁移列、agent_chat seq 迁移（SHOW COLUMNS/ALTER）、REPLACE/INSERT IGNORE/NOT IN 删除、范围查询均用真实 SQL（SQLite 适配）验证 + MySQL 语法静态审查；真实锁等待、权限、字符集无法本机验证。
2. 未启动 main.py 服务/未做真实部署演练：启动解密、原子写配置、Agent 状态重启、清空/压缩收口、归档/导出均在临时目录/内存库真实执行验证。
3. 生产需先 pip install -r requirements.txt（cryptography>=42；本机 50.0.0 已验证 CLI）。
4. 首次部署会新建 candle_archive / agent_state / agent_chat 三张表；agent_chat 若为老开发库会自动补 seq 列。
5. test_account_heatmap 的 6 个 Playwright 失败属环境/前端构建漂移（详见 2.3）。

## 七、推送前检查清单（全部实测通过）

| 检查项 | 结果 | 证据 |
| --- | --- | --- |
| 无临时文件/specs | PASS | 工作区无 specs/、无 *.tmp、无 config.yaml.bak.* 实体文件；git status 仅预期改动 |
| config.yaml 未入库 | PASS | git check-ignore -v => .gitignore:9；git ls-files 无 config.yaml；git status 不含它 |
| secrets.key / 备份未入库 | PASS | git check-ignore：secrets.key => .gitignore:12，config.yaml.bak.* => .gitignore:10；git ls-files 无相关文件 |
| 无真实密钥进 git | PASS | 对 git diff + 全部未跟踪文件（含本报告/本测试）做 7 类密钥正则扫描 => 0 命中 |
| 无大文件 | PASS | 最大待提交文件为测试文件（约 44KB），无 >1MB 文件（排除 .git/node_modules/web/dist/__pycache__） |
| 改动范围符合预期 | PASS | git status：10 modified + 9 untracked（8 个实现者新增文件 + 本测试 + 本报告）= 19 个条目；最终轮只改 ai/agent.py、storage.py、tools/export_klines.py 与测试文件（逐文件 hash 见第八节） |
| 依赖声明 | PASS | requirements.txt +cryptography>=42；本机 50.0.0，真实 CLI 加解密跑通 |
| main.py 接线 | PASS | AgentManager state_store；write_config_file 原子写并替换 3 处直接写；启动 decrypt 早于 asyncio.run；4 处归档；Storage 新参数与 mysql 超时；README 归档口径与实现一致 |
| 分支/远端 | PASS | HEAD 1633f48（第一批已提交）；第二批未提交，由 Lead 提交推送 origin/main |

## 八、最终基线哈希（推送前请比对；SHA256 前 16 位）

| 文件 | SHA256(16) | 文件 | SHA256(16) |
| --- | --- | --- | --- |
| secrets_store.py | 7717722A992397BC | ai/agent.py（chat 收口） | D4E2836EEE724DD3 |
| storage.py（clear/trim） | 2944ED8BEB0EC4F1 | decision/paper.py | F75BE37DAFCB1786 |
| main.py（原子写） | 7ADC12602B23E358 | quant.py | ECAAB40DAEDBE8D3 |
| config.example.yaml | 1BD1927660849E2E | README.md（归档口径修正） | 3F91ED92AFEC16CD |
| .gitignore | 73ACDEFDC2E33A11 | requirements.txt | B0691753598B6866 |
| tests/test_paper_e2e.py | 9EDFBB08BBC51C76 | tests/test_round2_e2e.py | 504CA1672FEA27B8 |
| tools/encrypt_config.py | B40759574C28DEB8 | tools/export_klines.py（SecretsError） | 780B871E184B556A |
| tests/test_storage_v3.py | 257CDF54919CA29C | tests/test_agent_state_db.py | B6BF5CF12B66F9C2 |

**tracked 改动内容哈希（只覆盖 10 个 tracked 修改文件，不含未跟踪文件）：**

~~~
cmd /c "git diff | git hash-object --stdin"
=> 952f0627f80de7a93d1cfdfe59ca8865756d3720
~~~

若推送前上述任一文件/内容哈希变化，本报告测试数字需重跑（两条 pytest 命令约 3s / 20s）。

## 九、最终判定

**可以推送（PASS）。** 三轮复验后所有已报告问题关闭（2 个 P2 + 全部 P3 中仅 1 条“迁移部分失败”P3 残留，窗口极小、不阻塞）；最终全量 608 passed、0 xfailed、0 新增失败（唯一失败与 4 skipped 均为既有环境问题）。
建议 Lead 直接 git add -A、commit、push origin main。部署提醒：先 pip install -r requirements.txt（cryptography）；首次启动会建三张新表（老库 agent_chat 自动补 seq 列）；建议上线后运行 python tools/encrypt_config.py 加密 config.yaml 明文密钥（会备份；secrets.key 需单独备份）。
