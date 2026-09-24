# 调用回执与故障定位

本次为 0.2.1 的诊断热修复，新增回执 `diagnostics.schema_version = 1`。不修改权限策略、工具风险声明、官方 RPC 白名单或平台检查。

## 可验证的边界

每个到达 MCP 工具处理器的调用获得独立 `operation_id`，贯穿 `mcp_received`、`tool_received`、`rpc_send`、`rpc_result` / `rpc_error`、`tool_finish`。RPC 响应通过请求 Future 保存关联信息，不依赖读线程继承调用线程的上下文。

`rpc_result` 只证明官方协议响应已收到；不等同于命令退出成功。应同时检查 `ok`、`result.state`、`result.exit_code` 和 `error`。

若平台显示拒绝而本机保留日志没有相应记录，只能报告“未观察到本机接收”。日志可能有轮转、传输中断、SDK 层拒绝等情况；不能据此编造平台内部规则或保证不存在平台安全检查。

## 回执字段

| 字段 | 含义 |
| --- | --- |
| `operation_id` | 本次调用编号，包含失败与幂等缓存返回 |
| `diagnostics.mcp_received` | 是否观察到 MCP 工具处理器接收 |
| `diagnostics.last_verified_stage` | 本次已观察到的最远处理阶段 |
| `diagnostics.rpc_dispatched_count` | 本次发送的官方协议请求数 |
| `diagnostics.rpc_response_count` | 本次收到的官方协议响应数 |
| `diagnostics.failure_origin` | 依据实际错误元数据确定的来源，未知时保留未分类 |
| `result.origin_operation_id` | 长任务最初启动调用的编号；续读调用有自己的新编号 |
| `diagnostics.replayed_operation_id` | 命中幂等缓存时，原始操作编号 |
| `diagnostics.upstream_safety_decision` | 固定为 `not_observable`，不猜测上游安全决策 |

诊断信息只保留协议元数据，不新增命令正文、环境变量值、文件内容或密钥记录。单次回执最多保留 32 条 RPC 元数据，并报告截断；完整事件按原审计轮转机制保存。

## 失败分类

- `bridge_validation`：本机参数校验拒绝，尚未发送该操作的 RPC。
- `codex_app_server`：收到官方 App Server 的协议错误；保留整数错误码、请求编号、运行实例和消息摘要，不记录原消息正文。
- `execution_transport`：连接丢失或响应状态未知；不自动重放。
- `command_process`：进程已结束且退出码非零。
- `bridge_or_adapter_unclassified`：缺少足够证据，不能进一步归因。

`session_read` 在任务失败或丢失时现在返回 `ok=false`，但仍保留 `result` 中的输出、游标和退出码。调用方不能因为外层失败就丢弃诊断输出，也不能把输出读取成功当作任务执行成功。`starting` / `running` 不是成功完成。

## 长任务游标

默认 `auto` 模式返回已有输出和 `next_cursor`，续读从该游标继续。显式 `session` 模式只返回会话元数据，不代表任何输出已交付，首次 `session_read` 从 `cursor=0` 开始。禁止重复发送原命令代替续读。

## 健康检查

`health_observation.mode` 区分主动实测与被动状态；每项记录证据来源、时间、运行实例及可用的证据年龄。历史 `PASS` 不保证下一次调用获准。旧运行实例的验证结果不作为新实例的健康证据。

## 验收记录

- `evidence/call-diagnostics-before.xml`：新增 11 项回归用例在旧实现上全部失败。
- `evidence/call-diagnostics-focused.xml`：相关测试 73 项通过。
- `evidence/call-diagnostics-regression-final.xml`：267 项通过，18 项 integration 标记测试未纳入该轮。
- `evidence/call-diagnostics-live.json`：真实 MCP stdio 加官方 Codex 执行验收，20 次调用，9 组断言通过；范围仅新建测试文件。

真实验收覆盖中文文件往返、非零退出和长任务关联、参数校验、官方目录不存在错误、并发关联、幂等写入、健康证据、测试文件删除和官方子进程退出。前两次验收脚本的问题分别是 Windows 换行转换和元数据会话首次游标取值，失败记录保留在 `call-diagnostics-live-first.json` 与 `call-diagnostics-live-second.json`；未修改产品逻辑以掩盖这些断言。

旧测试的变更仅适配新增审计事件与“失败会话不报告成功”的约定，原授权拒绝、表单转交、游标以及深拷贝断言仍保留。

本说明不构成原平台拦截已解除的证明，也不构成商城支付上线验收。
