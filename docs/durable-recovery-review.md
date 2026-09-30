# 0.2.2 持久化、真实恢复与 Dynamic MCP 实施记录

本记录对应云端分支 `fix/durable-recovery-lifecycle`，审查基线与起始 HEAD 均为 `027bdafc0cd32f5d1a04c84a6711e8e039391d40`。起始 `git status --short` 为空，`git diff --stat <基线>` 为空；没有覆盖用户已有改动。仓库、`/workspace` 及其 `.agents` 未发现 AGENTS.md 或相关 `.agents/skills`。工作仅在云端仓库及受控临时目录进行；无 push、PR、合并、部署、用户主机操作、认证/权限配置改写或模型 RPC。

速度：收到切换 Fast 指令后核查当前工具，未发现受支持的任务速度修改入口，没有修改全局配置或新建任务，也没有声称切换成功。模型/推理设置沿用委托；不能通过本仓库变更证明平台侧服务速度。

## 修改合同与证据

### 1. 幂等结果持久化

- 基线文件/函数：`idempotency.py:Idempotency.__init__/reserve/finish`；`bridge.py:Bridge.execute`。
- 触发证据：基线 calls 表只有 key_hash、args_hash、state、created_at。finish 只写 complete，结果在至多 256 项的 cache。基线 test_unit 在 close/reopen 后明确预期 execution_state_unknown。
- 预期：同 key/同参数在重开或淘汰后从磁盘恢复独立 JSON 副本；改参数冲突。started、缺 receipt、损坏、未知版本不能执行第二次。非目标：保证“未知执行其实没有副作用”、恢复旧进程或自动重试。
- 实现/错误：事务内检查表结构，增量 ADD receipt；BEGIN IMMEDIATE 原子预留，主键排斥不同连接的竞争者。版本 1 receipt 与 complete 单 UPDATE/事务提交；JSON 使用 Unicode，禁止 NaN。非法 receipt 返回 execution_state_unknown。事务失败回滚，Bridge 返回第一次真实结果并附 degraded diagnostics，重复 key 仍被 started 阻断。operation_id 原样存储，重放产生新的 operation_id 与 replayed_operation_id。
- 迁移/回退：旧 calls 表原位加可空列；旧完成行没有 receipt 时不重执行。旧代码能忽略新增列，但不能提供新磁盘重放能力。回退必须保留当前数据库及 started 行，不能删除库或恢复过旧库后重发命令。
- 测试输入/步骤/预期：258 个 receipt 淘汰首项、Unicode/嵌套结果副本修改、两个连接 Barrier 同 key、旧四列表手工建库、started/无 receipt/破损 JSON/version=99/数组结果、SQLite trigger RAISE ABORT、关闭后重开 Bridge。分别验证持久化结果相等、副本独立、冲突/未知错误、后端次数最多一次、state/receipt 同时回滚、首次真实响应不丢。
- 具体用例：`test_receipt_disk_eviction_independent_unicode`、`test_reserve_two_connections_at_most_once`、`test_legacy_corrupt_unknown_never_reexecutes`、`test_finish_failure_rolls_back_state_and_receipt`、`test_late_idempotency_failure_keeps_true_response`、`test_bridge_disk_replay_preserves_origin_after_reopen`。

### 2. 会话输出与真实恢复

- 基线：`sessions.py:Session.append/finish/read/metadata`、`SessionStore.__init__/create/get/notify/save`，`bridge.py:_session_start/_do/close`。
- 触发：基线 JSON 仅存 metadata，重启后的记录只在 previous 列表、get 不可读；running 变 lost 后旧 exit_code 没有强制 null。UTF-8 decoder 尾字节只在内存。
- 预期：每次输出增量持续落盘，重开能读已完成和 lost 的已存输出；cursor 单调、oldest_cursor、cursor_gap、截断和字节/事件上限保持。终态/退出码与最终输出同事务。非目标：恢复命令、重新附着进程、宣称跨重启持有 PTY。
- 实现：`sessions.sqlite3` 中版本 2 metadata 与 `session_events(session_id,cursor,event)`；只写新增事件，在同事务删除环形过期事件，终态和 decoder 尾文本一起提交。限制 capacity 和 8192 events；会话历史按 max_sessions 修剪并 cascade 删除输出。版本 1 快照可增量迁移。每条数据写入在 Session.lock 后拿 db_lock；列表/获取先释放 store.lock，再取 Session.lock，避免反向锁顺序。
- 错误：输出写入失败保留 live 内存与真实结果，metadata 报 persistence_error；磁盘保留上次一致快照，重开只能看 last durable output，running 快照变 lost。创建落盘失败不能派发。finish 在落盘尝试后置 finished，保证正常完成的读者可看到终态与尾输出。尾字节重启按 replace 展示且只 flush 一次。
- 归档：所有重开对象 archived=true；lost exit_code=null、reconnectable=false、不接管进程。归档 write/kill/resize 在 ensure_ready 前拒绝。runtime generation 校验仍保留。完整历史缺失的旧 JSON 输出显式 unavailable。
- 迁移：以 migrations 主键确保 legacy JSON 一次性幂等导入；原文件保持原样。invalid JSON/未知 SQLite snapshot 不静默清空。回退代码可使用保留 JSON，但看不到新输出；保留 SQLite，不能把 JSON 当作可重执行脚本。
- 测试：stdout 中文/emoji 分片、stderr 不完整字节、非零退出码、未调用 save 的持续落盘重开、4096→1024 环形淘汰、旧 JSON cursor=20、重开 unavailable、trigger 写失败、最终尾事件触发事务失败后 metadata 仍 running/exit=null、归档控制拒绝且不启动 runtime。
- 初次发现/修正：重开遍历 SQLite 游标时回写同表会反复读新行；测试挂起后保留诊断并终止测试进程，改为 fetchall 固定快照，再回写。没有重发实际业务命令。

### 3. 任务关联与数据库 revision

- 基线：`task_runtime.py:_save/checkpoint/resume/complete/manage`，`tools.py:EXEC/task_manage/mcp_tool_call`，`bridge.py:execute/_rpc/_session_start`，`common.py:CURRENT_TASK_EXECUTION`。
- 触发：基线 _save 只在内存比较 expected_revision，随后无条件 UPDATE，独立连接可丢更新；checkpoint 不使旧 final_review 失效；任务没有 execution/session/runtime/cursor 关联。
- 实现：新增 executions 表，保存 task_id、step_id、operation_id、tool、state、session_id、runtime_generation、cursor。Bridge 在 _do 前持久化关联，官方 RPC 与流式 session 在 backend begin 前写 generation/session；Dynamic MCP 在 call dispatch 前写服务 generation。关联失败不派发。表中不保存原始 command/env，因此不是自动恢复脚本。
- CAS：UPDATE tasks 使用数据库 json_extract(doc,'$.revision') 比较旧 revision，rowcount!=1 返回 task_revision_conflict；关联/任务 revision 的变化在一个事务内提交。checkpoint、新执行、resume、核验均使旧复核失效。旧 tasks 文档不需格式重写，新增表原位创建。
- recover：task_manage 的 recover/list/get 按只读风险处理；recover 返回任务检查点、关联、已存会话输出、未知终态 operation IDs 与“核验外部效果→resolve_execution→checkpoint→final_review”下一步。不调用 backend，不重放 command。归档 lost 或无法读取会话即 unknown。
- 阻碍：关联 started/running/unknown 或未知状态阻止 resume/complete。旧无关联任务仍可 resume。resolve_execution 需明确 operation ID、verified_completed/verified_failed、summary 和非空 evidence；不能把 live session 伪装为终态。修改 revision 事务失败会同时回滚核验。
- 测试：写 executions 的 trigger 故障，断言 _do 次数为 0；两连接同时加载 revision=1，只有一个 checkpoint 成功；checkpoint 后旧 pass 不能 complete；旧表无 executions 原位迁移；completed 不能 resume；lost 输出返回、resume 被阻断、核验证据后才允许继续；磁盘 receipt 重放不会增加关联或派发。
- 非目标/回退：不为业务效果自动判定成功，不自动安排模型核验。旧代码不认识 executions，回退后不得对有关联未知执行的任务直接 resume；保留库，先读取本版 recover 报告并核验。

### 4. Dynamic MCP 生命周期和分页

- 基线：`dynamic_mcp.py:_with_session/_run/refresh/manage/_cached_tools/call`，`bridge.py:__init__/close`。
- 触发：每次 asyncio.run 新建 transport/session；stdio 重启丢服务状态；list_tools 只取第一页；空列表每次重复 refresh。
- 实现：一个懒启动的专属事件循环线程，每 server/generation 一个 owner task；同一个 task 进入/退出 httpx、transport 与 ClientSession 上下文。每 owner 串行队列最多 64 项；queue/init/operation 都受 timeout_ms 限制，同步包装额外 5 秒等待边界。初始化 deadline 会取消尚未初始化 owner。server 独立，更新 generation 采用运行期单调计数，remove/register 不复用旧 generation。
- 失效/关闭：注册表只在 RLock 内修改，transport I/O 和 cleanup 在锁外进行。update/disable/remove 清理过期 owner；竞态中保留当前有效 generation。Bridge.close 调用 manager.close；重复关闭可确认线程退出，未完成 cleanup 返回 mcp_cleanup_timeout，不伪称释放。
- 不重试：call dispatch 后超时/断线返回 execution_state_unknown；call_tool 不自动重试。排队过期的请求绝不执行；派发前关联写失败也不执行。下次明确调用可建立新连接，但不是自动重放旧调用。
- 分页：最多 128 页/10000 工具，检测重复 cursor、重复工具名；只有完整列表成功且 generation 没变才替换缓存。tools_loaded 标记可表达成功空列表。坏分页保留旧工具；schema 参数校验和 secret key-only 公开输出保持。
- 测试：真实受控 stdio 计数器 refresh/call/call/refresh/call 为 1/2/3；真实 localhost HTTP FastMCP 的同一个 MCP session 计数为 1/2/3；fixture 确认 transport 与 session 退出 task 和进入 task 相同；末页调用、cursor loop、duplicate name、页数/数量上限、空列表只读取一次、dispatch 后 hang 只执行一次、初始化 hang 清理、66 请求中两个 resource_limit 且零派发、update/refresh 竞态、remove/register、独立服务、重复 close。
- 回退：registry JSON 保持原字段，generation/tools_loaded 是增量字段。旧版可读，但每次调用重建连接；回退前必须关闭本版 manager，不能留下旧 stdio 进程。不会复制业务状态或自动重放工具。

### 5. CUA 兼容层

- 基线：`computer.py:OfficialComputer._run/_entry/__init__`，`config.py:Config`。
- 触发：按 .mcp.json mtime 选择最新插件目录，只检查 js 名称；未知内部布局会被直接尝试启动。
- 实现：`select_manifest` 只接受 cua_compatibility.version/manifest_sha256 精确匹配；拒绝路径逃逸、缺文件、坏 JSON、坏 env 结构和 hash 不同，无 fallback。probe_tool_contract 检查 js 参数 code/title/timeout_ms 的 JSON Schema；初始化执行仅输出 API shape 的只读 js probe，不访问桌面，不调用模型。BridgeError 的 version_incompatible 与可解释原因保留，不被通用 startup error 吞掉。
- 证据边界：仓库没有已核验真实版本/fingerprint，云端无官方 Windows 插件。不能编造 allowlist；操作者必须依据已有实机验证证据填写选择。缺选择时官方 CUA 明确不可用。现用 Tabbit backend、应用访问确认与权限政策未修改。
- 测试：已核验旧 fixture 与更“新”坏 fixture 并存，仍只取明确旧版本；hash 变更、缺文件、路径逃逸、缺 js 参数/错误类型返回 version_incompatible。没有声称 Windows GUI 或实际官方插件兼容性通过。
- 回退：撤回代码可恢复旧选择方式，但会失去未知版本拒绝机制；本次没有写任何运行配置，因此无需回滚认证/授权配置。

### 6. POSIX、版本与发布证据

- 基线：`bridge.py:_argv/_read_bytes/_do(file_add/file_move)/health`，README 标题 0.2.0、pyproject/__version__ 为 0.2.1。
- 实现：Windows 默认 shell 保持 PowerShell，POSIX 默认 /bin/sh，显式支持 sh/bash。路径以结构化 argv 交给 Python3 标准库的文件大小/无覆盖移动辅助程序；不把路径拼进 shell，不在本机替代官方 RPC。健康探针选择对应平台语法。POSIX 移动 link+unlink 仅同文件系统、文件支持；unlink 故障可能留下双路径，保留事实并要求核验。
- 版本：README、pyproject、__version__ 统一 0.2.2；README 新增真实能力矩阵、恢复/不可重执行边界、CUA 选择说明、账单不能等同零扣额、可复现命令；旧生产记录为历史证据。
- CI：新增 Ubuntu/Windows、Python 3.11/3.12 的源码 compile 与非 integration 回归，15 分钟 job deadline，测试结果 artifact；无新凭据。CI 尚未远端运行，不能当作 Windows 实测。
- 平台测试：39 个旧失败中，36 个在非 Windows 新增平台跳过，两个真实官方 Runtime 的 lifecycle/OAuth transport 用例新增 integration 标记而 deselected，1 个 WSL fixture 修正后通过。Windows 非 integration CI 会执行上述 36 个，但仍排除两个 integration 案例。两个 controller 模块在导入 msvcrt 前新增平台跳过。最终 60 skipped=22 个基线已有+2 个新模块级+36 个新用例级；20 deselected=18 个原有+2 个新增。测试函数未删除；跳过与排除都不算验证通过。工具数用当前 TOOL_SPECS 验证。WSL 纯单测隔离 NO_PROXY/no_proxy 环境，修正受云端既有环境污染的断言，不改任何代理配置。
- 非目标：不改 DPAPI/权限分层，不为云端测试用明文 token 存储，不安装官方 runtime 到用户机器，不操作 SSH/Docker/Windows GUI 实机。

## 实际命令、结果与通过标准

工具首次运行系统 Python 发现没有 pytest；在 `/workspace/ccm-venv` 建独立 venv，安装项目 `.[dev]`。没有修改全局 Python 或用户认证配置。

```bash
python -m venv /workspace/ccm-venv
/workspace/ccm-venv/bin/python -m pip install -e '.[dev]'
/workspace/ccm-venv/bin/python -m pytest tests/test_durable_recovery.py tests/test_unit.py tests/test_call_diagnostics.py tests/test_infrastructure_extensions.py -q --tb=short
/workspace/ccm-venv/bin/python -m pytest tests/test_dynamic_lifecycle.py -q --tb=short -o faulthandler_timeout=20
/workspace/ccm-venv/bin/python -m pytest tests -q -m 'not integration' --tb=short -o faulthandler_timeout=60 --junitxml=evidence/durable-recovery/full.xml
# 在独立 detached 基线 /workspace/ccm-baseline（同一依赖环境）
/workspace/ccm-venv/bin/python -m pytest tests -q -m 'not integration' --ignore=tests/test_core_recovery_controller.py --ignore=tests/test_recovery_lease_races.py --tb=short --junitxml=evidence/durable-recovery/baseline.xml
```

初期持久化相关回归 51 passed / 1 failed（旧 sh schema 断言）；更新支持 shell 合同后 57 passed。Dynamic 初始 10 passed。增量输出完善后针对性组合 59 passed。原全量 Linux 274 passed / 39 failed / 24 skipped / 18 deselected；基线 239 passed / 同样 39 failed / 22 skipped / 18 deselected。两者 JUnit 的失败集合完全相同：introduced_failures=0，见 baseline-comparison.json。失败为 Windows kernel/DPAPI 依赖与环境污染，未把失败伪称通过；原始 full/baseline 字节日志保留在交付 raw-evidence；Git 跟踪副本只清理行尾空白，原件 SHA-256 记录于 verification.json。标明平台依赖后完整可运行回归 275 passed / 62 skipped / 18 deselected。新 HTTP fixture 初次错误假定 structuredContent，实际 SDK 返回 text JSON；修正读取 fixture 响应后真实 loopback 单项通过。

最终检查结果：新增验收 **45 passed**；完整可运行回归 **285 passed / 60 skipped / 20 deselected**，exit=0（headless wrapper，约 8 秒）。源码 compileall、依赖 pip check、wheel 构建及 diff --check 均通过。真实 stdio fixture 在 disable 后检查 owned PID 已不存在，事件循环线程已退出。Linux 的 headless_runner=false 表示未验证 Windows CREATE_NO_WINDOW，不能解释为 Windows 无控制台窗口已通过。

最终检查使用本节后续 evidence `acceptance-final.log`、`final-headless.log`、`final.xml` 与 `verification.json`，通过标准为新增验收全部成功、完整可运行回归 exit=0、compileall exit=0、git diff --check exit=0；跳过不算验证通过。日志与完整 patch 在云端交付目录 `/workspace/ccm-delivery`；仓库内原始 evidence 默认被 .gitignore 排除，本次仅对明确的受控测试日志强制纳入版本。

## 未验证与父任务决策事项

1. 真实 Windows 官方 Codex/PTY/fs/Git、CUA GUI、Tabbit 实机、Windows DPAPI/stop event/Job Object、macOS、SSH、Docker 未验证。CI 尚未运行。
2. 没有可核验的真实 CUA manifest fingerprint，不能在此环境给出已验证版本清单；本次不读取用户主机获取指纹。
3. 基线 freeze/host 固定版本 0.2.0 已按父任务授权增量修复，详见后面的 LKG 兼容补充；没有运行真实激活。真实 Windows 全栈证据仍缺失，full_stack_accepted=false，正式激活仍被证据门槛阻止。
4. Fast 没有受支持的当前任务设置入口，未切换或冒称切换。普通执行不增加模型回合不代表官方账单零扣额。

## 回退与交付

回退源码用本次单独提交的 `git revert <提交>`，或在独立检出回到基线；不得清空状态数据库来“重新开始”。本次没有认证、权限或生产配置改动。保存整个 state 目录（含 SQLite/WAL/SHM，确保关闭本版连接后备份）；旧 JSON 保留。新 SQLite 输出/关联不被旧版识别，需用本版只读 recover 导出并核验未知执行后才能在旧版继续任务。停止本版动态 MCP owner 后再换代码，不能把 transport 断线误当作业务未执行。完整 diff/提交补丁与 SHA-256 随交付目录提供，未 push/PR/合并/部署。


最终实际命令补充：

```bash
/workspace/ccm-venv/bin/python scripts/run_headless_tests.py tests -q -m 'not integration' --tb=short -o faulthandler_timeout=60 --junitxml=evidence/durable-recovery/final.xml
/workspace/ccm-venv/bin/python -m pytest tests/test_durable_recovery.py tests/test_dynamic_lifecycle.py tests/test_cua_compatibility_posix.py -q --tb=short --junitxml=evidence/durable-recovery/acceptance-final.xml
/workspace/ccm-venv/bin/python -m compileall -q src
/workspace/ccm-venv/bin/python -m pip check
/workspace/ccm-venv/bin/python -m pip wheel --no-deps . -w /workspace/ccm-delivery
git diff --check
```

最后自查还修复了三个边界：注册表快照在锁外串行落盘；多个排队超时只取消 owner 一次，防止重复 cancel 打断清理；SessionStore 不淘汰尚未完成持久化的终态对象。已知终态或人工核验后的 execution 在对应有界 session 历史被淘汰时保持原终态，单独标注输出 unavailable，不把已确认结果退化为未知；未确认执行仍保持未知阻碍。会话启动记录失败时保留原始执行异常，不让保存错误替换它。相关实现之后重新运行了上面的最终全部检查。


## LKG 版本兼容增量收尾

- 增量基线：上一实现提交 `454da7ca85b9e48fc0a03dfdcbf84fe6c4912837`。审查原基线 freeze 在 `scripts/freeze_source_lkg.py:32` 固定 0.2.0/47，在 45/53/56/59 固定候选、manifest、目标和备份目录；host 在 `Core-Source-Host.ps1:10` 固定旧目录。0.2.2 probe 因版本失败；旧版 activate 后 probe 失败没有回滚，只有 rename 异常回滚。
- 修改文件/函数：`freeze_source_lkg.py:freeze/main`；新增 `lkg_state.py:probe/validate_snapshot/snapshot_record/resolve_selection/selected_config/atomic_bytes/maintenance_guard/activate`；`Core-Source-Host.ps1` 的 lkg 选择；`Install-Core-RecoveryTasks.ps1` 的候选配置；`core_recovery_controller.py:run` 的选定配置读取；CI compile 增加 scripts。
- 预期与非目标：支持不同版本/工具数的不可变源码快照，保留旧 `lkg-0.2.0` 入口。激活只改来源选择，不启动服务、安装任务、变更权限认证、清空数据库或重放命令。不宣称 Windows/PowerShell/调度实机已验证。
- 实现：pyproject 与隔离导入包版本一致，工具数由 probe 实测；manifest schema=2 绑定源码逐文件散列/总散列、版本、工具数和原始验收。目录名绑定版本、源码散列与 manifest 散列；新验收建立新目录，不改写旧快照。active schema=1 指定 snapshot、版本、工具数、源码散列、manifest 散列；指纹不符、路径越界、symlink 或未知 schema 拒绝。没有 active 时保留旧 0.2.0/47 行为。
- 配置一致性：active 是唯一原子来源/配置选择，原 controller JSON 不需要双写。host 解析同一记录；controller 获得既有 maintenance guard 后读取，用它仅覆盖 lkg_direct 的版本/工具数；formal/current 使用各自版本/工具数，installer 的 formal 期望仍显式参数化，current 隔离 probe 读取。
- 错误/回退：共用永久 maintenance.lock.guard；已有 lease 拒绝激活，不删除 guard。前后隔离 probe，原子临时文件 flush/fsync/replace，POSIX 目录 fsync。异常恢复 previous 的精确字节或无 active 状态；覆盖写之前保存 rollback 记录。写入在 replace 后再报错也走回滚。回滚失败明确报错并保留 previous；存在未解决 rollback 记录时 host/controller 拒绝选定 LKG，须维护锁内人工检查与恢复，不能自动忽略。旧目录、静态 controller 配置始终不改写。
- 门槛：输入与快照原始证据的 all_passed/full_stack_accepted 均须 true 才能真实激活。本次交付 full_stack_accepted=false。受控 fault fixture 的 synthetic true 只验证代码门槛，不是实机验收；测试临时目录不会接触实际 home/state。
- 测试步骤/预期：临时构造 0.2.0/47、0.2.1/46、0.2.2/3 包并真正执行隔离导入；前/后 probe 故障分别检查 previous 存在/不存在恢复；备份/active 写入前及 replace 后故障检查原字节；copy/rename/probe 中途故障检查旧 LKG sentinel；篡改源码、manifest、active 字段或路径拒绝；版本/工具数 probe 不符拒绝；缺全栈证据在 probe 前拒绝；相同源码不同验收创建不同快照；回滚失败保留备份并阻止读取；共享 guard/既有 lease 阻止切换；当前仓库仅 freeze 到临时 home，证据 false，无 active 文件。以上 27 项无新增 skip。
- 首次完整运行在新测试导入 scripts 时 collection error；headless wrapper 不把仓库根加入 sys.path。改为按确切脚本路径 importlib 加载，controller 也按相邻 helper 路径加载；未修改平台跳过规则，未删除测试。失败原始 log/XML 保留于交付 raw-evidence/lkg-collection-failure.*。
- 最终命令：`python -m pytest tests/test_lkg_compatibility.py -q --tb=short --junitxml=evidence/durable-recovery/lkg-acceptance.xml`，27 passed；`python scripts/run_headless_tests.py tests -q -m 'not integration' --tb=short -o faulthandler_timeout=60 --junitxml=evidence/durable-recovery/lkg-final.xml`，312 passed / 60 skipped / 20 deselected、exit 0。`python -m compileall -q src scripts`、`python -m pip check`、`git diff --check` 通过。最终自查补充了 replace 后持久化报错的回滚，两项新故障测试后重新完成上述回归。
- 选择范围：39 旧失败仍为 36 skip + 2 deselect + 1 pass，没有把 skip 当修复。本轮仅新增 27 个可移植测试，没有新增 skip/deselect。CI 原有 -k 另外排除 WSL availability 测试；远端 CI 未运行。Windows PowerShell/调度器、真正的 maintenance guard 集成与服务激活、突然进程死亡/电源中断未实测，原 Windows/CUA/SSH/Docker 等未验证项继续保留。
- 迁移与源码回退：无需数据迁移，旧目录不变，无 active 即旧行为。若将来真实激活后回退 host 代码，须先在维护锁内恢复上一 active 选择/配置，并确保回退代码对应旧 lkg 路径；本次没有真实选择变更，无需操作用户状态。源码可 revert 此增量提交，保留已有持久化库和未知执行阻碍。
