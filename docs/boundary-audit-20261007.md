# Dynamic nodes, file operations and package lifecycle audit

This follow-up audit starts from `73c2239` on `fix/mcp-stall-20261007`.
Changes preserve the owner permission model and do not rewrite existing tasks,
credentials, production registries, tunnel configuration or unrelated services.

## Corrected behavior

* Dynamic validation returns rule names without echoing argument values. Public
  registry receipts redact URL credentials/query values and command arguments.
* Cached schemas cannot retrieve files or external URLs. Declared supported JSON
  Schema dialects are respected; unknown dialects fail before tool execution.
  A private computation worker bounds validation to five seconds or the node's
  shorter deadline. Validation failure/timeout never dispatches or replays a tool.
  Source installs and frozen applications have separate worker entrypoints.
* Updating connection configuration invalidates its cached tools. A refresh
  cannot publish results after that connection was replaced. Tool listing follows
  pagination with cursor, duplicate-name, count, schema and byte limits.
* Search reports unavailable nodes and continues through healthy cached nodes,
  with a thirty-second refresh budget for the entire search.
* Registry persistence failures restore the last saved in-memory state, permitting
  an explicit retry. Malformed registry objects fail clearly without resetting
  their contents. Invalid Schema cache metadata can be regenerated.
* File options are checked for each action and each route. Structured `file_add`
  remains a create-new operation and explicitly rejects force overwrite. Shell
  routes apply recursive deletion/listing, do not suppress arbitrary deletion
  errors, and use atomic create-new writes on Windows. UTF-8 reads/searches are
  explicit; POSIX grep's normal no-match status is not treated as execution failure.
  SSH and Docker destinations cannot be interpreted as command options.
* Long lines have lossless decoded UTF-8 continuation offsets and a ready next
  action. Continuation requires the original file digest and rejects changed
  files or offsets inside a character. Offsets describe decoded UTF-8 text,
  including when a different source encoding was requested.
* Skill staging rejects unsafe archive paths, duplicate paths, links and junctions;
  enforces byte/entry limits before copying/extraction; and streams file contents.
  `.git` and `__pycache__` do not enter installed or runtime packages. Valid empty
  directories and file modes are preserved.
* Skill activation/rollback stages and verifies the new package before replacing
  runtime content. The old runtime remains available until registry commit. A
  failed commit restores runtime and state; installation retry is possible.
  Uninstall failures restore packages. Failed OS restoration returns explicit
  unverified state and recovery paths. Cleanup leftovers are reported separately.

## Validation and evidence

Final native regression: **432 passed, 19 deselected, 71.25 seconds**; 49 new
boundary cases. The headless runner recorded 79 subprocess starts. Earlier
complete regressions passed 421, 428 and 430 cases as the audit expanded.
Final isolated HTTP verification passed and its owned service stopped with
exit code zero. Long Unicode text was recovered in seven pages, 7006 bytes.

`tests/test_round4_boundaries.py` exercises argument privacy, schema resolution and
deadlines, pagination, stale and replaced connections, offline nodes, registry
save failures, malformed cache state, real Windows file commands, Unicode file
continuation, archive budgets/traversal, junctions, package preservation and
activation/uninstall rollback failures. Existing suites remain enabled.

Full suites exclude the existing integration marker and WSL availability test;
they do not establish deployed SSH/Docker, official desktop GUI, or WSL coverage.
Tabbit tests and the real Windows shell checks execute in the installed native
environment. The frozen private worker entrypoint is checked through the CLI;
this audit does not rebuild a Windows release executable.

`scripts/verify_round4_repair.py` uses a separate owned HTTP service, real official
file execution and a real stdio MCP fixture. It checks exact long-line recovery,
changed-file rejection, option errors before writes, private validation errors,
valid dispatch, offline-node search and cache invalidation. Its production mode
does only file reads/rejection probes and never changes production registries.

Evidence is retained under `evidence/stall-repair-20261007/`:
`before-round4.zip`, `round4-verified-regression.{log,xml}`,
`round4-candidate.json`, `round4-production.json` and the formal switch receipt.
Intermediate suite logs preserve the repeated audit/repair cycle.

No finite audit proves that every possible defect is absent. Task receipts
still cannot force the calling model to continue a conversation or intercept
its final response; they enforce the explicit task completion state.
