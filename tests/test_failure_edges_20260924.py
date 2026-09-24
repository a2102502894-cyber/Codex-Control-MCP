"""Reproduce receipt loss and maintenance races in test-owned state only."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import runpy
import sys
import threading
from types import SimpleNamespace

import pytest

from codex_control_mcp.bridge import Bridge
from codex_control_mcp.config import Config
from codex_control_mcp.http_observation import (
    HTTP_OBSERVATION, HTTPObservation, attach_transport_receipt, observe_http,
)
from codex_control_mcp.maintenance import MaintenanceError, submit_request


def request_scope():
    return {"type": "http", "path": "/mcp", "method": "POST", "headers": []}


async def receive():
    return {"type": "http.request", "body": b"fixture", "more_body": False}


@pytest.mark.parametrize("event", ["http_response_started", "http_finished"])
def test_late_audit_failure_preserves_completed_http_response(event):
    calls, sent = [], []
    def emit(name, **fields):
        if name == event:
            raise OSError("PRIVATE_DISK_ERROR")
    async def app(scope, read, send):
        calls.append("executed-once")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"actual-result"})
    async def send(message):
        sent.append(message)
    asyncio.run(observe_http(SimpleNamespace(emit=emit), app, request_scope(), receive, send))
    assert calls == ["executed-once"]
    assert sent[-1]["body"] == b"actual-result"
    assert HTTP_OBSERVATION.get() is None


def test_late_logging_error_does_not_replace_original_send_exception():
    error = ConnectionError("ORIGINAL_SEND_ERROR")
    def emit(name, **fields):
        if name in {"http_dispatch_exception", "http_finished"}:
            raise OSError("SECONDARY_LOG_ERROR")
    async def app(scope, read, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
    async def send(message):
        raise error
    with pytest.raises(ConnectionError) as caught:
        asyncio.run(observe_http(SimpleNamespace(emit=emit), app, request_scope(), receive, send))
    assert caught.value is error and HTTP_OBSERVATION.get() is None


def test_receipt_attachment_retains_result_and_reports_failed_audit():
    observation = HTTPObservation("a" * 32)
    token = HTTP_OBSERVATION.set(observation)
    def emit(*args, **kwargs):
        raise OSError("PRIVATE_LOG_ERROR")
    out = {"ok": True, "operation_id": "b" * 32, "result": {"exit_code": 0}}
    try:
        attach_transport_receipt(out, SimpleNamespace(emit=emit))
    finally:
        HTTP_OBSERVATION.reset(token)
    assert out["ok"] and out["result"]["exit_code"] == 0
    assert out["transport_observation"]["audit_status"] == "degraded"
    assert "PRIVATE_LOG_ERROR" not in json.dumps(out)


def test_bridge_finish_log_failure_preserves_result_and_idempotency(tmp_path):
    bridge = Bridge(Config(home=tmp_path / "home", cwd=str(tmp_path)))
    calls = []
    original = bridge.audit.emit
    def emit(event, **fields):
        if event == "tool_finish":
            raise OSError("PRIVATE_LOG_ERROR")
        return original(event, **fields)
    bridge.audit.emit = emit
    bridge._do = lambda *args: calls.append(1) or {"exit_code": 0, "stdout": "done", "stderr": ""}
    args = {"argv": ["test-owned-not-launched"], "idempotency_key": "one-fixture"}
    try:
        first = bridge.execute("exec_command", args)
        second = bridge.execute("exec_command", args)
        assert first["ok"] and second["ok"] and calls == [1]
        assert second["idempotent_replay"]
        assert first["diagnostics"]["audit_recording"]["status"] == "degraded"
        assert "PRIVATE_LOG_ERROR" not in json.dumps(first)
    finally:
        bridge.audit.emit = original
        bridge.close()


def test_idempotency_finish_failure_does_not_erase_execution_result(tmp_path):
    bridge = Bridge(Config(home=tmp_path / "home", cwd=str(tmp_path)))
    calls = []
    bridge._do = lambda *args: calls.append(1) or {"exit_code": 0, "stdout": "done"}
    def fail(*args):
        raise OSError("PRIVATE_DATABASE_ERROR")
    bridge.idempotency.finish = fail
    args = {"argv": ["test-owned-not-launched"], "idempotency_key": "one-fixture"}
    try:
        out = bridge.execute("exec_command", args)
        assert out["ok"] and out["result"]["exit_code"] == 0
        assert out["diagnostics"]["idempotency_persistence"]["status"] == "failed"
        second = bridge.execute("exec_command", args)
        assert not second["ok"] and calls == [1]
        assert "PRIVATE_DATABASE_ERROR" not in json.dumps(out)
    finally:
        bridge.close()


@pytest.fixture
def maintenance_home(tmp_path):
    home = tmp_path / "home"
    state = home / "state"
    state.mkdir(parents=True)
    (state / "core-controller-config.json").write_text(json.dumps({
        "home": str(home), "request": str(state / "core-controller-requests")
    }), encoding="utf-8")
    return home


def test_concurrent_restart_requests_dispatch_only_once(maintenance_home):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def run(arg):
        calls.append(1)
        entered.set()
        assert release.wait(3)
    task = SimpleNamespace(Enabled=True, State=3, Run=run)
    def submit():
        try:
            return submit_request(maintenance_home, task)
        except MaintenanceError as exc:
            return {"ok": False, "code": exc.code}
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(submit)
        assert entered.wait(2)
        second = pool.submit(submit)
        try:
            result = second.result(timeout=2)
            assert not result["ok"] and result["code"] == "controller_busy"
        finally:
            release.set()
        assert first.result(timeout=2)["accepted"]
    assert calls == [1]


def test_ready_task_with_pending_request_cannot_queue_another(maintenance_home):
    calls = []
    task = SimpleNamespace(Enabled=True, State=3, Run=calls.append)
    first = submit_request(maintenance_home, task)
    with pytest.raises(MaintenanceError) as caught:
        submit_request(maintenance_home, task)
    assert caught.value.code == "pending_request_exists"
    assert len(calls) == 1
    queue = maintenance_home / "state/core-controller-requests"
    assert [p.stem for p in queue.glob("*.json")] == [first["request_id"]]


@pytest.fixture
def controller():
    if sys.platform != "win32":
        pytest.skip("Windows controller uses msvcrt")
    return runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/core_recovery_controller.py"))


@pytest.mark.parametrize("kind", ["future", "oversized", "bad_schema"])
def test_invalid_maintenance_record_never_becomes_restart(tmp_path, controller, kind):
    queue = tmp_path / "queue"
    queue.mkdir()
    record = {"schema": 1, "operation": "restart", "request_id": "c" * 32,
              "requested_by_pid": 1, "created_at": datetime.now(timezone.utc).isoformat()}
    if kind == "future":
        record["created_at"] = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    elif kind == "oversized":
        record["unused"] = "x" * 100000
    else:
        record["schema"] = 999
    (queue / (record["request_id"] + ".json")).write_text(json.dumps(record), "utf-8")
    mode, requests = controller["consume_requests"](queue)
    assert mode == "invalid" and not requests


def test_initial_bridge_log_failure_does_not_execute(tmp_path):
    bridge = Bridge(Config(home=tmp_path / "home", cwd=str(tmp_path)))
    calls = []
    bridge._do = lambda *args: calls.append(1)
    original = bridge.audit.emit
    def emit(*args, **kwargs):
        raise OSError("PRIVATE_AUDIT_ERROR")
    bridge.audit.emit = emit
    try:
        result = bridge.execute("exec_command", {"argv": ["fixture-not-launched"]})
        assert not result["ok"] and not calls
    finally:
        bridge.audit.emit = original
        bridge.close()


def test_audit_write_failure_remains_visible_after_recovery(tmp_path):
    from codex_control_mcp.common import Audit
    path = tmp_path / "audit.jsonl"
    audit = Audit(path)
    audit.emit("before")
    path.unlink()
    path.mkdir()
    with pytest.raises(OSError):
        audit.emit("failed_write")
    failed = audit.observation()
    assert failed["write_failures"] == 1 and failed["last_write_status"] == "failed"
    path.rmdir()
    audit.emit("recovered")
    recovered = audit.observation()
    assert recovered["write_failures"] == 1 and recovered["last_write_status"] == "recorded"
    assert recovered["last_failure"]["error_type"] in {"PermissionError", "IsADirectoryError"}
    assert str(tmp_path) not in json.dumps(recovered)


def test_final_http_logging_failure_is_retained_in_audit_health(tmp_path):
    from codex_control_mcp.common import Audit
    path = tmp_path / "audit.jsonl"
    audit = Audit(path)
    sent = []
    async def send(message):
        sent.append(message)
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"executed-result"})
        path.unlink()
        path.mkdir()
    asyncio.run(observe_http(audit, app, request_scope(), receive, send))
    assert sent[-1]["body"] == b"executed-result"
    assert audit.observation()["write_failures"] == 1
    assert HTTP_OBSERVATION.get() is None
