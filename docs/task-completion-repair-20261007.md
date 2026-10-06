# Task completion and continuation repair

The task store now rejects passing reviews with unfinished steps, missing checks,
unverified completion conditions, no verified facts, an undefined completion
scope, or a blocked task. Include each completion condition verbatim in
`verified`, alongside actual supporting facts. Text assertions remain the
caller's responsibility; the bridge cannot independently verify every external
business condition.

Passing reviews bind the task revision and a fingerprint of the reviewed goal,
steps, conditions, host and blocker state. Checkpoint, block and resume invalidate
the review. Legacy active tasks with unbound reviews require a fresh review;
existing completed records are preserved without migration. A passing review
keeps phase `verify`; only `complete` enters `closeout`.

Step regression repairs phase and current-step selection. Contradictory batch
fields fail atomically. SQLite compare-and-swap prevents independent store
instances from silently overwriting a concurrent update, even without an
explicit `expected_revision`. Clients should still provide that revision and
read the latest state on conflict.

Action-incompatible fields are rejected instead of silently ignored. A
checkpoint may append new `steps` and `completion_conditions`, preserving all
original required work, completed progress and scope. Extension invalidates
the review; duplicate scope, limits and mixed step contradictions fail without
persisting partial changes. This also lets an initially empty draft acquire a
real completion scope without creating another unrelated task.

Receipts and summaries distinguish `task_completed`, `continuation_required`,
remaining steps, unverified conditions, current review and the required next
action. Server instructions and task tool descriptions require full-scope
tracking, continued authorized work after stage summaries, and final review plus
completion before declaring the goal achieved.

This server executes tools, not autonomous model turns. It cannot intercept a
client's final reply, force another model turn, or prove the truth of supplied
verification facts. No model loop or unrelated project task is started by this
repair. Controlled tests use dedicated temporary task databases.
