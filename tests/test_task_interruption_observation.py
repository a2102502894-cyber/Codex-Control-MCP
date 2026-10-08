from datetime import datetime, timedelta, timezone

import pytest

from codex_control_mcp.task_runtime import RecoverableTaskStore
from codex_control_mcp.errors import BridgeError


@pytest.fixture
def task_store(tmp_path):
    store = RecoverableTaskStore(tmp_path / "tasks.sqlite3")
    try:
        yield store
    finally:
        store.close()


def make_task(store):
    return store.create({"title": "GEO", "goal": "Finish stage three",
                         "steps": [{"id": "script", "title": "Create test script"},
                                   {"id": "execute", "title": "Execute acceptance"}],
                         "completion_conditions": ["All tests actually executed"]})["task_id"]


def test_old_active_task_exposes_suspected_interruption_without_claiming_process_state(task_store):
    task_id = make_task(task_store)
    with task_store.lock:
        record = task_store._load(task_id)
        record["updated_at"] = (datetime.now(timezone.utc) - timedelta(minutes=12)).isoformat()
        task_store.db.execute("UPDATE tasks SET updated_at=?,doc=? WHERE id=?",
            (record["updated_at"], __import__("json").dumps(record), task_id))
        task_store.db.commit()
    before = task_store.get({"task_id": task_id})["task"]
    r = task_store.get({"task_id": task_id})["task_summary"]
    o = r["continuation_observation"]
    assert o["suspected_interruption"] is True
    assert o["checkpoint_idle_seconds"] >= 710
    assert o["next_step_id"] == "script"
    assert o["client_model_state"] == "not_observable"
    assert o["running_process_state"] == "not_checked"
    assert task_store.get({"task_id": task_id})["task"] == before


def test_resume_active_without_new_information_is_idempotent(task_store):
    task_id = make_task(task_store)
    task_store.checkpoint({"task_id": task_id, "completed_step_ids": ["script"]})
    before = task_store.get({"task_id": task_id})["task"]
    first = task_store.resume({"task_id": task_id, "expected_revision": before["revision"]})
    second = task_store.resume({"task_id": task_id, "expected_revision": before["revision"]})
    assert first["already_active"] and second["already_active"]
    assert first["execution_restarted"] is False
    assert first["task_summary"]["remaining_step_ids"] == ["execute"]
    assert task_store.get({"task_id": task_id})["task"] == before
    with pytest.raises(BridgeError) as err:
        task_store.resume({"task_id": task_id, "expected_revision": before["revision"] - 1})
    assert err.value.code == "task_revision_conflict"


def test_review_survives_noop_resume_but_explicit_new_summary_invalidates(task_store):
    task_id = make_task(task_store)
    task_store.checkpoint({"task_id": task_id, "completed_step_ids": ["script", "execute"]})
    task_store.final_review({"task_id": task_id, "review_status": "pass", "summary": "Verified",
                             "verified": ["All tests actually executed"]})
    before = task_store.get({"task_id": task_id})["task"]
    task_store.resume({"task_id": task_id})
    assert task_store.get({"task_id": task_id})["task"] == before
    assert task_store.get({"task_id": task_id})["task_summary"]["can_complete"]
    task_store.resume({"task_id": task_id, "summary": "New findings"})
    assert task_store.get({"task_id": task_id})["task"]["final_review"] is None


def test_blocked_and_completed_task_never_mistaken_as_silent_running(task_store):
    task_id = make_task(task_store)
    task_store.block({"task_id": task_id, "summary": "Provider is rate limited"})
    blocked = task_store.get({"task_id": task_id})["task_summary"]
    assert blocked["continuation_observation"]["suspected_interruption"] is False
    resumed = task_store.resume({"task_id": task_id, "summary": "Cooldown finished"})
    assert resumed["continuation_required"] is True
    assert resumed["execution_restarted"] is False
