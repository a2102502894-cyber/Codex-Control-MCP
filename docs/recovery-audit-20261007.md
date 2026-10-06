# Session, transaction and node continuation recovery audit

This round starts from `5355f84` on `fix/mcp-stall-20261007`.

## Corrected behavior

* Session creation rolls its in-memory reservation back if initial history
  persistence fails before dispatch. A failed save no longer consumes the last
  session slot or leaves a phantom starting session that delays runtime refresh.
* Task creation/update/completion and idempotency reservation/finalization roll
  failed SQLite transactions back before the next request. A later successful
  request cannot accidentally commit a previous failed change. Unknown executed
  operations retain their durable reservation and are never automatically replayed.
* Session continuation preserves the output format and byte budget. Raw binary
  output stays raw across pages; auto receipts retain their selected text budget.
* Dynamic calls validate the current registration and tool cache before accepting
  an immutable connection snapshot. Replacement, disable/removal or schema changes
  during validation prevent dispatch to a superseded node.
* Continuations carry `expected_connection_digest`, which binds the registered
  connection epoch and settings. Replacement with identical settings, registry
  removal/re-registration and disable/re-enable invalidate earlier continuations.
  The epoch persists across bridge restarts. Callers must forward the whole
  supplied `next_action`; the optional guard cannot protect callers that omit it.
* Connection changes after dispatch preserve the actual result and explicitly
  withhold unsafe continuation. Missing or malformed remote continuation targets
  cannot be promoted to local tools or another node. No operation is replayed.
* The source validator launches the trusted absolute worker file with Python's
  isolated mode; it does not depend on the working directory, PYTHONPATH or an
  editable installation. The frozen worker entrypoint remains unchanged.
* Read-only task, host-status and skill-inspection queries do not reserve
  idempotency results. Repeated read keys return the latest observation. Independently
  locked task/host/MCP registry reads bypass the unrelated global file mutation
  lock; skill reads retain their existing lock for package transaction consistency.

## Validation

Final native regression: **466 passed, 19 deselected, 71.25 seconds**.
All 80 observed test child processes started without console windows.
Compile checks, source transfer consistency and pinned dependency checks passed.

`tests/test_round5_recovery.py` adds 34 cases. The first 17 fault cases failed on
the original code before fixes. An intermediate focused run passed 117 cases;
subsequent review added read-query and malformed-continuation coverage.

The first broad run accidentally used the system Python with MCP 1.28.1 and
Starlette 1.7.0, outside the project's pinned dependencies. Its 439 passes and
17 failures are retained as diagnostic evidence, not accepted as production
validation. The native profile uses the existing service interpreter under
`.venv`, MCP 1.30.0, Starlette 1.6.0 and JSON Schema 4.26.0. A temporary assertion
indentation error was corrected before the final native run. No dependencies or
credentials were changed to make tests pass.

`scripts/verify_round5_repair.py` verifies a separate owned HTTP service and
official runtime 0.160.0. It recovers all 8192 raw binary bytes with raw/1024-byte
continuations, recovers a 7006-byte long Unicode line in seven pages, rejects
changed-file continuation and unsupported write options, exercises real stdio
validation, and proves a replaced-node continuation is rejected before a third
dispatch. Its owned temporary service stops with exit code zero. Production mode
uses harmless owned commands and file reads/rejection probes; it never changes
production dynamic-node registrations.

Evidence lives under `evidence/stall-repair-20261007/`:
`before-round5.zip`, `round5-before.xml`, `round5-targeted-final.xml`,
`round5-regression.{log,xml}`, `round5-final-regression.{log,xml}`,
`round5-candidate.json`, `round5-production.json` and the production switch receipt.

The native profile excludes the existing integration marker and WSL availability
test. It does not verify deployed SSH/Docker hosts, every official desktop GUI
action or all client tool-schema caches. The bridge cannot start the caller's
next model turn or intercept a final reply. A finite passing audit cannot establish
the absence of all defects in every possible environment.
