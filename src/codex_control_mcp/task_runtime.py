from __future__ import annotations

import copy
import hashlib
import json
import pathlib
import sqlite3
import threading
import uuid

from .common import utc_now
from .errors import BridgeError

TASK_STATUSES = {"active", "blocked", "completed"}
STEP_STATUSES = {"pending", "in_progress", "completed"}


def _txt(value, field, required=False, limit=16384):
    value = str(value or "").strip()
    if required and not value:
        raise BridgeError("invalid_arguments", f"{field} is required.")
    if len(value.encode("utf-8")) > limit:
        raise BridgeError("invalid_arguments", f"{field} is too large.")
    return value


def _event(kind, summary=""):
    return {"type": kind, "summary": str(summary or "").strip(), "created_at": utc_now()}


class RecoverableTaskStore:
    """Persistent goal/step/checkpoint/blocker/final-review task state."""

    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS tasks("
            "id TEXT PRIMARY KEY,status TEXT NOT NULL,updated_at TEXT NOT NULL,doc TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_status_updated ON tasks(status,updated_at DESC)"
        )
        self.db.commit()

    def _load(self, task_id):
        task_id = _txt(task_id, "task_id", True, 256)
        row = self.db.execute("SELECT doc FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise BridgeError("task_not_found", "Recoverable task was not found.")
        try:
            return json.loads(row[0])
        except json.JSONDecodeError as exc:
            raise BridgeError("task_state_corrupt", "Stored task state is invalid.") from exc

    def _summary(self, task):
        steps = task.get("steps") or []
        review = task.get("final_review") or {}
        verified = set(review.get("verified_facts") or [])
        remaining = [s["id"] for s in steps if s["status"] != "completed"]
        conditions = task.get("completion_conditions") or []
        unverified = [c for c in conditions if c not in verified]
        current_review = self._review_current(task)
        if task["status"] == "completed":
            next_required = "The recorded task is complete; report verified results and scope."
        elif task["status"] == "blocked":
            next_required = "Resolve the recorded blocker and resume; continue independent authorized work. Do not mark complete."
        elif remaining:
            next_required = "Continue authorized work on remaining steps and checkpoint progress; a stage summary is not final delivery."
        elif not current_review:
            next_required = "Verify every completion condition, resolve missing checks, and submit a passing final_review of the current state."
        else:
            next_required = "Call complete with the current expected_revision before reporting task completion."
        return {
            "id": task["id"],
            "title": task["title"],
            "goal": task["goal"],
            "project": task.get("project", ""),
            "host": task.get("host", ""),
            "status": task["status"],
            "phase": task["phase"],
            "revision": task["revision"],
            "steps_completed": sum(s["status"] == "completed" for s in steps),
            "steps_total": len(steps),
            "current_step_id": task.get("current_step_id", ""),
            "blocker": task.get("blocker", ""),
            "summary": task.get("summary", ""),
            "updated_at": task["updated_at"],
            "remaining_step_ids": remaining,
            "completion_conditions": conditions,
            "unverified_completion_conditions": unverified,
            "review_current": current_review,
            "can_complete": task["status"] == "active" and not remaining and bool(steps or conditions) and current_review,
            "task_completed": task["status"] == "completed",
            "continuation_required": task["status"] == "active",
            "next_required_action": next_required,
        }

    @staticmethod
    def _fingerprint(task):
        state = {key: task.get(key) for key in (
            "goal", "project", "host", "completion_conditions", "steps", "status", "blocker"
        )}
        return hashlib.sha256(json.dumps(state, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    def _review_current(self, task):
        review = task.get("final_review") or {}
        return bool(
            review.get("status") == "pass"
            and review.get("task_revision") == task.get("revision")
            and review.get("state_fingerprint") == self._fingerprint(task)
            and not review.get("missing_checks")
            and review.get("verified_facts")
            and set(task.get("completion_conditions") or []).issubset(review.get("verified_facts") or [])
        )

    @staticmethod
    def _check_revision(task, expected_revision):
        revision = int(task.get("revision", 0))
        if expected_revision is not None and revision != expected_revision:
            raise BridgeError("task_revision_conflict", "Task changed since it was read.",
                              details={"expected_revision": expected_revision, "actual_revision": revision})

    @staticmethod
    def _invalidate_review(task, reason):
        if task.get("final_review") is not None:
            task["events"].append(_event("review_invalidated", reason))
        task["final_review"] = None

    @staticmethod
    def _sync_phase(task):
        remaining = [s for s in task.get("steps") or [] if s["status"] != "completed"]
        task["phase"] = "execute" if remaining else "verify"
        ids = {s["id"] for s in remaining}
        if task.get("current_step_id") not in ids:
            chosen = next((s for s in remaining if s["status"] == "in_progress"), None)
            task["current_step_id"] = (chosen or (remaining[0] if remaining else {})).get("id", "")

    @staticmethod
    def _validate_pass(task, review):
        if task["status"] != "active":
            raise BridgeError("task_blocked", "Resolve the blocker and resume before a passing review or completion.")
        steps = task.get("steps") or []
        incomplete = [s["id"] for s in steps if s["status"] != "completed"]
        if incomplete:
            raise BridgeError("task_incomplete", "All task steps must be completed before closeout.",
                              details={"incomplete_step_ids": incomplete})
        conditions = task.get("completion_conditions") or []
        if not steps and not conditions:
            raise BridgeError("task_completion_scope_required", "Define steps or completion conditions before completion.")
        if review.get("missing_checks"):
            raise BridgeError("task_missing_checks", "Missing checks must be resolved before a passing review or completion.",
                              details={"missing_checks": review["missing_checks"]})
        if not review.get("verified_facts"):
            raise BridgeError("task_review_evidence_required", "Record verified facts before a passing review.")
        missing = [c for c in conditions if c not in review["verified_facts"]]
        if missing:
            raise BridgeError("task_conditions_unverified", "Include each verified completion condition verbatim in verified, with supporting facts.",
                              details={"unverified_completion_conditions": missing})

    def _receipt(self, action, record, **extra):
        summary = self._summary(record)
        return {"action": action, "task_id": record["id"], "task_summary": summary,
                "task_completed": summary["task_completed"],
                "continuation_required": summary["continuation_required"],
                "next_required_action": summary["next_required_action"],
                "state_path": str(self.path), **extra}

    def _save(self, task, expected_revision=None):
        revision = int(task.get("revision", 0))
        self._check_revision(task, expected_revision)
        row = self.db.execute("SELECT doc FROM tasks WHERE id=?", (task["id"],)).fetchone()
        if not row or json.loads(row[0]).get("revision") != revision:
            raise BridgeError("task_revision_conflict", "Task changed since it was read.")
        task["revision"] = revision + 1
        task["updated_at"] = utc_now()
        task["events"] = list(task.get("events") or [])[-256:]
        raw = json.dumps(task, ensure_ascii=False, separators=(",", ":"))
        try:
            cursor = self.db.execute(
                "UPDATE tasks SET status=?,updated_at=?,doc=? WHERE id=? AND doc=?",
                (task["status"], task["updated_at"], raw, task["id"], row[0]),
            )
            if cursor.rowcount != 1:
                raise BridgeError("task_revision_conflict", "Task changed since it was read.")
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return copy.deepcopy(task)

    def create(self, args):
        steps = []
        seen = set()
        for index, item in enumerate(args.get("steps") or [], 1):
            if not isinstance(item, dict):
                raise BridgeError("invalid_arguments", "Each task step must be an object.")
            step_id = _txt(item.get("id") or f"step-{index}", "steps[].id", True, 256)
            if step_id in seen:
                raise BridgeError("invalid_arguments", "Task step ids must be unique.")
            seen.add(step_id)
            steps.append({
                "id": step_id,
                "title": _txt(item.get("title"), "steps[].title", True, 2048),
                "status": "pending",
                "summary": "",
            })
        if len(steps) > 128:
            raise BridgeError("invalid_arguments", "A task cannot contain more than 128 steps.")
        conditions = [
            _txt(item, "completion_conditions[]", True, 4096)
            for item in (args.get("completion_conditions") or [])
        ]
        if len(conditions) > 64:
            raise BridgeError("invalid_arguments", "completion_conditions cannot exceed 64 items.")
        if len(set(conditions)) != len(conditions):
            raise BridgeError("invalid_arguments", "Completion conditions must be unique.")
        now = utc_now()
        task = {
            "schema_version": 1,
            "id": "task_" + uuid.uuid4().hex[:16],
            "title": _txt(args.get("title"), "title", True, 2048),
            "goal": _txt(args.get("goal"), "goal", True),
            "project": _txt(args.get("project"), "project", False, 2048),
            "host": _txt(args.get("host"), "host", False, 256),
            "status": "active",
            "phase": "execute",
            "revision": 1,
            "completion_conditions": conditions,
            "steps": steps,
            "current_step_id": steps[0]["id"] if steps else "",
            "events": [_event("created", "Task created")],
            "blocker": "",
            "summary": "",
            "final_review": None,
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
        }
        with self.lock:
            try:
                self.db.execute(
                    "INSERT INTO tasks(id,status,updated_at,doc) VALUES(?,?,?,?)",
                    (task["id"], task["status"], now, json.dumps(task, ensure_ascii=False, separators=(",", ":"))),
                )
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise
        return self._receipt("create", task)

    def list(self, args):
        status = _txt(args.get("status"), "status", False, 64).lower()
        if status and status not in TASK_STATUSES:
            raise BridgeError("invalid_arguments", "status must be active, blocked, or completed.")
        limit = max(1, min(int(args.get("limit") or 50), 200))
        with self.lock:
            if status:
                rows = self.db.execute(
                    "SELECT doc FROM tasks WHERE status=? ORDER BY updated_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = self.db.execute(
                    "SELECT doc FROM tasks ORDER BY updated_at DESC LIMIT ?", (limit,)
                ).fetchall()
        items = [self._summary(json.loads(row[0])) for row in rows]
        return {"action": "list", "tasks": items, "count": len(items), "state_path": str(self.path)}

    def get(self, args):
        with self.lock:
            task = self._load(args.get("task_id"))
        return self._receipt("get", task, task=task)

    def checkpoint(self, args):
        with self.lock:
            task = self._load(args.get("task_id"))
            if task["status"] == "completed":
                raise BridgeError("task_completed", "Completed tasks cannot be checkpointed.")
            # Scope extension is additive: original required work cannot vanish.
            used = {s["id"] for s in task.get("steps") or []}
            for item in args.get("steps") or []:
                if not isinstance(item, dict):
                    raise BridgeError("invalid_arguments", "Each task step must be an object.")
                number = len(used) + 1
                while f"step-{number}" in used:
                    number += 1
                new_id = _txt(item.get("id") or f"step-{number}", "steps[].id", True, 256)
                if new_id in used:
                    raise BridgeError("invalid_arguments", "Appended task step ids must be new and unique.")
                used.add(new_id)
                task["steps"].append({"id": new_id, "title": _txt(item.get("title"), "steps[].title", True, 2048),
                                      "status": "pending", "summary": ""})
            if len(task.get("steps") or []) > 128:
                raise BridgeError("invalid_arguments", "A task cannot contain more than 128 steps.")
            conditions = list(task.get("completion_conditions") or [])
            for item in args.get("completion_conditions") or []:
                condition = _txt(item, "completion_conditions[]", True, 4096)
                if condition in conditions:
                    raise BridgeError("invalid_arguments", "Appended completion conditions must be new and unique.")
                conditions.append(condition)
            if len(conditions) > 64:
                raise BridgeError("invalid_arguments", "completion_conditions cannot exceed 64 items.")
            task["completion_conditions"] = conditions
            by_id = {s["id"]: s for s in task.get("steps") or []}
            step_id = _txt(args.get("step_id"), "step_id", False, 256)
            step_status = _txt(args.get("step_status"), "step_status", False, 64).lower()
            completed = args.get("completed_step_ids")
            current = _txt(args.get("current_step_id"), "current_step_id", False, 256)
            if bool(step_id or step_status) and (completed is not None or current):
                raise BridgeError("invalid_arguments", "Use single-step or batch checkpoint fields, not both.")
            if step_id or step_status:
                if step_id not in by_id or step_status not in STEP_STATUSES:
                    raise BridgeError("invalid_arguments", "Valid step_id and step_status are required.")
                by_id[step_id]["status"] = step_status
                by_id[step_id]["summary"] = _txt(args.get("summary"), "summary", False, 8192)
                if step_status == "in_progress":
                    task["current_step_id"] = step_id
            else:
                if current and current in (completed or []):
                    raise BridgeError("invalid_arguments", "A completed step cannot also be the current in-progress step.")
                for item in completed or []:
                    if item not in by_id:
                        raise BridgeError("invalid_arguments", f"Unknown completed step id: {item}")
                    by_id[item]["status"] = "completed"
                if current:
                    if current not in by_id:
                        raise BridgeError("invalid_arguments", "current_step_id is unknown.")
                    by_id[current]["status"] = "in_progress"
                    task["current_step_id"] = current
            summary = _txt(args.get("summary"), "summary", False, 8192)
            if summary:
                task["summary"] = summary
            evidence = [_txt(x, "evidence[]", True, 4096) for x in (args.get("evidence") or [])]
            if evidence:
                task["events"].append(_event("evidence", " | ".join(evidence)))
            task["events"].append(_event("checkpoint", summary or "Progress checkpoint"))
            self._invalidate_review(task, "Progress changed; review the current state again.")
            self._sync_phase(task)
            saved = self._save(task, args.get("expected_revision"))
        return self._receipt("checkpoint", saved)

    def block(self, args):
        with self.lock:
            task = self._load(args.get("task_id"))
            summary = _txt(args.get("summary"), "summary", True, 8192)
            if task["status"] == "completed":
                raise BridgeError("task_completed", "Completed tasks cannot be blocked.")
            task["status"] = "blocked"
            task["blocker"] = summary
            task["summary"] = summary
            task["events"].append(_event("blocked", summary))
            self._invalidate_review(task, "A blocker was recorded.")
            self._sync_phase(task)
            saved = self._save(task, args.get("expected_revision"))
        return self._receipt("block", saved)

    def resume(self, args):
        with self.lock:
            task = self._load(args.get("task_id"))
            if task["status"] == "completed":
                raise BridgeError("task_completed", "Completed tasks cannot be resumed.")
            summary = _txt(args.get("summary"), "summary", False, 8192)
            task["status"] = "active"
            task["blocker"] = ""
            if summary:
                task["summary"] = summary
            task["events"].append(_event("resumed", summary or "Task resumed"))
            self._invalidate_review(task, "Task resumed; review the current state again.")
            self._sync_phase(task)
            saved = self._save(task, args.get("expected_revision"))
        return self._receipt("resume", saved)

    def final_review(self, args):
        review_status = _txt(args.get("review_status"), "review_status", True, 64).lower()
        if review_status not in {"pass", "failed"}:
            raise BridgeError("invalid_arguments", "review_status must be pass or failed.")
        with self.lock:
            task = self._load(args.get("task_id"))
            if task["status"] == "completed":
                raise BridgeError("task_completed", "Completed tasks cannot be reviewed again.")
            self._check_revision(task, args.get("expected_revision"))
            summary = _txt(args.get("summary"), "summary", True, 8192)
            review = {
                "status": review_status,
                "summary": summary,
                "verified_facts": [_txt(x, "verified[]", True, 4096) for x in (args.get("verified") or [])],
                "open_risks": [_txt(x, "risks[]", True, 4096) for x in (args.get("risks") or [])],
                "missing_checks": [_txt(x, "missing_checks[]", True, 4096) for x in (args.get("missing_checks") or [])],
                "evidence": [_txt(x, "evidence[]", True, 4096) for x in (args.get("evidence") or [])],
                "reviewed_at": utc_now(),
                "task_revision": task["revision"] + 1,
                "state_fingerprint": self._fingerprint(task),
            }
            if review_status == "pass":
                self._validate_pass(task, review)
            task["final_review"] = review
            task["summary"] = summary
            self._sync_phase(task)
            task["events"].append(_event("final_review", f"{review_status}: {summary}"))
            saved = self._save(task, args.get("expected_revision"))
        return self._receipt("final_review", saved, final_review=review)

    def complete(self, args):
        with self.lock:
            task = self._load(args.get("task_id"))
            self._check_revision(task, args.get("expected_revision"))
            if task["status"] == "completed":
                return self._receipt("complete", task, already_completed=True)
            if task["status"] != "active":
                raise BridgeError("task_blocked", "Resolve the blocker and resume before completion.")
            incomplete = [s["id"] for s in task.get("steps") or [] if s["status"] != "completed"]
            if incomplete:
                raise BridgeError("task_incomplete", "All task steps must be completed before closeout.", details={"incomplete_step_ids": incomplete})
            if (task.get("final_review") or {}).get("status") != "pass":
                raise BridgeError("task_review_required", "A passing final_review is required before complete.")
            if not self._review_current(task):
                raise BridgeError("task_review_stale", "A passing final_review of the current task state is required before complete.")
            self._validate_pass(task, task["final_review"])
            task["status"] = "completed"
            task["phase"] = "closeout"
            task["blocker"] = ""
            task["completed_at"] = utc_now()
            task["events"].append(_event("completed", task.get("summary") or "Task completed"))
            saved = self._save(task, args.get("expected_revision"))
        return self._receipt("complete", saved)

    def manage(self, args):
        action = _txt(args.get("action"), "action", True, 64).lower()
        handlers = {
            "create": self.create,
            "list": self.list,
            "get": self.get,
            "checkpoint": self.checkpoint,
            "block": self.block,
            "resume": self.resume,
            "final_review": self.final_review,
            "complete": self.complete,
        }
        if action not in handlers:
            raise BridgeError("invalid_arguments", "Unsupported task_manage action.", details={"allowed": sorted(handlers)})
        fields = {
            "create": {"title", "goal", "project", "host", "steps", "completion_conditions"},
            "list": {"status", "limit"},
            "get": {"task_id"},
            "checkpoint": {"task_id", "step_id", "step_status", "completed_step_ids", "current_step_id",
                           "summary", "evidence", "steps", "completion_conditions", "expected_revision"},
            "block": {"task_id", "summary", "expected_revision"},
            "resume": {"task_id", "summary", "expected_revision"},
            "final_review": {"task_id", "summary", "review_status", "verified", "risks", "missing_checks",
                             "evidence", "expected_revision"},
            "complete": {"task_id", "expected_revision"},
        }
        unsupported = sorted(set(args) - fields[action] - {"action", "idempotency_key"})
        if unsupported:
            raise BridgeError("invalid_arguments", "Fields are not supported for this task action; nothing was changed.",
                              details={"action": action, "unsupported_fields": unsupported})
        return handlers[action](args)

    def close(self):
        with self.lock:
            self.db.close()
