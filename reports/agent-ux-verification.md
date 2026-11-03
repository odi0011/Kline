
# Agent 优化批次独立验证报告（推送前终检 · P1/P2 修复后最终复验）

- 验证者：audit-verifier（独立于全部实现者；只验证，未修改实现文件）
- 验证时间：2026-11-03（初验 → P1 SSRF/P2 只发表情包修复 → 最终复验）
- 验证对象：工作区未提交改动（HEAD = 89e6850，用户 hotfix 已推 origin/main；本批 Agent 优化改动 = 8 modified + 5 untracked：feed/search.py、3 个实现者测试、本测试、本报告）
- 环境：Windows / python 3.12.6 / pytest 9.1.1 / node + vite 6.4.5；系统代理 http://127.0.0.1:7897；所有 pytest 均加 -p no:cacheprovider
- 验证边界：未连接生产 MySQL；未启动 main.py 服务；未 commit/push；未读/改 config.yaml（含密钥）
- 本次产物：tests/test_agent_ux_e2e.py（47 passed、0 xfail）、reports/agent-ux-verification.md（本报告）

## 一、结论

**PASS —— 可以推送（0 xfail）。**

- 上轮报告的 **P1（SSRF）与 P2（只发表情包）均已修复并通过独立复验**；P3 中的 search.count 接线、timeout/proxy 配置容错、系统代理回退也已到位。
- 独立 e2e：**47 passed、0 xfailed**（原 3 个 strict xfail 已去掉并真实通过；另新增默认 count 用例与 5 个数值/编码 IP 拦截参数）。
- 全量回归：**801 passed / 1 failed（既有 test_sandbox echo 环境问题）/ 4 skipped / 0 xfailed / 51 subtests passed in 28.94s**；失败集合与基线一致，无新增回归。
- 真实安全复测：nip.io/元数据通配域名、十进制/八进制/十六进制/127.1/%2e 编码 IP、localhost/内网/元数据字面地址共 8 类，全部拒绝且**本机临时服务实际收到 0 次连接**。
- web 生产构建成功（vite 6.4.5，3827 modules，28.11s）；真实联网（经系统代理）web_search ok=duckduckgo 5 条、fetch_page example.com 正文正常。
- 剩余风险仅为“代理路径 DNS TOCTOU（已在实现注释/测试说明的残余）”与仍需人工目测的前端体验（无浏览器级自动化）；不影响本次推送。

## 二、独立证据（命令 + 数字，最终工作区实测）

### 2.1 新增独立 e2e 测试（xfail 已全部转真实通过）

~~~
python -m pytest tests/test_agent_ux_e2e.py -q -p no:cacheprovider
=> 47 passed in 5.09s
~~~

- 47 个用例独立编写：真实 AgentManager + fake provider 驱动完整工具回合；搜索安全离线（DNS/连接全 spy，绝不联网）；前端/接线源码静态断言；真实联网单独一条（经系统代理，环境波动会 skip 并保留重试记录）。
- **0 xfailed**：原 P1 SSRF-DNS、数值主机名、P2 只发表情包 3 个 strict xfail 已去掉，现全部真实通过。

### 2.2 全量回归

~~~
python -m pytest tests -q -p no:cacheprovider
    --ignore=tests/test_structure.py --ignore=tests/test_account_heatmap.py
=> 1 failed, 801 passed, 4 skipped, 51 subtests passed in 28.94s
~~~

- 唯一 failed = tests/test_sandbox.py::test_allowed_command_runs（本机无 echo 可执行文件，既有环境问题）。
- 4 skipped = test_sandbox.py 平台跳过（既有无改动）。
- 0 xfailed（本轮已无待记录的未修问题）。
- 用户 hotfix 新增的 tests/test_secrets.py::test_startup_audit_runs_on_raw_config_not_decrypted 已单独运行通过（1 passed）。

### 2.3 实现者补测

~~~
python -m pytest tests/test_search.py tests/test_agent_behavior.py -q -p no:cacheprovider
=> 135 passed in 2.89s
~~~

- 搜索安全测试已从“只断言 ok=False”改为 spy 断言零 DNS/零连接（82 passed，覆盖 SSRF 新实现）。

### 2.4 前端生产构建

~~~
cd web && npm run build
=> vite v6.4.5 building for production... 3827 modules transformed. built in 28.11s
   dist/index.html 0.95 kB / index-*.css 206.62 kB / index-*.js 1494.40 kB
~~~

- 构建成功；chunk >500KB 为既有提示；web/dist 仍被 .gitignore 忽略且未跟踪。

### 2.5 真实联网（经系统代理，与 main.py 的回退逻辑一致）

- 本机系统代理：http://127.0.0.1:7897；main.py 新增“无显式 proxy 时沿用系统代理”后：
  web_search("bitcoin price", count=5) → ok=True，provider=duckduckgo，5 条；
  fetch_page("https://example.com") → ok=True，title=Example Domain。
- 说明：本机直连（不带代理）会超时，因此 main.py 的系统代理回退是必要的；该项已接线并有静态断言。

### 2.6 真实安全复测（上轮 SSRF harness，含实际连接计数）

对以下 URL 全部 fetch_page 返回 ok=False；同时本机 127.0.0.1 临时 HTTP 服务记录 **0 次请求**：

~~~
BLOCKED http://127.0.0.1.nip.io:<port>/        （通配 DNS -> 127.0.0.1，连接前按真实 IP 拒绝）
BLOCKED http://169.254.169.254.nip.io/latest/meta-data/   （元数据通配域名）
BLOCKED http://localhost:<port>/
BLOCKED http://2130706433:<port>/   http://0177.0.0.1:<port>/
BLOCKED http://0x7f.0.0.1:<port>/   http://127.1:<port>/
BLOCKED http://127%2e0%2e0%2e1:<port>/
actual local-server hits: 0 []
~~~

- 另：_validate_public_url 对 21 个不安全样本（含全部数值/编码形式）逐一 BLOCKED；nip.io 域名在语法层按设计放行、由 DNS 解析层拒绝（分层防护）。

## 三、逐项验收（最终）

| 项 | 验收点 | 独立验证方法/结果 | 状态 |
| --- | --- | --- | --- |
| A1 | web_search 工具端到端 | fake provider required→tool→结论；tool 回包 untrusted=true、available/provider/results 完整、结果进入 messages、step succeeded、最终回复正常 | PASS |
| A2 | read_web_page 端到端与截断 | 16000 字正文 → 工具截到 12000 且 truncated=true；untrusted=true、title/url 正确 | PASS |
| A3 | 工具未注入安全降级 | search_fn/fetch_fn=None：工具执行但 available=false，不抛，最终回复正常 | PASS |
| A4 | 默认 count（P3 修复） | 模型不传 count 时使用 manager.search_default_count（main 注入 config.search.count），实测 7 生效 | PASS |
| B1 | 只发表情包（原 P2） | 只调 send_sticker、最终回复空：status=succeeded、content="[sticker:崩溃]"（2 次模型请求）；不再失败 | PASS |
| B2 | 正文末尾追加 | 「不知道，别问我。」+ send_sticker(疑问) → "不知道，别问我。\n[sticker:疑问]" | PASS |
| B3 | 重复/已有标记 | 一回合两次只认第一个（第二次 sent=false）；正文已有标记不重复追加 | PASS |
| B4 | 非法名字拒绝 | step=failed；_send_sticker 抛 ValueError、不写 pendingSticker | PASS |
| B5 | 前后端一致性 | stickers.jsx 名字/尺寸与后端 STICKER_NAMES、PNG IHDR 实测尺寸（739×415/568×488/640×452）一致；schema enum 一致；提示词列出全部名字 | PASS |
| C1 | notify 参数校验 | _notify_mode(None/空)→on_change；三模式通过；非法值 ValueError；工具与 UI create 均拒绝非法 notify | PASS |
| C2 | 通知三模式 | always/on_change 去重/never；失败/空结果不推；kind=watch、role=assistant、内容【title】结果且 ≤400 字；不触发新回复 | PASS |
| C3 | 真实 _execute | 一次性任务跑完主动推进聊天；on_change 同结果二次运行不重复推 | PASS |
| C4 | 重启持久化 | 任务 notify 与 lastNotified 经 JSON 状态重启保留 | PASS |
| D | 真实联网 | 经系统代理 web_search ok/duckduckgo/5 条；fetch_page example.com 正文正常 | PASS |
| E1 | 前端构建 | npm run build 成功 | PASS |
| E2 | 前端回归静态审查 | 贴底 rAF 合并+ResizeObserver/MutationObserver 且尊重用户上翻；入场动画只给首次出现的 assistant 分组；胶囊 key 只随 busy/idle；MessageGroup 签名覆盖全部渲染字段（无旧消息不更新）；分组(id-2)/时间分隔/未读/[sticker:] 解析/附件/拖拽/折叠均保留；贴纸 eager+真实宽高、单图 4:3 占位 | PASS（无浏览器级验证） |
| F | 全量回归 | 801 passed / 1 failed(env) / 4 skipped / 0 xfailed | PASS |
| G1 | main.py 接线（最新 HEAD 重接） | import feed.search；_search_number 容错；enabled 条件；agent.search_default_count；lambda query, count=0 + count or search_default_count；系统代理回退 getproxies()；fetch_fn 注入；feed/search.py 与本批一起提交（不会再 ModuleNotFoundError） | PASS |
| G2 | 推送前卫生 | git status 13 条目（8 modified + 5 untracked）均为预期；web/dist ignore 未跟踪；config.yaml/secrets.key ignore；密钥扫描 0 命中；无大文件；HEAD 用户 hotfix 已推 origin/main | PASS |
| G3 | storage 建表容错（已提交 83e8437） | 该提交已推 origin/main；独立复验：单条坏 DDL 只记录失败不拖垮存储层、store 仍 enabled；paper_trades 保留字列 trailing 反引号后真实 SQL 往返正确；全量无退化 | PASS（不属于本批 diff） |
| G4 | SSRF 修复复验 | 所有请求先本地 DNS 解析并逐个 IP 校验；直连固定连校验后的 IP（Host/SNI 用原域名，防 rebinding）；代理路径目标 host 强制本地解析校验；重定向每跳重新校验；8 类攻击 URL 全部拒绝且 0 连接；正则 d+ 改 \d+、host 先 unquote | PASS |
| G5 | P2 表情包修复复验 | _agent_loop 空回复分支在 pendingSticker 存在时放行；_run_chat 组装 [sticker:名字]；e2e 实测 succeeded | PASS |

## 四、提示词与“更像聊天”目标（静态）

- SYSTEM_PROMPT 保留【聊天】【搜索】【后台任务】【表情包】；搜索/网页/表情包的工具说明与不可信数据边界完整；后端不替模型决定是否搜索。
- “更像聊天/不像播报机”属主观体验，本报告只能确认提示词与行为约束到位，无法做人工观感评估。

## 五、问题状态汇总（P1/P2 已关闭，仅剩已接受的 P3/残余）

### 已修复并复验关闭

1. **P1 SSRF**：feed/search.py 现为分层防护——URL 语法/字面 IP 校验（含 unquote、\d+ 数值主机名）→
   socket.getaddrinfo 本地解析并逐个 IP 用 _ip_blocked 校验 → 直连使用校验后的 IP 建连（HTTP Host/HTTPS SNI 与证书仍用原域名，防 DNS rebinding TOCTOU）；
   搜索 provider、网页、重定向每一跳都走该链路；代理路径目标 host 强制本地解析校验（代理侧再解析的 TOCTOU 已在注释/测试中说明为已知残余）。
   独立复验：8 类攻击 URL 全部拒绝、本机服务 0 连接；实现者 tests/test_search.py 82 passed（已改为 spy 零调用断言）。
2. **P1/P2 数值主机名正则**：d+ 改 \d+，host 先 unquote；2130706433/0177.0.0.1/0x7f.0.0.1/127.1/127%2e... 全部在校验阶段拒绝且零解析/零连接。
3. **P2 只发表情包**：_agent_loop 空回复但 target.pendingSticker 存在时放行；实测只调 send_sticker、最终回复空 → succeeded、content="[sticker:崩溃]"。
4. **P3 search.count 未接线**：main.py 注入 agent.search_default_count（config.search.count 夹 1..10），_web_search 缺省使用它；e2e 验证默认 7 生效。
5. **P3 timeout/proxy 配置容错**：main.py _search_number 容错（坏类型/负数回退默认），并新增系统代理回退（无显式 proxy 时 getproxies()，本机实测搜索恢复）。

### 仍存在（不阻塞推送）

1. 正文手写 [sticker:] 标记若写在句号之后，会被短答截断丢掉；提示词已改为优先使用 send_sticker 工具，属低频边界，建议后续在裁剪后检查标记。
2. 代理场景的 DNS TOCTOU：本地解析校验后，HTTP(S) 代理会再次解析目标域名并自行连接，代理侧 DNS 不受本进程控制（实现注释已说明）；直连路径无此问题。
3. storage 的 _ddl_failures 目前只打印启动日志、未暴露 /health（该 commit 83e8437 已推送，属后续小优化）。

## 六、前端改动审阅结论（静态 + 构建，本轮前端文件未再变）

- 贴底：rAF 合并写 scrollTop + ResizeObserver/MutationObserver；followRef 在用户上翻超过阈值后不再拉回；发送消息后主动回底。
- 入场动画只给本帧首次出现且非 user 的分组；流式增长/状态更新不重放；折叠期间仍登记，展开不重放。
- 胶囊 key 只在 busy/idle 切换时重挂载，状态词变化不再触发入场 spring。
- MessageGroup 签名覆盖 id/role/status/content/error/attachments 全部渲染相关字段（不渲染 steps/progress/kind/ts），无“旧消息不更新”边界。
- 既有行为保留：id-2 分组、时间分隔、未读、[sticker:] 解析、附件图片/文件气泡、拖拽/位置持久化、折叠/展开；STICKER_SIZES 与 PNG 实测一致；单图 4:3 占位、贴纸行 min-height。
- 局限：本机 Playwright 环境与 web/dist 构建漂移导致 test_account_heatmap 既有 6 个失败（非本批文件），本轮未做真实浏览器交互回归。

## 七、剩余风险与验证边界

1. 未连生产 MySQL；未启动 main.py 服务（搜索接线用静态审查 + feed.search 单独实测，未在完整 main 进程内跑真实模型）。
2. Agent 端到端用 fake provider，不验证真实模型是否按提示词主动搜索/贴纸；真实 provider 的网络/流式差异不在本报告范围。
3. 前端只有构建 + 静态审查，无浏览器自动化（Playwright 环境不可用），建议部署后目测一次流式贴底与图片占位。
4. 真实联网依赖本机系统代理（127.0.0.1:7897）；main.py 已加系统代理回退，无代理的服务器可直连。
5. 代理路径 SSRF TOCTOU 为已记录残余；直连路径已固定校验后的 IP。
6. 搜索引擎 HTML 结构变化可能导致线上偶发回退到 Bing/报错，属外部依赖风险。

## 八、推送前检查清单

| 检查项 | 结果 | 证据 |
| --- | --- | --- |
| 工作区无临时文件 | PASS | git status 仅 8 modified + 5 untracked（feed/search.py、3 个实现者测试、本测试、本报告）；无 *.tmp/specs |
| web/dist 未被跟踪 | PASS | git check-ignore -q web/dist/index.html 通过；git status 无 dist |
| config.yaml/secrets.key | PASS | git check-ignore 分别命中 .gitignore:9 / :12；git ls-files 无 |
| 无真实密钥进 git | PASS | 对 git diff + 全部未跟踪文件 7 类密钥正则扫描 => 0 命中 |
| 无大文件 | PASS | 最大新增文件 feed/search.py 约 40KB；无 >1MB |
| main.py 接线 | PASS | 最新 HEAD 基础上重新接线：feed.search 导入、search_default_count、count=0 默认、配置容错、系统代理回退、工具未注入安全降级；feed/search.py 随本批一起提交 |
| README/config 与实现一致 | PASS | search.enabled/proxy/timeout/count 均已接线；README 与 config.example 描述一致 |
| 分支/远端 | PASS | HEAD 89e6850（用户 hotfix 已推 origin/main）；本批 13 条目未提交，由 Lead 提交推送 |

## 九、验证基线哈希（SHA256 前 16 位，便于推送前比对）

| 文件 | SHA256(16) | 文件 | SHA256(16) |
| --- | --- | --- | --- |
| README.md | 11A95C0CFF7536B1 | ai/agent.py | 0AC658479CE1EE0A |
| ai/agent_tools.py | A17E06DFE9DA31B1 | config.example.yaml | 53170B44B1EE751B |
| main.py | 6F5A70773E446EBF | chat-dock.jsx | 0D89102D6E64A3EC |
| chat-dock.module.css | 78C84DAAF26BAFF3 | stickers.jsx | 21A1C8B210356C6A |
| feed/search.py | F6C4EE5578768DE3 | tests/test_agent_behavior.py | A336269F829CDED1 |
| tests/test_search.py | 589785530E41B74D | tests/test_agent_ux_e2e.py | 85C6DF03614EFA5D |

**本批 tracked 改动内容哈希（8 个 tracked 修改文件）：**
~~~
cmd /c "git diff | git hash-object --stdin"
=> aa62ba2d18a7493783cf6108ce1620f543648511
~~~

（storage.py 的 DDL 容错已随用户要求的 hotfix 提交 83e8437 进入 origin/main，不属于本批 diff；其工作区内容 SHA256=0705FAB0BDD57E6D，已单独复验。）

## 十、最终判定

**可以推送（PASS，0 xfail）。** P1 SSRF 与 P2 只发表情包均已修复并复验关闭；search.count/配置容错/系统代理回退已接线；
全量 801 passed、0 xfailed、0 新增失败（唯一 failed 与 4 skipped 为既有环境问题）；前端构建成功、真实联网与 SSRF 攻击样本实测均符合预期。
剩余仅为已记录的 P3 边界与代理侧 TOCTOU 残余，不阻塞上线。
建议 Lead 直接 git add -A、commit、push origin main（feed/search.py 与 main.py 接线务必同批提交，避免再次 ModuleNotFoundError）。
部署提醒：本机/有系统代理的环境会自动沿用系统代理；上线后建议实测一次 web_search/read_web_page，并确认 storage 各表已正常创建（83e8437 的修复已随 HEAD 推送）。
