"""Output integrity, no-token status, failure telemetry and bounded MCP pages."""
import asyncio
import base64
import concurrent.futures
import json
from types import SimpleNamespace

import pytest

from codex_control_mcp.bridge import Bridge
from codex_control_mcp.sessions import Session
from codex_control_mcp.server import execute_with_progress
from codex_control_mcp.http_observation import observe_http, observe_rpc_body
from codex_control_mcp.tools import validate_tool
from codex_control_mcp.dynamic_mcp import DynamicMCPManager
from codex_control_mcp.errors import BridgeError


def append(session, data, stream="stdout"):
    session.append({"deltaBase64": base64.b64encode(data).decode(), "stream": stream})


def finish(session, code=0):
    future = concurrent.futures.Future()
    future.set_result({"exitCode": code})
    session.finish(future)


def test_default_bridge_page_is_compact_and_all_bytes_are_recoverable():
    session = Session("fixture", "g", ".", 1048576)
    data = ("中文😀\n" * 20000).encode()
    append(session, data)
    finish(session)
    bridge = Bridge.__new__(Bridge)
    bridge.sessions = SimpleNamespace(get=lambda sid: session)
    cursor = 0
    pages = []
    while True:
        page = bridge._do("session_read", {"session_id": "fixture", "cursor": cursor})
        assert "chunks" not in page and "data_base64" not in json.dumps(page)
        assert page["page_bytes"] <= 32768
        pages.append(page["stdout"])
        if not page["has_more"]:
            assert page["next_action"] is None
            break
        assert page["next_cursor"] > cursor
        cursor = page["next_cursor"]
    assert "".join(pages).encode() == data


@pytest.mark.parametrize("output_format", ["text", "chunks", "raw", "legacy"])
def test_output_modes_preserve_streams_and_raw_bytes(output_format):
    session = Session("fixture", "g", ".", 4096)
    append(session, b"out\x80", "stdout")
    append(session, "错误".encode(), "stderr")
    page = session.read(output_format=output_format)
    assert page["next_action"]["arguments"]["cursor"] == 2
    if output_format == "raw":
        assert base64.b64decode(page["chunks"][0]["data_base64"]) == b"out\x80"
        assert "stdout" not in page and "text" not in page["chunks"][0]
    elif output_format == "chunks":
        assert page["chunks"][1]["text"] == "错误"
        assert "stdout" not in page and "data_base64" not in page["chunks"][0]
    else:
        assert page["stderr"] == "错误" and page["stdout"] == "out�"


def test_tiny_chunks_do_not_create_an_unbounded_response():
    session = Session("fixture", "g", ".", 100000)
    for _ in range(3000):
        append(session, b"x")
    page = session.read(output_format="legacy")
    assert len(page["chunks"]) == 1024 and page["has_more"]
    assert not page["cursor_gap"]


def test_elapsed_time_stops_at_completion_and_deadline_is_explicit():
    session = Session("fixture", "g", ".", 4096, timeout_ms=1234)
    finish(session, 124)
    first = session.metadata()
    session.started_monotonic -= 1
    second = session.metadata()
    assert first["effective_timeout_ms"] == 1234
    assert second["elapsed_ms"] == first["elapsed_ms"] + 1000
    assert session.finished_monotonic is not None


def test_no_progress_token_still_returns_an_observable_receipt():
    calls = []
    context = SimpleNamespace(meta=None)
    bridge = SimpleNamespace(execute=lambda *args: calls.append(args) or {"ok": True})
    result = asyncio.run(execute_with_progress(bridge, context, "fixture", {}))
    assert len(calls) == 1
    assert result["progress_observation"] == {"requested": False, "sent": 0, "failed_or_timed_out": 0, "client_display_confirmed": False}


def test_failed_progress_and_summary_logging_do_not_lose_command_result(monkeypatch):
    import codex_control_mcp.server as module
    monkeypatch.setattr(module, "PROGRESS_INTERVAL_SECONDS", 0.005)
    import time
    def execute(*args):
        time.sleep(0.03)
        return {"ok": True, "result": {"exit_code": 0}}
    async def broken(*args, **kwargs):
        raise OSError("PRIVATE_PROGRESS_PAYLOAD")
    def emit(event, **fields):
        if event == "mcp_progress_summary":
            raise OSError("PRIVATE_AUDIT_PAYLOAD")
    bridge = SimpleNamespace(execute=execute, audit=SimpleNamespace(emit=emit))
    context = SimpleNamespace(meta=SimpleNamespace(progressToken=0), request_id=1, session=SimpleNamespace(send_progress_notification=broken))
    result = asyncio.run(execute_with_progress(bridge, context, "fixture", {}))
    assert result["ok"] and result["progress_observation"]["failed_or_timed_out"] > 0
    assert result["progress_observation"]["audit_status"] == "degraded"
    assert "PRIVATE" not in json.dumps(result)


def test_rpc_and_http_telemetry_are_bounded_and_do_not_capture_payloads():
    rows = []
    audit = SimpleNamespace(emit=lambda event, **fields: rows.append({"event": event, **fields}))
    async def receive():
        return {"type": "http.disconnect"}
    async def send(message):
        pass
    async def app(scope, receive, send):
        observe_rpc_body(b'{"method":"tools/call","params":{"PRIVATE_SECRET":"value"}}')
        await receive()
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b"PRIVATE_RESULT"})
    scope = {"type": "http", "path": "/mcp", "method": "POST", "headers": [(b"mcp-protocol-version", b"PRIVATE_HEADER")]}
    asyncio.run(observe_http(audit, app, scope, receive, send))
    assert rows[-1]["rpc_method"] == "tools/call" and rows[-1]["disconnected"]
    assert rows[-1]["response_bytes"] == 14
    assert rows[0]["protocol_version"] == "missing_or_invalid" or rows[0]["protocol_version"] == "invalid"
    assert "PRIVATE" not in json.dumps(rows)


def test_session_format_validation_keeps_legacy_available():
    validate_tool("session_read", {"session_id": "fixture", "output_format": "legacy"})
    with pytest.raises(Exception):
        validate_tool("session_read", {"session_id": "fixture", "output_format": "unknown"})


@pytest.mark.parametrize("policy,expected", [(None, "Stop"), ("continue", "Continue")])
def test_powershell_cmdlet_errors_fail_fast_with_explicit_legacy_option(policy, expected):
    bridge = Bridge.__new__(Bridge)
    arguments = {"command": "Write-Error 'OWNED_ERROR'"}
    if policy:
        arguments["shell_error_policy"] = policy
    argv = bridge._argv(arguments)
    script = base64.b64decode(argv[-1]).decode("utf-16-le")
    assert f"$ErrorActionPreference='{expected}'" in script
    assert "$ProgressPreference='SilentlyContinue'" in script
    assert script.endswith(arguments["command"])


@pytest.mark.parametrize("mutating,code,retryable", [(False, "mcp_unavailable", True), (True, "execution_state_unknown", False)])
def test_dynamic_mcp_overall_deadline_and_no_mutation_replay(mutating, code, retryable):
    manager = DynamicMCPManager.__new__(DynamicMCPManager)
    calls = []
    async def stalled(item, operation):
        calls.append(1)
        await asyncio.sleep(10)
    manager._with_session = stalled
    with pytest.raises(BridgeError) as caught:
        manager._run({"timeout_ms": 20}, None, mutating=mutating)
    assert caught.value.code == code and caught.value.retryable == retryable
    assert calls == [1]


def test_real_stdio_tool_that_never_answers_times_out_once(tmp_path):
    import sys
    import time
    script = tmp_path / "hanging_mcp.py"
    marker = tmp_path / "dispatch-count.txt"
    script.write_text('''import json,sys,time,pathlib
marker=pathlib.Path(sys.argv[1])
for line in sys.stdin:
    message=json.loads(line)
    if message.get('method')=='initialize':
        result={'protocolVersion':'2025-11-25','capabilities':{'tools':{}},'serverInfo':{'name':'owned-hanging-fixture','version':'1'}}
        print(json.dumps({'jsonrpc':'2.0','id':message['id'],'result':result}),flush=True)
    elif message.get('method')=='tools/call':
        marker.write_text('1')
        time.sleep(30)
''', 'utf-8')
    manager = DynamicMCPManager(SimpleNamespace(home=tmp_path, cwd=str(tmp_path)))
    manager.servers['owned'] = {'name':'owned','transport':'stdio','command':sys.executable,'args':[str(script),str(marker)],'timeout_ms':1500,'enabled':True,'tools':[{'name':'hang','inputSchema':{'type':'object'}}]}
    started = time.monotonic()
    with pytest.raises(BridgeError) as caught:
        manager.call({'name':'owned:hang','arguments':{}})
    assert caught.value.code == 'execution_state_unknown' and not caught.value.retryable
    assert time.monotonic() - started < 12
    assert marker.read_text() == '1'
