# 模拟仓落库与重启恢复 · 独立验证报告（最终版，第 4 轮复验）

- 验证人：paper-verifier（独立验证，未参与实现）
- 最终验证时间：2026-11-03 11:41–11:50（Asia/Shanghai）
- 最终目标工作区 diff hash：`c6f7ae2e90935b2ac92b3e262e21756a6d9f585a`（11:48 / 11:49 两次采样一致；未跟踪的 tests/reports 不计入该 hash）
- 仓库：`C:\Users\yd\Desktop\齐梓\齐梓\K线`（改动未提交，全部在工作区）
- 验证人产物：`tests/test_paper_e2e.py`（11 个 e2e；其中 F2 为实现方补的幂等用例、F3 为验证人补的跨重启去重用例）、本报告；**未改任何实现文件**
- 约束遵守：未 commit/push、未启动 main.py 服务、未读/未改 config.yaml、未连生产 MySQL

## 结论：PASS（可交付，无阻塞项）

四轮复验累计修复了 1 个 P1（丢单）、10 项 P2 与 4 项修复轮发现的残余问题；本轮 R3b / R1b / R2-m 三处收尾均**验证通过**，全量测试无回归。

剩余**非阻塞**开放项（已明确记录，非本轮目标，不影响交付）：
1. **R2c 理论碰撞**：trade_key=symbol:entryTs 在"同一毫秒内同一币开仓又被平掉重开"或系统时钟回拨时会误去重（生产概率极低；可加进程内单调序号彻底消除）。
2. **P2-5**：`drop_missing` 仍无生产调用方 → 取消监控的币未平仓行永久残留。
3. **P2-7**：停机补结算近似边界未改（开仓 bar 排除、部分覆盖、无 bars 用现价）。
4. **P2-8**：存储层仍只有 `connect_timeout=5`，无 read/write timeout，paper 写库仍在 5 秒循环内。
5. **R4 观察**：`_store_suspended` 本进程内不恢复（设计如此），建议加高等级日志/健康标记。

## 一、四轮复验历史（问题台账）

| 轮次 | 目标 hash | 发现/修复 | 结论 |
|---|---|---|---|
| 1 | `26e26369` | 发现 P1-1（流水写失败仍删未平仓行→永久丢单）+ 10 项 P2 | PASS-with-notes |
| 2 | `1a57282f` | P1-1 修复复验通过（旧行保留+pending 重试）；7 项 P2 修复；发现 R1/R2/R3 残余 | PASS-with-notes |
| 3 | `26b18935` | R2 幂等键、R1 类型校验、R3 summary 先 flush 复验；发现 R3b（锁外 flush 竞态）、R1b（数值字符串）、R2-m（迁移缺口） | PASS-with-notes |
| 4（最终） | `c6f7ae2e` | R3b/R1b/R2-m 三处收尾修复复验通过 | **PASS（可交付）** |

## 二、第 4 轮三处修复复验

### 1) R3b：summary 补 pending 改为锁内执行 —— 已修复

- 实现：`decision/paper.py:419-424` → `with self._lock: self._flush_pending()`，与 tick 共用同一把 RLock（支持重入）。
- 独立复验（真实 SQL，非 FakeStore）：造出 1 条 pending 流水后，用 Barrier 让 `summary()` 与 `tick()` 同时启动，连续 3 轮结果：
```
errors: []  trade_writes: 1  db_trades: 1  pending_left: 0   （×3 轮全部一致）
```
  修复前同样脚本会抛 `ValueError: list.remove(x): x not in list`；现在无异常、pending 只被补写一次、库里只 1 条。实现方新增的 `test_summary_flush_is_thread_safe` 连跑 5 次均通过（2 passed ×5）。
- 结论：竞态已消除；无新问题（summary 持锁做 flush，与 tick 同序，时间开销可忽略）。

### 2) R1b：数值字段强转 —— 已修复

- 实现：`decision/paper.py:174-204` 对 int_fields（entry_ts/expires_ts/leverage/ticks）与 float_fields（entry/qty/sl/tp/margin/liq/notional/sl_pct/tp_pct/atr_at_entry/mark/upnl/upnl_pct/peak）做 `_num` 强转，可选 rr/score/gap/winrate 保留 None、坏值跳过。
- 独立复验：
  - 把持久化 JSON 的 peak="101.2"、mark="100.5"、ticks="3"、entry_ts=str(...)、sl="98.5" → 恢复后类型全部为 float/int（实测 `"peak": float 101.2, "ticks": int 3, "entry_ts": int ...`），tick 正常推进不抛 TypeError；
  - tick 后写回库的 JSON 里 peak 已自愈为 float（不再是字符串）；
  - 坏值 `abc / "-5" / "xyz" / "NaN"` 所在行均被跳过（positions 为空），不会让该币模拟仓静默停摆；
  - rr/score 为 None 的合法行正常恢复（None 保留）。
- 结论：R1b 已修复，无新问题。

### 3) R2-m：老表迁移补 trade_key —— 已缓解/修复（部署注意）

- 实现：`storage.py:212-227` 迁移循环同时补 recovered 与 trade_key；补 trade_key 时追加 `ALTER TABLE paper_trades ADD UNIQUE (trade_key)`，失败不阻塞启动；新建表本来就在 CREATE 里带列与 UNIQUE。
- 独立复验（SQLite 手工造"第 1 轮老表"：无 recovered/trade_key）：
  - `_init()` 后两列都补上；实际执行的 ALTER 依次为 `ADD COLUMN recovered ...`、`ADD COLUMN trade_key VARCHAR(48) DEFAULT NULL`、`ADD UNIQUE (trade_key)`（SQLite 不支持该 ALTER，异常被按设计吞掉，不影响启动与写入）；
  - 新库（正常建表路径）`_init()` 不再尝试任何 ALTER（列已存在）。
- MySQL 语义判断（静态，未连库）：`ALTER TABLE paper_trades ADD UNIQUE (trade_key)` 是合法 MySQL 8 语法（可空唯一索引允许多个 NULL）；老表补列后所有历史行 trade_key=NULL，因此唯一索引可成功创建；**创建成功后 INSERT IGNORE 的幂等去重生效**，与新建表一致。生产主路径（新表）本来就带唯一键，无需迁移。
- 残余注意（非阻塞）：若该 ALTER 因权限/重复非空值失败，异常被吞 → 列在、唯一约束缺，`INSERT IGNORE` 不再去重（数据不会丢、可能重复）；建议把失败打一条 warning 或 health 标记。SQLite 适配环境无法 ALTER ADD UNIQUE，迁移后老表去重不生效（仅测试环境限制，与生产 MySQL 无关）。

## 三、测试数字（最终轮）

```
python -m pytest tests/test_paper_e2e.py -q -p no:cacheprovider
11 passed in 0.20s

python -m pytest tests/test_paper_e2e.py tests/test_paper_persist.py tests/test_paper_store.py \
    tests/test_paper.py tests/test_paper_context.py tests/test_paper_agent.py -q -p no:cacheprovider
81 passed in 0.76s

python -m pytest tests -q -p no:cacheprovider --ignore=tests/test_structure.py --ignore=tests/test_account_heatmap.py
1 failed, 463 passed, 3 skipped, 51 subtests passed in 12.18s
```

- 第 3 轮 461 passed → 本轮 463 passed（+2 = 实现方新增 `test_summary_flush_is_thread_safe`、`test_numeric_string_fields_are_coerced_on_restore`），无回归。
- 唯一失败仍是 `tests/test_sandbox.py::test_allowed_command_runs`：本机无 `echo.exe`（`where.exe echo` 退出码 1、复现 FileNotFoundError WinError 2），`ai/sandbox.py` 与 `tests/test_sandbox.py` 全程未修改，与本改动无关（四轮一致）。
- 实现方给出的对照数字（e2e 11 / 全量 463）与本次独立复跑一致（其"系列 94 passed"是文件口径不同，本次未采用该口径）。

## 四、e2e 覆盖（11/11 通过，全部真实 SQL，不用 FakeStore）

| # | 测试 | 覆盖 |
|---|---|---|
| A | test_open_tick_close_persists_through_real_sql | 开仓/tick UPSERT/平仓 25 字段往返/删行+新仓落库 |
| B | test_restart_restores_positions_history_stats_and_mysql_summary | 重启恢复 + 实时平仓 recovered=False |
| C | test_downtime_expired_settles_tp_sl_timeout_and_recovered_column | 停机补结算 tp/sl/timeout/同 bar 高低都碰 + recovered 列 |
| C2 | test_downtime_settle_uses_trailing_sl_and_falls_back_to_price | 移动止损 sl / 空头方向 / bars 不覆盖 / 无 bars |
| D | test_disabled_storage_degrades_to_memory | disabled Storage 降级 source=memory（含直接注入） |
| E | test_sql_write_failure_does_not_break_tick | 写失败不打断 5 秒 tick |
| F | test_failed_trade_write_keeps_old_row_then_retries | P1 修复：旧行保留 + 自动补写 |
| F2 | test_ambiguous_trade_write_retry_is_idempotent | R2：歧义失败重试只 1 条流水 |
| F3 | test_restart_settlement_does_not_duplicate_committed_trade | R2：崩溃点跨重启补结算不重复 |
| G | test_restore_read_failure_degrades_to_memory_without_overwrite | 启动读失败纯内存、不覆盖旧行 |
| H | test_failed_position_delete_does_not_duplicate_trade | 删除失败不产生重复流水 |

## 五、问题台账最终状态

| 编号 | 问题 | 最终状态 |
|---|---|---|
| P1-1 | 流水写失败仍删未平仓行 → 丢单 | ✅ 已修复（第 2 轮，pending 重试 + 旧行保留） |
| P2-1 | recovered 乱标 | ✅ 已修复（显式 recovered 参数） |
| P2-2 | 坏 JSON 类型不校验 | ✅ 已修复（第 4 轮强转收尾，R1b） |
| P2-3 | 删除失败 → 重复流水 | ✅ 已修复（pending_deletes + trade_key 幂等） |
| P2-4 | 恢复读失败覆盖旧仓 | ✅ 已修复（_store_suspended） |
| P2-5 | drop_missing 无生产调用方 | ⏳ 开放（非阻塞） |
| P2-6 | disabled Storage 误报 mysql | ✅ 已修复 |
| P2-7 | 补结算近似边界 | ⏳ 开放（设计取舍，非阻塞） |
| P2-8 | 无 read/write timeout | ⏳ 开放（非阻塞，建议上线前排期） |
| P2-9 | Agent 无条件 MySQL 口径 | ✅ 已修复（条件口径） |
| P2-10 | 同毫秒排序不稳定 | ✅ 已修复（ORDER BY closed_ts DESC, id DESC） |
| R1 | 坏类型行静默停摆 | ✅ 已修复（必填+运行字段校验） |
| R1b | 数值字符串穿透 | ✅ 已修复（第 4 轮强转） |
| R2 | 歧义失败重复流水 | ✅ 已修复（trade_key + INSERT IGNORE；F2/F3/store 测试） |
| R2-m | 老表无 trade_key 迁移 | ✅ 已修复（迁移补列+尝试 UNIQUE；见部署注意） |
| R2c | 同 key 理论碰撞 | ⏳ 开放（理论风险，非阻塞） |
| R3 | summary/DB 统计短暂不一致 | ✅ 已修复（先 flush） |
| R3b | 锁外 flush 并发 ValueError | ✅ 已修复（第 4 轮，锁内 flush） |
| R4 | suspended 进程内不恢复 | 📝 观察（设计如此，建议可观测性） |

## 六、MySQL 静态审查（四轮累计，仍未连接真实 MySQL）

- 新表 DDL：`paper_positions(symbol PK, ts, data MEDIUMTEXT)`；`paper_trades` 含 `trade_key VARCHAR(48) DEFAULT NULL + UNIQUE (trade_key)`、`recovered TINYINT`、25 个业务列与两个普通索引。全部为合法 MySQL 8 语法（可空唯一索引允许多个 NULL；VARCHAR(48) utf8mb4 索引长度 192B）。
- `save_paper_trade` 为 `INSERT IGNORE`，**26 列 = 26 占位符 = 26 实参**，列序一致；INSERT IGNORE 在唯一键冲突时忽略重放，实现 at-least-once → 幂等。
- 迁移：`SHOW COLUMNS ... LIKE` + 条件 `ALTER ADD COLUMN`（recovered / trade_key）+ 尝试 `ADD UNIQUE`，异常不阻塞启动。
- 查询：`ORDER BY closed_ts DESC, id DESC ` 稳定排序；`SUM(CASE)`/`GROUP BY` 合法；LIMIT 参数已夹取。
- 仍**未连接真实 MySQL**执行：生产/dev 首次上线时建议在隔离库跑一次建表+迁移+开平仓确认（尤其 `ADD UNIQUE` 权限与既有数据）。

## 七、未覆盖风险（交付后需知）

1. 真实 MySQL 未连接：唯一索引/迁移/INSERT IGNORE 的真实行为为静态审查 + SQLite 适配验证。
2. 真实行情 + 真实进程 kill/重启未演练；停机补结算的 bars 覆盖回落见 P2-7。
3. 多实例并发写同一账本未验证（唯一键防流水重复，但未平仓行 REPLACE 仍会互相覆盖，部署需单实例）。
4. 真实网络/磁盘故障未演练；P2-8 无读写超时会放大 DB 半死时的阻塞。
5. config.yaml 未读取；未启动 main.py；未构建前端。

## 八、交付建议（全部非阻塞）

1. 上线前：在隔离 MySQL 验证建表/迁移/UNIQUE 索引/INSERT IGNORE；建议给 `ADD UNIQUE` 失败加 warning。
2. 排期：P2-8（pymysql read/write timeout）> P2-5（drop_missing 接线）> P2-7（补结算近似来源）> R2c（如需绝对严格，加单调序号）。
3. 可观测性：暴露 pending 流水/删除数量、suspended 状态、INSERT IGNORE 命中数。

## 附：最终复跑命令

```
python -m pytest tests/test_paper_e2e.py -q -p no:cacheprovider
python -m pytest tests/test_paper_e2e.py tests/test_paper_persist.py tests/test_paper_store.py tests/test_paper.py tests/test_paper_context.py tests/test_paper_agent.py -q -p no:cacheprovider
python -m pytest tests -q -p no:cacheprovider --ignore=tests/test_structure.py --ignore=tests/test_account_heatmap.py
```
