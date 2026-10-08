# Task interruption recovery - 2026-10-08

The GEO stage-three acceptance report recorded 133 read-only checks:
123 passes and 10 HTTP 429 responses. The previous conversation stopped
after writing a test script and before execution/final delivery. Bridge
health checks showed no pending work at the time of inspection. Those
checks do not establish why ChatGPT stopped the next model turn.

There are separate failure domains: (1) a running MCP command or its
output; (2) a ChatGPT model/client that stops requesting tools; (3) a
business command that fails or triggers service rate limits. The first
was addressed by commit 8df355d. MCP cannot force a stopped model to
resume or generate its final chat message.

The new task_manage continuation_observation reports seconds since the
last persistent checkpoint, an active-task suspicion flag after 180
seconds, the next incomplete step, and the checkpoint timestamp.
It states explicitly that the client model state is unobservable and
the running process state has not been checked. Check session_list,
session_read, and the original logs before re-executing anything.

A resume call against an already-active task with no new summary and
the correct revision is idempotent: no extra revision, no loss of final
review, and no re-execution. An explicit new summary or a previously
blocked task can still be resumed with the usual review invalidation.

For GEO task_2c22cb3a4ee44fb7 the next actions remain: wait for
provider cooldown, run the paced read-only fail-fast acceptance when
appropriate, finish the remaining stage-three checks, and deliver
actual evidence. No production requests were repeated as part of this
MCP repair. Neither the ten 429 responses nor the missing checks were
marked successful.

This repair does not create autonomous agents or a server-side model
loop. Further protection against the client itself stopping requires
support in the ChatGPT client/runtime, beyond MCP server control.
