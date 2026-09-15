from __future__ import annotations

import copy
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
        }

    def _save(self, task, expected_revision=None):
        revision = int(task.get("revision", 0))
        if expected_revision is not None and revision != expected_revision:
            raise BridgeError(
                "task_revision_conflict",
                "Task changed since it was read.",
                details={"expected_revision": expected_revision, "actual_revision": revision},
            )
        task["revision"] = revision + 1
        task["updated_at"] = utc_now()
        task["events"] = list(task.get("events") or [])[-256:]
        raw = json.dumps(task, ensure_ascii=False, separators=(",", ":"))
        self.db.execute(
            "UPDATE tasks SET status=?,updated_at=?,doc=? WHERE id=?",
            (task["status"], task["updated_at"], raw, task["id"]),
        )
        self.db.commit()
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
            self.db.execute(
                "INSERT INTO tasks(id,status,updated_at,doc) VALUES(?,?,?,?)",
                (task["id"], task["status"], now, json.dumps(task, ensure_ascii=False, separators=(",", ":"))),
            )
            self.db.commit()
        return {
            "action": "create",
            "task_id": task["id"],
            "task_summary": self._summary(task),
            "state_path": str(self.path),
            "next_required_action": "Checkpoint meaningful progress; final_review must pass before complete.",
        }

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
        return {"action": "get", "task": task, "state_path": str(self.path)}

    def checkpoint(self, args):
        with self.lock:
            task = self._load(args.get("task_id"))
            if task["status"] == "completed":
                raise BridgeError("task_completed", "Completed tasks cannot be checkpointed.")
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
            if task.get("steps") and all(s["status"] == "completed" for s in task["steps"]):
                task["phase"] = "verify"
                task["current_step_id"] = ""
            saved = self._save(task, args.get("expected_revision"))
        return {"action": "checkpoint", "task_id": saved["id"], "task_summary": self._summary(saved), "state_path": str(self.path)}

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
            saved = self._save(task, args.get("expected_revision"))
        return {"action": "block", "task_id": saved["id"], "task_summary": self._summary(saved), "state_path": str(self.path)}

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
            saved = self._save(task, args.get("expected_revision"))
        return {"action": "resume", "task_id": saved["id"], "task_summary": self._summary(saved), "state_path": str(self.path)}

    def final_review(self, args):
        review_status = _txt(args.get("review_status"), "review_status", True, 64).lower()
        if review_status not in {"pass", "failed"}:
            raise BridgeError("invalid_arguments", "review_status must be pass or failed.")
        with self.lock:
            task = self._load(args.get("task_id"))
            if task["status"] == "completed":
                raise BridgeError("task_completed", "Completed tasks cannot be reviewed again.")
            summary = _txt(args.get("summary"), "summary", True, 8192)
            review = {
                "status": review_status,
                "summary": summary,
                "verified_facts": [_txt(x, "verified[]", True, 4096) for x in (args.get("verified") or [])],
                "open_risks": [_txt(x, "risks[]", True, 4096) for x in (args.get("risks") or [])],
                "missing_checks": [_txt(x, "missing_checks[]", True, 4096) for x in (args.get("missing_checks") or [])],
                "reviewed_at": utc_now(),
            }
            task["final_review"] = review
            task["summary"] = summary
            task["phase"] = "closeout" if review_status == "pass" else "verify"
            task["events"].append(_event("final_review", f"{review_status}: {summary}"))
            saved = self._save(task, args.get("expected_revision"))
        return {"action": "final_review", "task_id": saved["id"], "task_summary": self._summary(saved), "final_review": review, "state_path": str(self.path)}

    def complete(self, args):
        with self.lock:
            task = self._load(args.get("task_id"))
            if task["status"] == "completed":
                return {"action": "complete", "task_id": task["id"], "task_summary": self._summary(task), "already_completed": True, "state_path": str(self.path)}
            incomplete = [s["id"] for s in task.get("steps") or [] if s["status"] != "completed"]
            if incomplete:
                raise BridgeError("task_incomplete", "All task steps must be completed before closeout.", details={"incomplete_step_ids": incomplete})
            if (task.get("final_review") or {}).get("status") != "pass":
                raise BridgeError("task_review_required", "A passing final_review is required before complete.")
            task["status"] = "completed"
            task["phase"] = "closeout"
            task["blocker"] = ""
            task["completed_at"] = utc_now()
            task["events"].append(_event("completed", task.get("summary") or "Task completed"))
            saved = self._save(task, args.get("expected_revision"))
        return {"action": "complete", "task_id": saved["id"], "task_summary": self._summary(saved), "state_path": str(self.path)}

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
        return handlers[action](args)

    def close(self):
        with self.lock:
            self.db.close()
