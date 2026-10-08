"""Silent execution, first-page delivery and final-receipt recovery regressions."""
import asyncio
import base64
import concurrent.futures
import json
import threading
import time
from types import SimpleNamespace

import pytest

from codex_control_mcp.bridge import Bridge
from codex_control_mcp.config import Config
from codex_control_mcp.errors import BridgeError
from codex_control_mcp.results import promote_execution
from codex_control_mcp.server import execute_with_progress
from codex_control_mcp.sessions import Session, SessionStore
from codex_control_mcp.tools import validate_tool


def append(session, data, stream="stdout"):
    session.append({"deltaBase64": base64.b64encode(data).decode(), "stream": stream})


def finish(session, code=0):
    future = concurrent.futures.Future()
    future.set_result({"exitCode": code})
    session.finish(future)


@pytest.mark.parametrize("mode", ["session_start", "exec_command"])
def test_start_receipt_delivers_first_page_without_skipping_early_output(tmp_path, mode):
    bridge = Bridge(Config(home=tmp_path / "home", cwd=str(tmp_path)))
    bridge.ensure_ready = lambda *a, **k: None
    dispatched = []

    def begin(method, args):
        dispatched.append(method)
        session = bridge.sessions.get(args["processId"])
        append(session, ("EARLY中文\n" * 200).encode())
        future = concurrent.futures.Future()
        future.set_result({"exitCode": 0})
        return future

    bridge.rpc = SimpleNamespace(generation="fixture", begin=begin, close=lambda: None)
    try:
        args = {"argv": ["fixture"], "output_limit_bytes": 1024}
        if mode == "exec_command":
            args["execution_mode"] = "session"
        receipt = bridge._do(mode, args)
        text = receipt.get("stdout", "")
        cursor = receipt["next_cursor"]
        while receipt.get("next_action"):
            receipt = bridge._do("session_read", receipt["next_action"]["arguments"])
            text += receipt["stdout"]
        assert text == "EARLY中文\n" * 200
        assert cursor < receipt["next_cursor"]
        assert receipt["exit_code"] == 0 and receipt["final_receipt_ready"]
        assert dispatched == ["command/exec"]
    finally:
        bridge.close()


def test_exit_is_not_final_delivery_until_output_pages_are_drained():
    session = Session("fixture", "g", ".", 16384)
    append(session, b"x" * 6000 + b"FINAL_SUMMARY\n")
    finish(session)
    first = session.read(max_bytes=1024, output_format="text")
    assert first["process_completed"] and not first["completed"]
    assert first["continuation_required"] and not first["final_receipt_ready"]
    assert "输出" in first["status_message"]
    receipt, output = first, first["stdout"]
    while receipt["continuation_required"]:
        args = dict(receipt["next_action"]["arguments"])
        args.pop("session_id")
        receipt = session.read(**args)
        output += receipt["stdout"]
    assert output.endswith("FINAL_SUMMARY\n")
    assert receipt["completed"] and receipt["final_receipt_ready"]
    assert receipt["result_status"] == "succeeded"


@pytest.mark.parametrize("event", ["output", "exit", "lost"])
def test_bounded_read_wakes_on_output_exit_or_connection_loss(event):
    session = Session("fixture", "g", ".", 4096)
    def change():
        if event == "output":
            append(session, b"NOW\n")
        elif event == "exit":
            finish(session, 7)
        else:
            future = concurrent.futures.Future()
            future.set_exception(BridgeError("execution_state_unknown", "fixture"))
            session.finish(future)
    timer = threading.Timer(0.08, change)
    timer.start()
    try:
        started = time.monotonic()
        receipt = session.read(output_format="text", wait_ms=1000)
        assert 0.04 <= time.monotonic() - started < 0.8
        assert receipt["stdout"] == ("NOW\n" if event == "output" else "")
        assert receipt["heartbeat"]["observed_at"]
        if event == "lost":
            assert receipt["result_status"] == "unknown"
            assert not receipt["process_completed"] and not receipt["final_receipt_ready"]
            assert receipt["next_action"] is None
    finally:
        timer.join()


def test_silent_read_is_bounded_and_does_not_fabricate_stdout():
    session = Session("fixture", "g", ".", 4096)
    started = time.monotonic()
    receipt = session.read(output_format="text", wait_ms=80)
    assert 0.05 <= time.monotonic() - started < 0.8
    assert receipt["stdout"] == receipt["stderr"] == ""
    assert receipt["heartbeat"]["output_idle_ms"] >= 50
    assert receipt["continuation_required"] and not receipt["final_receipt_ready"]
    assert receipt["next_action"]["arguments"]["wait_ms"] == 80


def test_unread_output_and_terminal_results_never_wait():
    session = Session("fixture", "g", ".", 4096)
    append(session, b"ALREADY_HERE")
    started = time.monotonic()
    first = session.read(wait_ms=1000)
    finish(session)
    final = session.read(first["next_cursor"], wait_ms=1000)
    assert time.monotonic() - started < 0.4
    assert first["stdout"] == "ALREADY_HERE" and final["final_receipt_ready"]


def test_poll_request_sends_real_progress_while_program_is_silent(monkeypatch):
    import codex_control_mcp.server as module
    monkeypatch.setattr(module, "PROGRESS_INTERVAL_SECONDS", 0.02)
    session = Session("fixture", "g", ".", 4096)
    bridge = Bridge.__new__(Bridge)
    bridge.sessions = SimpleNamespace(get=lambda sid: session)
    def execute(name, args):
        return {"ok": True, "result": bridge._do(name, args)}
    bridge.execute = execute
    notifications = []
    async def notify(token, progress, **kwargs):
        notifications.append((progress, kwargs["message"]))
    context = SimpleNamespace(meta=SimpleNamespace(progressToken="owned"), request_id=1,
                              session=SimpleNamespace(send_progress_notification=notify))
    result = asyncio.run(execute_with_progress(bridge, context, "session_read",
        {"session_id": "fixture", "wait_ms": 100}))
    assert len(notifications) >= 2
    assert all(a[0] < b[0] for a, b in zip(notifications, notifications[1:]))
    assert result["result"]["stdout"] == "" and result["result"]["continuation_required"]
    assert result["progress_observation"]["client_display_confirmed"] is False


@pytest.mark.parametrize("exit_code", [None, True, "0", 0.5])
def test_malformed_exit_code_does_not_claim_finished_success(exit_code):
    session = Session("fixture", "g", ".", 4096)
    finish(session, exit_code)
    assert session.state == "lost" and session.error["code"] == "execution_state_unknown"
    receipt = session.read()
    assert receipt["result_status"] == "unknown" and not receipt["final_receipt_ready"]


@pytest.mark.parametrize("code", [0, 7])
def test_historical_terminal_exit_is_readable_without_reattaching_or_inventing_output(tmp_path, code):
    path = tmp_path / "sessions.json"
    first = SessionStore(path, 4096, 4)
    session = first.create("old", str(tmp_path))
    append(session, b"SAVED_ELSEWHERE")
    finish(session, code)
    first.save()
    second = SessionStore(path, 4096, 4)
    restored = second.get(session.id)
    receipt = restored.read(output_format="text")
    assert receipt["exit_code"] == code and receipt["process_completed"]
    assert receipt["history_only"] and not receipt["reconnectable"]
    assert not receipt["output_complete"] and not receipt["final_receipt_ready"]
    assert receipt["stdout"] == "" and receipt["cursor_gap"]
    assert receipt["recovery_warning"]["code"] == "session_output_unavailable"
    with pytest.raises(BridgeError):
        second.get(session.id, "new")


def test_silent_terminal_receipt_survives_restart_without_output_loss(tmp_path):
    path = tmp_path / "sessions.json"
    first = SessionStore(path, 4096, 4)
    session = first.create("old", str(tmp_path))
    finish(session)
    first.save()
    receipt = SessionStore(path, 4096, 4).get(session.id).read()
    assert receipt["exit_code"] == 0 and receipt["final_receipt_ready"]


def test_previous_active_session_stays_unknown_after_restart(tmp_path):
    path = tmp_path / "sessions.json"
    first = SessionStore(path, 4096, 4)
    session = first.create("old", str(tmp_path))
    receipt = SessionStore(path, 4096, 4).get(session.id).read()
    assert receipt["state"] == "lost" and not receipt["process_completed"]
    assert receipt["result_status"] == "unknown" and not receipt["final_receipt_ready"]


def test_routed_receipt_preserves_drain_and_heartbeat_contract():
    inner = {"process_completed": True, "completed": False, "continuation_required": True,
             "final_receipt_ready": False, "output_complete": False, "result_status": "succeeded",
             "heartbeat": {"observed_at": "fixture", "output_idle_ms": 50}}
    result = promote_execution({"result": {"structuredContent": {"result": inner}}})
    assert all(result.get(k) == v for k, v in inner.items())


@pytest.mark.parametrize("value", [-1, 10001, True, 0.1])
def test_wait_validation_rejects_bad_values(value):
    with pytest.raises(BridgeError):
        validate_tool("session_read", {"session_id": "fixture", "wait_ms": value})
    with pytest.raises(BridgeError):
        Session("fixture", "g", ".", 4096).read(wait_ms=value)


def test_wait_zero_is_supported_for_immediate_observation():
    validate_tool("session_read", {"session_id": "fixture", "wait_ms": 0})
    receipt = Session("fixture", "g", ".", 4096).read(wait_ms=0)
    assert receipt["continuation_required"]


def test_immediately_failed_session_start_preserves_top_level_failure(tmp_path):
    bridge = Bridge(Config(home=tmp_path / "home", cwd=str(tmp_path)))
    bridge.ensure_ready = lambda *a, **k: None
    def begin(method, args):
        future = concurrent.futures.Future()
        future.set_result({"exitCode": 7})
        return future
    bridge.rpc = SimpleNamespace(generation="fixture", begin=begin, close=lambda: None)
    try:
        result = bridge.execute("session_start", {"argv": ["fixture"]})
        assert not result["ok"] and result["error"]["code"] == "command_failed"
        assert result["result"]["exit_code"] == 7 and result["result"]["final_receipt_ready"]
    finally:
        bridge.close()
