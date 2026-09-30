# CI Runtime 分类与验收范围

此记录对应 API 上传的候选 `96857cd91ea35096258694e8a732901737904f89`（本地 `6415004`，内容 tree 相同）的首次 CI。Ubuntu 两项为 312 passed / 59 skipped / 21 deselected；Windows 两项各为 367 passed / 7 failed / 6 skipped / 21 deselected。[原运行及不可更改的日志](https://github.com/a2102502894-cyber/Codex-Control-MCP/actions/runs/36682336887)保留，不把七个失败改称通过。

## Controller 调用合同

真实 `verify_service(config, pid, expected_tool_count=None)` 已支持默认工具数和显式候选工具数；`authenticated_mcp_probe` 按候选版本/工具数核对实际服务。选定 LKG 加上 expected_tool_count 后走既有三参数路径，旧两参数测试替身抛 TypeError，被 controller 转为 EXIT_UNEXPECTED=60。因此修正替身使用真实函数的 autospec 与可选第三参数，保留候选顺序、旧版本 fallback 及成功退出断言，并补充工具数断言。新增 distinct-candidate 和 real-verifier forwarding 用例覆盖可选/显式工具数。本次不改变生产调用、权限或 Runtime 选择。

## 五个真实 Runtime 前提

| 用例 | 实际前提与行为 | 分类及本轮处理 |
|---|---|---|
| refresh_defers_active_session_and_disconnect_is_lost | 官方 App Server 能执行 PowerShell 流式命令；在有活会话时拒绝 refresh，关闭真实连接并核对 lost/终态，下一次明确执行可新建连接 | integration；保留原真实执行和断言。新增受控 fixture 仅验证延迟刷新逻辑；原有持久化 lost/generation 测试仍运行 |
| pty_resize_and_input | 官方 command/exec 支持 ConPTY、stdin、resize、尾输出与非伪造退出码 | integration；保留原真实执行和断言。新增 fixture 验证代际拒绝和 write/resize/kill 参数，不能证明真实 PTY 可用 |
| controlled_proxy_route_through_official_child | 官方子进程继承受控 HTTP_PROXY，真正访问临时 loopback 代理；不接触用户代理 | integration；保留原代理服务/请求断言。fixture 仅验证 env 序列化，不能证明子进程继承或网络路由 |
| stdio_real_mcp | 真实 MCP stdio 服务及已装官方 Runtime，实际执行 marker、exit 9 并回传结构化错误 | integration；保留原 SDK/命令/错误断言。受控 MCP memory stream 用例补成功、非零退出与非法参数契约，不能当作真实 stdio+Runtime 验收 |
| http_real_mcp_auth_origin_body_limits | loopback HTTP 服务、现有官方 Runtime、SDK 真实执行；随后 doctor --active 经运行服务核对 shell | integration；保留 token/origin/host/body/SDK/doctor 原断言。既有 HTTP gate 与真实 ASGI MCP manager fixture 仍执行，但不能证明官方执行链 |

五项在普通 hosted Windows runner 上失败为 runtime_missing，不是 Windows 内核或 DPAPI 失败。本次直接在这五个测试上增加 integration 标记，不添加缺 Runtime 时自动 skip，也不删除用例。未安装或伪造 Codex executable，没有认证/权限变更。

普通 CI 的 `-m 'not integration'` 明确排除它们；因此普通 CI 即使通过也仅表示当前可运行回归。原来的 WSL availability `-k` 排除继续保留。原 invalid-arguments 用例不调用 Runtime，移除其不必要的模块级 Windows skip 后也在 Linux 执行；WSL availability 仍保留单独 Windows 条件。DPAPI 与 Windows lifecycle 用例继续在 hosted Windows 运行。

## 可执行的官方 Runtime 验收

新增 `.github/workflows/official-runtime-acceptance.yml`，仅 workflow_dispatch，使用**已有、可丢弃的 Windows lab runner**，标签 `self-hosted, Windows, ccm-runtime-lab`。不自动运行 push/PR，不注册 runner，不操作用户电脑，不创建 secrets，不安装 Codex 或改全局配置。操作者需先核验将执行的 commit 和 lab 的既有官方安装；lab 需 PowerShell 7、Python 3.11+、官方 command/exec 可用并已有必要授权。Discovery 前置检查只读版本/指纹，找不到 Runtime 则失败，不冒充跳过成功。

job 仅在其工作区建立 venv、安装项目测试依赖，严格执行上述五个 node ID，随后要求 JUnit 恰好五项且零 skip/failure/error。测试创建临时 Bridge homes、端口、进程和受控 HTTP 代理。即使五项通过，runtime-scope.json 的 full_stack_accepted 仍 false；CUA GUI、用户 Windows 实机、LKG 真实服务激活等仍需独立证据。**本轮没有触发此 job，五项真实 Runtime 验收未执行。**

已有 lab 中可复现：

```powershell
python -m pytest -q -m integration --tb=short --junitxml=runtime-results.xml tests/test_runtime_edges.py::test_refresh_defers_active_session_and_disconnect_is_lost tests/test_runtime_edges.py::test_pty_resize_and_input tests/test_runtime_edges.py::test_controlled_proxy_route_through_official_child tests/test_transport.py::test_stdio_real_mcp tests/test_transport.py::test_http_real_mcp_auth_origin_body_limits
```

不要用 `full_stack_accepted=true` 代替缺失的证据；fixture 测试、hosted Windows 单测和真正官方 Runtime 验收分别报告。
