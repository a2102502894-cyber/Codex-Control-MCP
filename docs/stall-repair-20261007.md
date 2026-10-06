# 远程停顿修复

auto 命令默认进程期限改为 1 小时，本次请求仍默认等待约 1 秒。显式 timeout_ms 不变；session_start 的 0 仍允许无期限。buffered 为兼容保留默认 30 秒，不推荐用于远程长任务。

exec_command auto 和 session_read 默认只返回 32 KiB 文本页；必须按 next_cursor 继续读取，直到最终状态且 has_more=false。session_read 的 output_format=legacy 恢复旧字段，chunks 返回分块文本，raw 返回分块原始 Base64。中文增量解码与 stdout/stderr 顺序分类保留。

每次会话读取都带 completed、next_action、生效期限、耗时、最近输出和最近读取时间。退出码 124 会提示可能达到期限，但不会断言程序没有主动返回 124。

进度通知遵守 MCP progressToken 要求。未请求进度时不能伪造 token；工具返回 progress_observation 说明是否请求、发送成功次数与失败次数，不能据此保证手机已显示。HTTP 观测补协议版本、固定方法类别、断线和响应字节数；不记录请求正文、授权或用户自定义方法。

core_application_probe.py 经认证执行 initialize、tools/list、被动健康查询、无副作用短命令、会话最终状态读取和 session_list。守护程序据此区分端口存活与应用链路可用；端口仍在时探针失败只标记 degraded，避免误杀在途任务。--public 测试电脑发出的公网回环，不代表 ChatGPT 出口全链路。

本补丁仍是工具桥：它不能令停止发请求的远程对话自动继续多步 AI 任务。桥重启仍会丢失活跃会话控制，需先核对后台任务归属和影响。保持 MCP structuredContent 与文本兼容表示。

PowerShell cmdlet 报错默认 Stop，避免前面的步骤报错后仍继续并最终返回退出 0。shell_error_policy=continue 可以显式恢复旧行为，命令内部也可以自行覆盖。外部程序的非零退出码仍需要脚本显式检查；不擅自推断用户有意处理的非零状态。准备模块的进度不再写入 CLIXML，工具进度由 MCP 通知负责。

动态 MCP 的 timeout_ms 现在覆盖初始化与工具调用整体（此前 stdio 通路没有执行这个期限）。工具调用超时或断线返回 execution_state_unknown、retryable=false，避免对可能已完成的写操作再次执行。只读工具发现仍可重试。
