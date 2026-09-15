# Codex-Control-MCP 0.2.0

Codex-Control-MCP 是本机/远端执行基础设施 MCP。它通过已安装的官方 Codex Runtime 提供 Shell、文件、Git、长任务会话、Browser、Computer Use，以及 0.2.0 新增的可恢复任务、多主机路由、动态 MCP 和 Skill 生命周期管理。

## 当前架构

- **Codex-Control-MCP**：47 个顶层工具。只负责执行基础设施，不内置 DirectorDesk 或 Grok 媒体工具。
- **DirectorDesk-MCP**：独立 stdio MCP，原 17 个 `director_*` 场景工具通过本机 DirectorDesk `/director` bridge 调用；另加 `director_host_task` 非执行宿主任务接口（合计 18）。它仅 inspect/prepare/get/cancel，不会调用 ChatGPT 原生工具或生成图片，见 `C:\path\to\DirectorDesk-MCP\README.md`。
- **Grok-MCP**：独立 stdio MCP，3 个媒体工具：`grok_image_generate`、`grok_image_edit`、`grok_video_generate`。
- **Dynamic MCP**：Codex-Control-MCP 通过 `mcp_manage / mcp_tool_search / mcp_tool_inspect / mcp_tool_call` 按需发现和调用独立 MCP。第三方工具不再静态塞进 Core。

生产公网地址：`https://codex-control.aiwsb.site/mcp`

生产 Core：`http://127.0.0.1:8774/mcp`，需要 owner Bearer 或 OAuth。Cloudflare tunnel 直接指向 8774。旧 8767 DirectorDesk 聚合网关已退役。

## 0.2.0 新基础设施

### Recoverable Task Runtime

`task_manage` 支持：`create / list / get / checkpoint / block / resume / final_review / complete`。

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

当前正式注册：

- `director` → DirectorDesk-MCP，18 tools（17 场景工具 + 1 非执行宿主任务工具）
- `grok` → Grok-MCP，3 tools

### Skill 生命周期

`skill_package` 支持 `validate / install / activate / rollback / uninstall / list / inspect`，支持本地目录、ZIP 和 Git URL，提供 SHA-256 校验、stable/development/canary/pinned 通道、版本激活与回滚。

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

当前 LKG：`%USERPROFILE%\.codex-control-mcp\state\lkg-0.2.0`。

## 独立组件

### DirectorDesk-MCP

源码：`C:\path\to\DirectorDesk-MCP`

独立 bridge key：`%USERPROFILE%\.director-desk-mcp\state\bridge.key`。不再依赖旧 `director-desk-gateway` 目录。

### Grok-MCP

源码：`C:\path\to\Grok-MCP`

Grok-MCP 不复制 API Key；通过 `GROK_MCP_CREDENTIAL_PATH` 引用现有 Windows DPAPI 凭据文件，密钥不进入 Dynamic MCP 注册表返回值。

## 验收基线

2026-09-10 最后回归与当前边界（详细结果以 `evidence/architecture-0.2.0-final.json` 为准，不能只看本表当全绿）：

- Core 非集成回归：240 passed，18 integration deselected
- Core 顶层工具：47
- 静态 `grok_*`：0
- DirectorDesk-MCP：17 tools，`director_read` 实调成功
- Grok-MCP：3 tools，独立生图实调成功
- 公网 OAuth metadata/challenge 正常
- 公网 MCP：47 tools，`bridge_version=0.2.0`
- Dynamic MCP：director=17 ready，grok=3 ready
- Grok-MCP 回归：12 passed；DirectorDesk-MCP 回归：1 passed。
- Recoverable Task、Multi-Host local/MCP/file roundtrip、Skill 生命周期真实生产验收通过；SSH/Docker 只验协议/路由契约，不冒充真实远端执行。
- Browser 的 start/snapshot/fill/click/press/scroll/navigate/close 已真实通过；测试仅使用隔离页并关闭临时 HTTP server。
- Proxy PASS 要求官方 Codex command/exec 子进程继承代理，实际 CONNECT 到 `127.0.0.1:7897`，经系统信任库完成 TLS 证书验证并取得 HTTPS 成功响应，不能拿裸 HTTPS 200 推断代理链路。
- Computer Use 返回成功不等于真实输入成功；失败会撤销旧 verified，type 必须观察到输入后文本才累计已验证动作。本轮发现官方 CUA 的输入/窗口激活问题，不能据此前虚假的计数报 healthy。
- Grok 改图冷却后仍返回 HTTP 502，状态未知。POST 不自动重放；超时和断连也标为 execution_state_unknown，GET/status 允许有限重试。已保存脱敏上游响应；真实部署源站日志仍缺失，不能推测已经修好。生图/视频历史有效产物保留，不重复消费生成额度。
- 旧 `director-desk-gateway` 空目录仍被现有 DirectorDesk 工作目录持有，已登记下次 Windows 重启删除；没有为了删空目录重启用户服务。

## LKG 与证据范围

`scripts/freeze_source_lkg.py` 可生成独立源码候选、隔离导入核对 0.2.0/47 工具/无静态 Grok 或 Director/默认 8774，并写 UTF-8 manifest 与逐文件 SHA256。默认只生成候选；只有验收范围明确且通过后才使用 `--activate`。快照不包含凭据，不等于把未通过的外部 GUI/Grok 能力认证为通过，`packaging_required=false`。

