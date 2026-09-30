# Codex-Control-MCP 0.2.2

Codex-Control-MCP 是本机/远端执行基础设施 MCP。它通过已安装的官方 Codex Runtime 提供 Shell、文件、Git、长任务会话、Browser、Computer Use，以及 0.2.0 新增的可恢复任务、多主机路由、动态 MCP 和 Skill 生命周期管理。


## 0.2.2 持久化与恢复候选（尚未发布部署）

- 幂等调用的版本化 JSON receipt 与 complete 状态在一个 SQLite 事务提交；重启、缓存淘汰后可读回独立副本。started、旧版无 receipt、损坏或未知版本结果均拒绝自动重执行。第一次真实响应不会被晚期落盘失败替换。
- 会话输出增量写入 `state/sessions.sqlite3`，与最终退出码/状态原子提交，受字节与事件数量限制。重启可读已存输出；原 running 会话成为 lost、退出码为 null、不可重连或控制，不接管原进程。旧 `sessions.json` 一次性导入，原文件保留；未记录的输出标记 unavailable。
- `exec_command`、`session_start`、`host_exec`、`mcp_tool_call` 可传 `task_id/step_id`。关联在派发前落盘；`task_manage(action="recover")` 是只读视图，不执行命令。unknown/lost 执行阻止 resume/complete；人工核验后用 `resolve_execution` 提交 operation ID、核验状态、summary 和 evidence。检查点和新执行使旧 final_review 失效。
- Dynamic MCP 每个服务使用独立 owner task 和有界串行队列，在专属事件循环复用 stdio/HTTP 会话；完整分页成功后才替换 schema。超时或断线发生在调用后时返回结果未知，不自动重试。update/disable/remove 使旧 generation 失效，Bridge.close 关闭进程和线程。
- 官方 CUA 插件要求明确的已核验版本与 manifest SHA-256，按配置匹配后进行只读契约探测，拒绝未知新版。仓库没有可作为真实 Windows 验收依据的指纹；本次仅验证受控 fixture。Tabbit 后端和既有应用访问授权策略保持原样。

| 能力 | Linux 云端本次证据 | Windows / macOS 本次证据 |
| --- | --- | --- |
| SQLite 幂等、归档输出、任务 CAS/recover | 单测与故障/竞争 fixture 通过 | hosted Windows CI 已运行；本轮修复待增量 CI 核验；用户实机未验 |
| Dynamic MCP stdio / HTTP | 真实受控 stdio 与 loopback HTTP 的状态复用测试通过 | 未实机验证 |
| POSIX Shell/文件辅助操作 | 实际 sh、Unicode、结构化 argv、不覆盖移动通过；需 python3 | Windows 分支保留；macOS 未实机验证 |
| 官方 Codex Shell/PTY/fs/Git | 没有真实官方 Runtime 全链路验收 | 未验证 |
| 官方 CUA / Tabbit GUI | 契约 fixture；无真实 GUI 验收 | 未验证 |
| OAuth DPAPI / named event / Job Object | 非 Windows 明确跳过 | hosted Windows 原测试已执行；用户实机未验 |
| SSH / Docker 远端路由 | 仅已有路由 fixture | 未实机验证 |

普通执行路径不新增模型 RPC 回合；这不等于官方账单或订阅额度“零扣额”，账单归属仍需产品侧证据。第三方 MCP 自身的模型调用/计费不由本桥承诺。

可复现的云端检查（Python 3.11+）：

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m compileall -q src
.venv/bin/python -m pytest tests -q -m 'not integration' --tb=short -o faulthandler_timeout=60
```

Windows 使用 `.venv\Scripts\python.exe`。标记 integration 的测试需明确具备官方 Codex；Windows OS/DPAPI 测试在非 Windows 环境标记跳过，不用明文或 mock 加密代替。CI 仅运行上述可运行范围，没有新建凭据。五项官方 Runtime 用例的前提、受控 fixture 边界及可执行的独立验收 job 见 [CI Runtime 分类](docs/ci-runtime-classification.md)。普通 CI 通过不代表这五项或 Windows 全栈通过；独立 job 本次未触发。完整修改合同、命令日志、基线失败对照和回退方式见 [持久化与恢复实施记录](docs/durable-recovery-review.md)。

官方 CUA 的选择配置需要由拥有实机验证证据的操作者填写（本次没有写入任何运行配置）：

```toml
[cua_compatibility]
version = "已核验的插件目录版本"
manifest_sha256 = "已核验的 .mcp.json 原始字节 SHA-256"
```

POSIX 文件移动使用拒绝覆盖的 link 后 unlink；只支持可硬链接的同一文件系统，跨文件系统失败时保留源文件。断电/失败可能留下源与目标同时存在，应核验后手动处理，不自动重试。状态数据库回退前必须保存当前库，不能清空幂等 started 行或恢复过旧库后重复命令。

## 一句话定位

> **把支持 MCP 的网页 AI 直接变成跨设备执行中枢：人在手机上发出自然语言指令，Codex-Control-MCP 负责把旗舰模型的决策落到已接入的电脑、服务器、浏览器、文件、Git、长任务和远程主机上。**

这不是另一个内置模型的 Agent，也不是重新做一套 Codex。它的价值在于把“网页 AI 的智能”与“真实设备的执行能力”直接连起来，让一部手机就能成为现代 AI 办公的统一入口。

## 最佳使用场景

### 1. 一部手机，统一控制已接入的设备

手机上的 ChatGPT 或其他支持标准 MCP 的客户端可以作为统一控制台。你可以在移动端发一句话，让 Codex-Control-MCP 把任务分发到已经接入的 Windows / Linux / macOS 主机、远程 MCP 节点、SSH 主机或 Docker 容器，并继续操作文件、Shell、Git、浏览器和 Computer Use。

典型场景：

- 人在外面，只拿手机，要求办公室电脑修改项目、运行测试、提交 Git、检查网页结果。
- 一条任务同时协调本地工作站、远程服务器和容器，各自完成最适合自己的步骤。
- 长任务通过可恢复任务状态持续记录进度，换设备或换会话后仍能继续，而不是把整条工作流绑死在某个前端页面上。

### 2. 让网页版旗舰模型直接控制电脑

Codex-Control-MCP 把模型和执行层解耦。只要网页端 AI / 客户端能够连接标准 MCP，它就可以把自身的推理、规划和决策转换成真实设备操作，而无需把模型重新封装进本项目。

以 ChatGPT 网页版为例：模型仍运行在 ChatGPT 本身，Codex-Control-MCP 只负责执行。这样可以直接利用网页端旗舰模型、现有订阅能力和产品侧持续升级，而不需要为了每个执行步骤再单独调用一套模型 API。

**重要边界：**这里的优势是“无需额外按 API token 为每一步执行付费”，不是承诺任何网页模型拥有字面意义上的无限 token。实际上下文长度、频率限制和使用额度仍由对应网页产品、模型和订阅计划决定。

### 3. 面向 AI 高速发展时代的移动办公

传统远程桌面要求人盯着小屏幕点按钮；Codex-Control-MCP 更接近“把意图交给 AI，由 AI 调度设备完成工作”。手机负责下达目标和查看结果，电脑、服务器、浏览器和工具负责执行。

```text
手机 / 平板 / 网页 AI
        │
        │ 标准 MCP
        ▼
Codex-Control-MCP
        │
        ├─ 本机 Codex Runtime：Shell / Files / Git / Sessions
        ├─ Browser / Computer Use：网页与桌面操作
        ├─ Multi-Host：MCP / SSH / Docker / 远程设备
        ├─ Recoverable Tasks：长任务、断点与恢复
        └─ Dynamic MCP：按需接入独立外部 MCP
```

核心思路只有一句：**模型负责想，Codex-Control-MCP 负责把“想法”可靠地变成真实世界里的设备动作。**
## 当前架构

- **Codex-Control-MCP**：47 个顶层工具，只负责通用执行基础设施，不内置任何 Grok、DirectorDesk 或其他第三方业务/媒体工具。
- **Dynamic MCP**：通过 `mcp_manage / mcp_tool_search / mcp_tool_inspect / mcp_tool_call` 按需注册、发现和调用独立 MCP。它是通用扩展机制，不代表任何被接入的第三方 MCP 属于本项目功能。
- **外部 MCP**：Grok-MCP、DirectorDesk-MCP 等均为独立项目，拥有独立源码、版本、运行状态和验收结果；它们的故障或可用性不参与 Codex-Control-MCP 本体的生产可用性判定。

生产公网地址：`https://codex-control.aiwsb.site/mcp`

生产 Core：`http://127.0.0.1:8774/mcp`，需要 owner Bearer 或 OAuth。Cloudflare tunnel 直接指向 8774。旧 8767 DirectorDesk 聚合网关已退役。

## 0.2.0 新基础设施

### Recoverable Task Runtime

`task_manage` 支持：`create / list / get / checkpoint / block / resume / final_review / complete / recover / resolve_execution`。

任务保存在 `%USERPROFILE%\.codex-control-mcp\state\recoverable-tasks.sqlite3`，使用 SQLite WAL。任务记录 goal、steps、completion conditions、checkpoint、blocker、revision、event history 和 final review。只有全部步骤完成且 final review 为 pass 后才能 complete。

### Multi-Host

- `host_manage`
- `host_exec`
- `host_files`
- `host_route`

支持 `local / mcp / ssh / docker`。优先使用远端 Codex-Control-MCP MCP 节点，没有节点时可使用 SSH；Docker 用于本机容器路由。

### Dynamic MCP

- `mcp_manage`：register / update / list / get / enable / disable / refresh / remove
- `mcp_tool_search`
- `mcp_tool_inspect`
- `mcp_tool_call`

支持 stdio 与 streamable HTTP。第三方 MCP schema 会缓存，并在调用前用 JSON Schema 校验参数。

运行时可以注册任意兼容的外部 MCP。具体注册了哪些服务属于部署环境状态，不属于 Codex-Control-MCP 的静态功能清单。

### Skill 生命周期

`skill_package` 支持 `validate / install / activate / rollback / uninstall / list / inspect`，支持本地目录、ZIP 和 Git URL，提供 SHA-256 校验、stable/development/canary/pinned 通道、版本激活与回滚。

## 历史部署与生产记录（不代表 0.2.2 已部署）

以下保留原有部署/LKG 记录，版本和生产状态仅对应此前验收。本次 0.2.2 的证据以本页候选能力矩阵和实施记录为准。

## 源码生产方式

从 0.2.0 起，日常开发和生产默认都直接运行源码，不再为每次更新执行 PyInstaller 打包。

```powershell
cd 'C:\path\to\Codex-Control-MCP'
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m pytest -q -m "not integration"
```

生产计划任务 `Codex-Control-MCP-OnDemand` 直接运行：

```text
C:\path\to\Codex-Control-MCP\.venv\Scripts\python.exe
-m codex_control_mcp --home %USERPROFILE%\.codex-control-mcp serve --transport streamable-http
```

HTTPS 计划任务 `Codex-Control-MCP-HTTPS-OnDemand` 同样使用该 venv 运行 `tunnel`。

正常更新流程：

```text
修改源码 → pytest → 独立控制器重启源码 Core → MCP/能力验收 → 再固化已验证 LKG
```

只有明确需要向没有 Python 环境的外部机器分发时，才单独考虑构建二进制。仓库中的旧打包脚本仅作为历史工具保留，不属于默认发布流程。

## 自动恢复

`Codex-Control-MCP-Watchdog` 每分钟及用户登录时运行。连续两个真实周期观察到 8774 无监听才请求独立 `Codex-Control-MCP-Core-Controller`：

1. 正式 `Codex-Control-MCP-OnDemand`：editable 源码冷启动。
2. `Codex-Control-MCP-Core-Current`：独立任务直接运行当前源码。
3. `Codex-Control-MCP-Core-LKG`：独立任务设置 LKG 的 `PYTHONPATH` 后运行。
4. 每条候选必须验证实际 8774 监听、对应 `service.json` PID、认证 MCP initialize/version 和 47 工具；任务显示 Running 不等于通过。
5. 控制器有独立进程寿命、唯一请求 ID 队列、操作系统字节锁、有限超时、确定退出码和恢复报告。`maintenance.lock.guard` 是永久互斥文件，不是维护中标志；只有被进程持有的锁才生效，不能因一个遗留空 `maintenance.lock` 无限停掉 watchdog。
6. HTTPS 恢复另行保守判定：活着的 cloudflared 不因 Core 502 被重启；配置保持 `protocol: http2`、origin `127.0.0.1:8774`，不添加错误的 `edge-ip-version: 4`。

安装/更新调度配置（管理员 PowerShell；安装会备份原任务，不主动重启服务）：

```powershell
.\scripts\Install-Core-RecoveryTasks.ps1 -Apply
```

正常重载只提交请求，不从 Core 派生一个负责拉回自己的控制器：

```powershell
.\scripts\Request-Core-Restart.ps1
```

旧 `state/final-core-reload.ps1` 已改为上述提交入口的兼容包装，不再直接停止 Core。提交成功只代表 accepted，最终结果读 `%USERPROFILE%\.codex-control-mcp\state\core-controller-report.json`，并实调公网 MCP。

退出码：`0` 已启动/已健康；`10` 另一控制器持锁；`21` 旧端口未释放；`22` 无监听但存在其他活源码 host；`30` 三条候选全部失败；`40` 配置/内部错误。失败不会冒充回滚成功。

正式任务具备 AtStartup/AtLogon，使用用户交互式会话，不保存用户密码或改用 S4U。已验真实冷启动；**未通过注销或重启 Windows 验证登录/开机，不宣称无人登录前可用**。

旧 LKG 入口：`%USERPROFILE%\.codex-control-mcp\state\lkg-0.2.0`。 没有 `lkg-active.json` 时仍使用它；新快照由 active 选择记录指定。本次云端未执行真实激活。

## 外部组件边界

本仓库不包含 Grok 媒体能力，也不包含 DirectorDesk 业务能力。Grok-MCP、DirectorDesk-MCP 或其他第三方 MCP 即使通过 Dynamic MCP 接入，也始终是独立组件。

因此：

- 外部 MCP 的 HTTP 错误、模型可用性、媒体生成结果和回归状态，不是 Codex-Control-MCP 的验收项。
- Codex-Control-MCP 只验收“能否按通用 MCP 协议注册、发现、校验和调用外部工具”，不为外部工具自身业务结果背书。
- `docs/` 中旧版本验收记录只反映当时版本与当时环境，不应用来覆盖当前 README 的现状说明。

## 验收基线

当前 Codex-Control-MCP 本体基线：

- Core 非集成回归：242 passed，18 integration deselected
- Core 顶层工具：47
- 静态 `grok_*`：0
- 公网 OAuth metadata/challenge 正常
- 公网 MCP：47 tools，`bridge_version=0.2.0`
- Recoverable Task、Multi-Host local/MCP/file roundtrip、Skill 生命周期真实生产验收通过；SSH/Docker 只验协议/路由契约，不冒充真实远端执行。
- Browser 的 start/snapshot/fill/click/press/scroll/navigate/close 已真实通过；测试仅使用隔离页并关闭临时 HTTP server。
- Proxy PASS 要求官方 Codex command/exec 子进程继承代理，实际 CONNECT 到 `127.0.0.1:7897`，经系统信任库完成 TLS 证书验证并取得 HTTPS 成功响应，不能拿裸 HTTPS 200 推断代理链路。
- Computer Use 当前可用。此前出现过“工具目录可发现 `computer_snapshot`，但某个 ChatGPT 对话实际执行时插件被禁用”的会话状态；新开 ChatGPT 对话重新挂载工具后，`computer_snapshot` 已真实成功，随后 `computer_click`、`computer_type`、`computer_press` 等直接执行也取得真实回执。该现象按宿主会话工具挂载/刷新问题处理，不再归类为 Computer Use 后端输入或窗口激活缺陷。

## LKG 与证据范围

`scripts/freeze_source_lkg.py --tests-json <证据.json>` 默认只生成不可变版本化源码候选。隔离 probe 将 pyproject 版本与实际包版本核对，记录实际工具数，并校验无静态 Grok/Director 与默认端口 8774。manifest 绑定版本、工具数、逐文件 SHA256 和证据；相同源码的后续验收生成另一快照，不改写旧证据。快照不包含凭据；外部 MCP 业务能力不属于本项目 LKG 认证范围，`packaging_required=false`。

激活要求输入及快照原有证据均为 `all_passed=true`、`full_stack_accepted=true`。本次云端缺真实 Windows 全栈证据，交付中 `full_stack_accepted=false`，不能用于正式激活。已通过实机验收后才可使用 `--activate`；`--snapshot <路径>` 选择已冻结且有原始验收证据的快照。激活只切换选择，不启动服务、不清空状态库、不重放命令。

新的 `scripts/lkg_state.py` 在 controller 的维护 guard 下核验指纹、执行前后隔离 probe，并原子替换单个 `state/lkg-active.json`。该记录同时指定来源、版本与工具数；host 与 controller 共用它，formal/current 候选保持各自的期望版本/工具数。失败恢复上一选择，旧 `lkg-0.2.0` 目录从不被重命名或覆盖。回滚失败或进程中断留下 `lkg-active.rollback.json` 时，host/controller 拒绝启动选定 LKG；在维护锁内人工检查记录、恢复其 previous 原始选择（null 表示删除 active 选择）并核验后，才能移除 rollback 记录。不得绕过该阻碍重发未知业务命令。

受控验证命令：`python -m pytest tests/test_lkg_compatibility.py -q`。它验证临时目录中的快照/选择故障，不能证明 Windows PowerShell、Scheduler、DPAPI、实机服务切换或电源中断已验收。安装器 `ExpectedVersion/ExpectedToolCount` 描述 formal 候选；current 从隔离 probe 读取，LKG 从 active 记录读取，无记录则保持旧 0.2.0/47。



## 原生维护与分层故障回执

日常维护优先使用 `scripts\Maintain-Core.cmd --check`；明确重启时使用 `--restart`。默认只检查，入口不修改 PowerShell 执行策略。详见 [原生维护与 HTTP 观测](docs/native-maintenance-and-http-observation.md)。

工具回执包含 `operation_id`，HTTP 回执额外关联服务器生成的 `http_request_id`。成功读取输出不等于命令执行成功，进程非零退出保留失败结果。平台内部安全判定仍不在本机可观察范围内。
