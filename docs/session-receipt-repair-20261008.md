# Silent sessions and final receipt delivery

This repair continues `fix/mcp-stall-20261007` from `91ebcac`.
The reported symptom is a program continuing or already having finished while
the caller shows no useful progress and never delivers the final result.

## Confirmed defects and changes

1. `session_start` and `exec_command(execution_mode="session")` returned only
   metadata. Its `next_cursor` could already point beyond output produced during
   dispatch, so following it skipped the initial output. They now return the
   first bounded output page and its actual continuation cursor.
2. Polls returned immediately even when a program was silent. The request-scoped
   heartbeat then stopped before its next update. `session_read` now waits at
   most 10 seconds when no unread output exists. Output, exit, or connection loss
   wakes it immediately. `wait_ms=0` supports immediate observations.
3. A process exit set `completed=true` even when additional output pages remained.
   The receipt now requires those pages to be drained before `completed=true`.
   Explicit continuation, output completeness and final-receipt fields prevent
   a process exit from being mistaken for full result delivery.
4. Saved terminal session metadata was listed after a bridge restart but could
   not be read using the original session ID. Retained history now supports
   read-only exit-state recovery. It never attaches to or replays an old process.
   Existing history stores metadata only; unavailable terminal output is clearly
   reported and must be checked in the original task log.
5. Invalid final exit codes such as null, booleans, strings and fractional values
   were accepted as exited states. They now become an unknown result. An immediate
   nonzero `session_start` result also propagates failure at the outer receipt.
6. Routed receipts preserve the new continuation and observation fields. Progress
   notifications for an active session read describe that session and use actual
   monotonic elapsed time instead of incrementing a nominal sleep counter.

## Receipt contract

| Field | Meaning |
| --- | --- |
| `process_completed` | A valid integer final exit code was received from the official execution result. |
| `continuation_required` | The session is still pending/running, or more cached output pages remain. |
| `completed` | No further session polling/output draining is indicated. Unknown or incomplete results still require inspection. |
| `output_complete` | The current page reaches the available stream end with no observed truncation, cursor gap or missing historical output. |
| `final_receipt_ready` | A final exit code and complete output receipt are available. Check the exit code: a failure receipt can also be ready. |
| `result_status` | `running`, `succeeded`, `failed`, or `unknown`; this is execution status, not completion of the user's full task. |
| `heartbeat` | Timestamp, elapsed duration and terminal-output idle time observed by the bridge. It does not prove application-level progress. |
| `read_action` | Safe recovery entry point for a listed session, starting at cursor zero. A caller retaining a confirmed cursor can continue from it. |
| `history_only` | The receipt comes from saved metadata. Controls are unavailable and old processes cannot be reattached. |

Consumers must retain the initial output, follow the full `next_action`, inspect
the final exit code and all loss indicators, then promptly deliver the result or
continue the remaining authorized task. A known task log is the recovery source
when output was redirected or the bridge's bounded output cache is unavailable.
Recovering output can redeliver text; it never reexecutes the underlying command.

## Verification

The initial 23 fault/contract cases failed against the unmodified source. The
first focused run passed 122 cases, and the first native regression passed 489
with 19 deselected. A subsequent review found the immediate `session_start`
failure-wrapping gap and added one more regression case; final results are
recorded in `evidence/progress-receipt-20261008/`.

Final native regression: **490 passed, 19 deselected, 72.31 seconds**. All 24 new
cases are included. The real HTTP verifier completed with exit code zero; both
owned temporary service stops also returned zero. The silent task completed in
12.1 seconds and progress frames were received at approximately 0, 5 and 10 seconds
of the first poll, before the terminal result returned in the second poll.
The 9,915-byte Unicode output was recovered identically across 11 pages.

`scripts/verify_session_receipts.py` runs a temporary authenticated HTTP MCP
service through the installed official runtime. It checks a 12-second program
that writes only to a task log, receives progress frames before the result,
recovers 9,915 bytes of Unicode output across bounded pages, observes an actual
exit code 7, and restarts only its own temporary service to verify saved exit
receipt recovery. `--production` runs only the harmless owned command probes and
does not restart the production service.

The native test profile retains the previous exclusions: the integration marker
and the WSL availability probe. It does not claim every GUI, WSL, SSH/Docker or
client-cache combination has been verified.

## Boundaries

MCP progress belongs to a live request that supplied a progress token. The bridge
cannot force a client to display notifications, start its next model turn, or
produce its final chat reply. Initialization/tool guidance requires periodic
user updates and final result handoff, but those are still caller responsibilities.
No autonomous model loop, command replay, permission expansion, network setting
change, or persistent stdout/stderr logging was introduced.

Protocol references:

- https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/progress
- https://csharp.sdk.modelcontextprotocol.io/concepts/progress/progress.html
