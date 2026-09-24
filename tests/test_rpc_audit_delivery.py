"""Audit failure regression tests using in-memory protocol fixtures, not production."""
from __future__ import annotations

import concurrent.futures
import io
import json
import threading
from types import SimpleNamespace

import pytest

from codex_control_mcp.diagnostics import CURRENT_TRACE, CallTrace
from codex_control_mcp.errors import BridgeError
from codex_control_mcp.rpc import AppServer


def fixture(failing_event: str | None = None):
    rows, writes = [], []

    def emit(event, **fields):
        if event == failing_event:
            raise OSError("PRIVATE_AUDIT_ERROR")
        rows.append({"event": event, **fields})

    app = AppServer.__new__(AppServer)
    app.lock = threading.RLock()
    app.write_lock = threading.Lock()
    app.closed = False
    app.broken = False
    app.generation = "test-generation"
    app.next_id = 0
    app.pending = {}
    app.audit = SimpleNamespace(emit=emit)
    app.schema = SimpleNamespace(validate=lambda *a: None, validate_response=lambda *a: None)
    app.proc = SimpleNamespace(poll=lambda: None, stdout=io.BytesIO())
    app.callback = lambda message: None
    app._write = lambda message: writes.append(message)
    return app, rows, writes


def responses(app, frames):
    app.proc.stdout = io.BytesIO(b"".join(json.dumps(x).encode() + b"\n" for x in frames))
    app._read()


@pytest.mark.parametrize("kind", ["result", "error"])
def test_response_audit_failure_does_not_orphan_future(kind):
    app, rows, writes = fixture("rpc_" + kind)
    trace = CallTrace("fixture-operation")
    token = CURRENT_TRACE.set(trace)
    try:
        future = app.begin("fs/readFile", {"path": "test-owned"})
    finally:
        CURRENT_TRACE.reset(token)
    frame = {"id": 1, "result": {"content": "test-value"}} if kind == "result" else {
        "id": 1, "error": {"code": -32603, "message": "PRIVATE_BACKEND_ERROR"}}
    responses(app, [frame])
    assert future.done(), "Received official response must always settle its future"
    if kind == "result":
        assert future.result() == {"content": "test-value"}
    else:
        with pytest.raises(BridgeError) as caught:
            future.result()
        assert caught.value.details["rpc_code"] == -32603
        assert "PRIVATE" not in str(caught.value)
    evidence = trace.receipt({"ok": kind == "result"})
    assert evidence["rpc_response_count"] == 1
    assert evidence["rpc_audit_error_count"] == 1
    assert evidence["rpc_audit_status"] == "degraded"
    assert len(writes) == 1


def test_sibling_response_survives_result_logging_failure():
    app, rows, writes = fixture("rpc_result")
    first = app.begin("fs/readFile", {})
    second = app.begin("fs/readDirectory", {})
    responses(app, [{"id": 1, "result": {"n": 1}}, {"id": 2, "result": {"n": 2}}])
    assert first.done() and second.done()
    assert first.result() == {"n": 1} and second.result() == {"n": 2}
    assert len(writes) == 2 and not app.pending


def test_pre_dispatch_audit_failure_prevents_send():
    app, rows, writes = fixture("rpc_dispatch_intent")
    with pytest.raises(BridgeError) as caught:
        app.begin("command/exec", {"command": ["test-only"]})
    assert caught.value.code == "audit_unavailable"
    assert caught.value.details["origin"] == "bridge_runtime"
    assert not writes and not app.pending


def test_post_send_audit_failure_preserves_pending_result():
    app, rows, writes = fixture("rpc_send")
    trace = CallTrace("fixture-operation")
    token = CURRENT_TRACE.set(trace)
    try:
        future = app.begin("command/exec", {"command": ["test-only"]})
    finally:
        CURRENT_TRACE.reset(token)
    assert len(writes) == 1 and len(app.pending) == 1
    responses(app, [{"id": 1, "result": {"exitCode": 0}}])
    assert future.done() and future.result()["exitCode"] == 0
    evidence = trace.receipt({"ok": True})
    assert evidence["rpc_dispatched_count"] == 1 and evidence["rpc_audit_error_count"] == 1


def test_unhandled_callback_stays_rejected_when_logging_fails():
    app, rows, writes = fixture("unexpected_callback")
    responses(app, [{"id": "callback-fixture", "method": "unsupported/request", "params": {}}])
    assert len(writes) == 1 and writes[0]["error"]["code"] == -32601


def test_disconnect_audit_failure_cannot_hide_transport_error():
    app, rows, writes = fixture("appserver_disconnect")
    future = app.begin("fs/readFile", {})
    responses(app, [])
    assert future.done()
    with pytest.raises(BridgeError) as caught:
        future.result()
    assert caught.value.code == "execution_state_unknown"
    assert caught.value.details["origin"] == "execution_transport"


def test_notification_error_logging_does_not_break_next_response():
    app, rows, writes = fixture("notification_handler_error")
    def fail(message):
        raise ValueError("test notification failure")
    app.callback = fail
    future = app.begin("fs/readDirectory", {})
    responses(app, [{"method": "test/notification", "params": {}}, {"id": 1, "result": {}}])
    assert future.done() and future.result() == {}
