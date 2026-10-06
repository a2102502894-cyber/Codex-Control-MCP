from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from codex_control_mcp.errors import BridgeError
from codex_control_mcp.task_runtime import RecoverableTaskStore


@pytest.fixture
def store(tmp_path):
    value = RecoverableTaskStore(tmp_path / "tasks.sqlite3")
    try:
        yield value
    finally:
        value.close()


def create(store, **extra):
    return store.create({"title": "Full acceptance", "goal": "Verify the whole requested scope",
                         "steps": [{"id": "page", "title": "Verify all pages"}],
                         "completion_conditions": ["All pages verified"], **extra})["task_id"]


def finish_steps(store, task_id):
    return store.checkpoint({"task_id": task_id, "step_id": "page", "step_status": "completed",
                             "summary": "All page checks ran", "evidence": ["page-checks.json"]})


def review(store, task_id, **extra):
    return store.final_review({"task_id": task_id, "review_status": "pass",
                               "summary": "Acceptance passed", "verified": ["All pages verified", "page-checks.json passed"],
                               **extra})


def raises_code(code, operation):
    with pytest.raises(BridgeError) as exc:
        operation()
    assert exc.value.code == code


@pytest.mark.parametrize("extra,code", [
    ({"missing_checks": ["Mobile page not checked"]}, "task_missing_checks"),
    ({"verified": ["Build passed"]}, "task_conditions_unverified"),
    ({"verified": []}, "task_review_evidence_required"),
])
def test_incomplete_review_is_rejected_without_changing_state(store, extra, code):
    task_id = create(store)
    finish_steps(store, task_id)
    before = store.get({"task_id": task_id})["task"]
    raises_code(code, lambda: review(store, task_id, **extra))
    assert store.get({"task_id": task_id})["task"] == before
    raises_code("task_review_required", lambda: store.complete({"task_id": task_id}))


def test_passing_review_cannot_precede_step_completion(store):
    task_id = create(store)
    raises_code("task_incomplete", lambda: review(store, task_id))
    finish_steps(store, task_id)
    raises_code("task_review_required", lambda: store.complete({"task_id": task_id}))


@pytest.mark.parametrize("change", ["checkpoint", "block", "resume"])
def test_progress_and_resume_invalidate_review(store, change):
    task_id = create(store)
    finish_steps(store, task_id)
    assert review(store, task_id)["task_summary"]["can_complete"]
    if change == "checkpoint":
        store.checkpoint({"task_id": task_id, "summary": "New acceptance evidence"})
    elif change == "block":
        store.block({"task_id": task_id, "summary": "Acceptance service unavailable"})
        raises_code("task_blocked", lambda: review(store, task_id))
        raises_code("task_blocked", lambda: store.complete({"task_id": task_id}))
        store.resume({"task_id": task_id, "summary": "Service restored"})
    else:
        store.resume({"task_id": task_id})
    current = store.get({"task_id": task_id})
    assert current["task"]["final_review"] is None
    assert current["continuation_required"] and not current["task_completed"]
    raises_code("task_review_required", lambda: store.complete({"task_id": task_id}))
    review(store, task_id)
    assert store.complete({"task_id": task_id})["task_completed"]


def test_regressed_step_restores_execute_phase_and_current_step(store):
    task_id = create(store)
    finish_steps(store, task_id)
    review(store, task_id)
    regressed = store.checkpoint({"task_id": task_id, "step_id": "page", "step_status": "pending"})
    assert regressed["task_summary"]["phase"] == "execute"
    assert regressed["task_summary"]["current_step_id"] == "page"
    assert regressed["task_summary"]["remaining_step_ids"] == ["page"]
    raises_code("task_incomplete", lambda: store.complete({"task_id": task_id}))
    finish_steps(store, task_id)
    raises_code("task_review_required", lambda: store.complete({"task_id": task_id}))


def test_single_step_completion_moves_current_to_remaining_step(store):
    task_id = create(store, steps=[{"id": "page", "title": "Pages"}, {"id": "mobile", "title": "Mobile"}])
    receipt = finish_steps(store, task_id)
    assert receipt["task_summary"]["current_step_id"] == "mobile"
    assert receipt["continuation_required"]


def test_contradictory_batch_step_fields_are_atomic(store):
    task_id = create(store)
    before = store.get({"task_id": task_id})["task"]
    raises_code("invalid_arguments", lambda: store.checkpoint({"task_id": task_id,
                "completed_step_ids": ["page"], "current_step_id": "page"}))
    assert store.get({"task_id": task_id})["task"] == before


def test_failed_review_can_record_missing_checks_before_steps_finish(store):
    task_id = create(store)
    receipt = review(store, task_id, review_status="failed", verified=[], missing_checks=["All pages"])
    assert receipt["task_summary"]["phase"] == "execute"
    assert receipt["continuation_required"]
    assert not receipt["task_summary"]["can_complete"]


def test_failed_review_supersedes_prior_pass(store):
    task_id = create(store)
    finish_steps(store, task_id)
    review(store, task_id)
    review(store, task_id, review_status="failed", missing_checks=["New regression found"])
    raises_code("task_review_required", lambda: store.complete({"task_id": task_id}))


def test_review_is_not_closeout_and_reads_do_not_invalidate(store):
    task_id = create(store)
    finish_steps(store, task_id)
    passed = review(store, task_id)
    assert passed["task_summary"]["phase"] == "verify"
    assert passed["continuation_required"] and not passed["task_completed"]
    assert passed["task_summary"]["can_complete"]
    revision = passed["task_summary"]["revision"]
    store.list({})
    store.get({"task_id": task_id})
    finished = store.complete({"task_id": task_id, "expected_revision": revision})
    assert finished["task_completed"] and not finished["continuation_required"]
    assert finished["task_summary"]["phase"] == "closeout"
    raises_code("task_revision_conflict", lambda: store.complete({"task_id": task_id, "expected_revision": revision}))
    assert store.complete({"task_id": task_id})["already_completed"]


def test_empty_draft_cannot_be_vacuously_completed(store):
    task_id = create(store, steps=[], completion_conditions=[])
    raises_code("task_completion_scope_required", lambda: review(store, task_id))


def test_conditions_only_task_can_finish_with_matching_verification(store):
    task_id = create(store, steps=[])
    review(store, task_id)
    assert store.complete({"task_id": task_id})["task_completed"]


def test_steps_only_task_still_needs_verified_facts(store):
    task_id = create(store, completion_conditions=[])
    finish_steps(store, task_id)
    raises_code("task_review_evidence_required", lambda: review(store, task_id, verified=[]))
    review(store, task_id, verified=["page-checks.json passed"])
    assert store.complete({"task_id": task_id})["task_completed"]


def test_duplicate_conditions_rejected(store):
    raises_code("invalid_arguments", lambda: create(store, completion_conditions=["condition", " condition "]))


def test_appended_scope_preserves_original_work_and_requires_new_review(store):
    task_id = create(store)
    finish_steps(store, task_id)
    review(store, task_id)
    receipt = store.manage({"action": "checkpoint", "task_id": task_id,
                           "steps": [{"id": "mobile", "title": "Verify mobile"}],
                           "completion_conditions": ["Mobile verified"]})
    task = store.get({"task_id": task_id})["task"]
    assert task["steps"][0]["status"] == "completed"
    assert task["completion_conditions"] == ["All pages verified", "Mobile verified"]
    assert receipt["task_summary"]["remaining_step_ids"] == ["mobile"]
    assert task["final_review"] is None
    raises_code("task_incomplete", lambda: store.complete({"task_id": task_id}))
    store.checkpoint({"task_id": task_id, "step_id": "mobile", "step_status": "completed"})
    raises_code("task_conditions_unverified", lambda: review(store, task_id))
    review(store, task_id, verified=["All pages verified", "Mobile verified", "page and mobile reports passed"])
    assert store.complete({"task_id": task_id})["task_completed"]


def test_empty_draft_acquires_scope_through_checkpoint(store):
    task_id = create(store, steps=[], completion_conditions=[])
    store.checkpoint({"task_id": task_id, "completion_conditions": ["All pages verified"]})
    review(store, task_id)
    assert store.complete({"task_id": task_id})["task_completed"]


@pytest.mark.parametrize("extra", [
    {"steps": [{"id": "page", "title": "Replace original scope"}]},
    {"steps": [{"id": "extra", "title": "Extra"}], "completion_conditions": ["All pages verified"]},
    {"steps": [{"id": "extra", "title": "Extra"}], "current_step_id": "unknown"},
])
def test_scope_extension_errors_do_not_persist_partial_changes(store, extra):
    task_id = create(store)
    before = store.get({"task_id": task_id})["task"]
    raises_code("invalid_arguments", lambda: store.checkpoint({"task_id": task_id, **extra}))
    assert store.get({"task_id": task_id})["task"] == before


def test_action_incompatible_parameters_are_rejected(store):
    task_id = create(store)
    before = store.get({"task_id": task_id})["task"]
    raises_code("invalid_arguments", lambda: store.manage({"action": "checkpoint", "task_id": task_id,
                "goal": "Quietly shrink the original goal"}))
    assert store.get({"task_id": task_id})["task"] == before


def test_review_preserves_supporting_evidence(store):
    task_id = create(store)
    finish_steps(store, task_id)
    result = store.manage({"action": "final_review", "task_id": task_id,
                           "summary": "Verified", "review_status": "pass",
                           "verified": ["All pages verified"], "evidence": ["page-checks.json"]})
    assert result["final_review"]["evidence"] == ["page-checks.json"]


def test_legacy_unbound_review_requires_reverification(store):
    task_id = create(store)
    finish_steps(store, task_id)
    task = store.get({"task_id": task_id})["task"]
    task["final_review"] = {"status": "pass", "verified_facts": ["All pages verified"], "missing_checks": []}
    store.db.execute("UPDATE tasks SET doc=? WHERE id=?", (json.dumps(task), task_id))
    store.db.commit()
    raises_code("task_review_stale", lambda: store.complete({"task_id": task_id}))
    review(store, task_id)
    assert store.complete({"task_id": task_id})["task_completed"]


def test_same_revision_changed_state_does_not_reuse_review(store):
    task_id = create(store)
    finish_steps(store, task_id)
    review(store, task_id)
    task = store.get({"task_id": task_id})["task"]
    task["steps"][0]["summary"] = "Evidence changed outside this store"
    store.db.execute("UPDATE tasks SET doc=? WHERE id=?", (json.dumps(task), task_id))
    store.db.commit()
    raises_code("task_review_stale", lambda: store.complete({"task_id": task_id}))


def test_old_completed_record_is_preserved_without_migration(store):
    task_id = create(store)
    task = store.get({"task_id": task_id})["task"]
    task.update(status="completed", phase="closeout", final_review={"status": "pass"})
    raw = json.dumps(task)
    store.db.execute("UPDATE tasks SET status='completed',doc=? WHERE id=?", (raw, task_id))
    store.db.commit()
    assert store.get({"task_id": task_id})["task_completed"]
    assert store.complete({"task_id": task_id})["already_completed"]
    assert store.db.execute("SELECT doc FROM tasks WHERE id=?", (task_id,)).fetchone()[0] == raw


def test_two_independent_stores_cannot_overwrite_a_concurrent_update(tmp_path):
    path = tmp_path / "shared.sqlite3"
    left, right = RecoverableTaskStore(path), RecoverableTaskStore(path)
    try:
        task_id = create(left)
        states = [left.get({"task_id": task_id})["task"], right.get({"task_id": task_id})["task"]]
        barrier = threading.Barrier(2)

        class ReadBarrierConnection:
            def __init__(self, raw):
                self.raw = raw
            def execute(self, sql, params=()):
                if sql == "SELECT doc FROM tasks WHERE id=?":
                    row = self.raw.execute(sql, params).fetchone()
                    barrier.wait(timeout=5)
                    class Row:
                        def fetchone(self):
                            return row
                    return Row()
                return self.raw.execute(sql, params)
            def __getattr__(self, name):
                return getattr(self.raw, name)

        for value in (left, right):
            value.db = ReadBarrierConnection(value.db)

        def write(value, state, text):
            state["summary"] = text
            try:
                value._save(state)
                return "saved"
            except BridgeError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(write, value, state, text) for value, state, text in
                       [(left, states[0], "left"), (right, states[1], "right")]]
            assert sorted(f.result(timeout=10) for f in futures) == ["saved", "task_revision_conflict"]
        left.db = left.db.raw
        right.db = right.db.raw
        task = left.get({"task_id": task_id})["task"]
        assert task["revision"] == 2 and task["summary"] in {"left", "right"}
    finally:
        left.close()
        right.close()
