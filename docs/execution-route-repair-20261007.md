# Execution route and dispatch repair

The follow-up repair keeps the verified first repair and addresses failures
outside the direct exec path. Version remains 0.2.1 and the 48 top-level tools
remain compatible.

- A single bounded pipe writer handles App Server and Tabbit dispatch. The
  caller and protocol reader never perform a blocking stdin write. Queued
  expired operations are cancelled before dispatch, and stalled writes are
  never replayed. Input close is performed away from the shutdown thread.
- Timed-out RPC waits detach their futures. Read-only timeouts no longer pin
  runtime updates. Unknown mutations have bounded metadata and still prevent
  unsafe connection replacement; late responses reconcile these records.
  Recovery reads remain possible when only the mutation uncertainty budget
  is exhausted. Health reports a stalled/uncertain transport as degraded.
- Routed exit codes, nested MCP failures, and running-session receipts propagate
  to the outer tool result. Remote MCP continuation is qualified to its origin
  node. Original nested evidence is retained.
- SSH uses a quoted remote command string; Windows uses UTF-16 encoded
  PowerShell with UTF-8 output. Docker retains its actual argv semantics.
  Process deadline, mode, output page, cwd and env are applied. Unsupported
  remote TTY/WSL/shell and file-range options fail before dispatch.
- Shell file writes refuse accidental overwrite unless force=true. Remote
  PowerShell errors stop the command and native exit codes are preserved.
- Host execution and dynamic tool calls do not hold the global file-write lock.
  Routed read/list/search operations are classified as read-only at execution.
- Local CLI calls have an overall deadline even if HTTP progress keeps the
  stream alive; an unknown dispatched call never falls back to another executor.

Evidence uses owned temporary state and loopback services. `round2-regression`
is the complete headless scope; WSL/integration exclusions are explicit.
`verify_execution_routes.py` additionally exercises the real official runtime,
authenticated MCP-node continuation, long execution, pagination and progress.
Neither a passed headless suite nor loopback SSH shell semantics establishes
all real production SSH/Docker environments or desktop GUI integrations.

Backups: before-round2.zip, before-round2-browser.zip, before-round2-dynamic.zip.
Unknown mutations deliberately remain protected: repairing stale wait records
does not turn a timeout into evidence that an operation did not execute.
