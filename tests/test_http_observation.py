"""HTTP diagnostics tests; all data and applications are test-owned."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from codex_control_mcp.http_guard import OwnerHTTP
from codex_control_mcp.http_observation import (
    HTTP_OBSERVATION, attach_transport_receipt, observe_http,
)
from codex_control_mcp.server import make_server


def logger():
    rows = []
    return SimpleNamespace(emit=lambda event, **fields: rows.append({"event": event, **fields})), rows


def scope():
    return {"type": "http", "path": "/mcp", "method": "POST", "headers": []}


async def receive():
    return {"type": "http.request", "body": b"PRIVATE_BODY", "more_body": False}


def test_unchanged_body_and_only_server_generated_identifier():
    audit, rows = logger()
    sent = []
    async def send(value):
        sent.append(value)
    async def app(s, r, w):
        await r()
        await w({"type": "http.response.start", "status": 200,
                 "headers": [(b"x-ccm-request-id", b"not-trusted")]})
        await w({"type": "http.response.body", "body": b"PRIVATE_RESULT"})
    asyncio.run(observe_http(audit, app, scope(), receive, send))
    request_id = dict(sent[0]["headers"])[b"x-ccm-request-id"].decode()
    assert len(request_id) == 32 and sent[1]["body"] == b"PRIVATE_RESULT"
    assert all(r["http_request_id"] == request_id for r in rows)
    assert rows[-1]["response_complete"]
    assert "PRIVATE" not in json.dumps(rows) and HTTP_OBSERVATION.get() is None


def test_actual_http_gate_rejection_is_logged_without_authentication_data():
    audit, rows = logger()
    called = []
    async def handler(s, r, w):
        called.append(True)
    manager = SimpleNamespace(app=SimpleNamespace(_ccm_audit=audit), handle_request=handler)
    gate = OwnerHTTP(manager, "test-owner-token")
    sent = []
    async def send(value):
        sent.append(value)
    request = scope()
    request["headers"] = [(b"authorization", b"Bearer PRIVATE_INVALID_CREDENTIAL")]
    asyncio.run(gate(request, receive, send))
    assert sent[0]["status"] == 401 and not called
    assert rows[-1]["gate_status"] == 401 and rows[-1]["response_complete"]
    assert "PRIVATE_INVALID_CREDENTIAL" not in json.dumps(rows)


def test_delegate_error_is_not_falsely_labeled_as_http_gate():
    audit, rows = logger()
    async def handler(s, r, w):
        await w({"type": "http.response.start", "status": 400, "headers": []})
        await w({"type": "http.response.body", "body": b"PRIVATE_SDK_ERROR"})
    async def send(value):
        pass
    asyncio.run(observe_http(audit, handler, scope(), receive, send))
    assert rows[-1]["status"] == 400 and rows[-1]["gate_status"] is None
    assert "PRIVATE" not in json.dumps(rows)


def test_failed_delivery_is_incomplete_and_never_replayed():
    audit, rows = logger()
    calls = []
    async def handler(s, r, w):
        calls.append(True)
        await w({"type": "http.response.start", "status": 200, "headers": []})
        await w({"type": "http.response.body", "body": b"result"})
    async def send(value):
        raise OSError("PRIVATE_NATIVE_ERROR")
    with pytest.raises(OSError):
        asyncio.run(observe_http(audit, handler, scope(), receive, send))
    assert len(calls) == 1 and not rows[-1]["response_complete"]
    assert any(r["event"] == "http_dispatch_exception" for r in rows)
    assert "PRIVATE_NATIVE_ERROR" not in json.dumps(rows)
    assert HTTP_OBSERVATION.get() is None


def test_concurrent_receipts_do_not_share_identifiers():
    audit, rows = logger()
    receipts = []
    async def app(s, r, w):
        await asyncio.sleep(0)
        out = {"ok": True, "operation_id": "owned-fixture"}
        attach_transport_receipt(out, audit)
        receipts.append(out)
        await w({"type": "http.response.start", "status": 200, "headers": []})
        await w({"type": "http.response.body", "body": b"{}"})
    async def send(value):
        pass
    async def run():
        await asyncio.gather(*(observe_http(audit, app, scope(), receive, send) for _ in range(3)))
    asyncio.run(run())
    assert len({r["http_request_id"] for r in receipts}) == 3
    assert len([r for r in rows if r["event"] == "http_tool_receipt"]) == 3


def test_disabled_observer_does_not_change_dispatch():
    calls = []
    async def app(s, r, w):
        calls.append(True)
        return 17
    assert asyncio.run(observe_http(None, app, scope(), receive, None)) == 17
    assert calls == [True]


def test_real_mcp_http_manager_connects_header_receipt_and_log():
    audit, rows = logger()
    bridge = SimpleNamespace(
        cfg=SimpleNamespace(oauth={}, browser_use_enabled=False, computer_use_enabled=False),
        audit=audit,
        execute=lambda name, args: {"ok": True, "operation_id": "owned-fixture-operation", "result": {"status": "ok"}},
    )
    manager = StreamableHTTPSessionManager(app=make_server(bridge), json_response=True, stateless=True)
    gate = OwnerHTTP(manager, "unit-owner-credential")
    assert gate.audit is audit
    async def run():
        async with manager.run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gate), base_url="http://localhost",
                headers={"Authorization": "Bearer unit-owner-credential", "Accept": "application/json, text/event-stream"}) as client:
                r = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "codex_health", "arguments": {}}})
                assert r.status_code == 200, r.text
                payload = r.json()["result"]["structuredContent"]
                assert payload["http_request_id"] == r.headers["x-ccm-request-id"]
                return payload["http_request_id"]
    request_id = asyncio.run(run())
    assert any(r["event"] == "http_tool_receipt" and r["http_request_id"] == request_id for r in rows)
    assert "unit-owner-credential" not in json.dumps(rows)


def test_initial_audit_failure_does_not_dispatch_or_leak_context():
    calls = []
    def fail(event, **fields):
        raise OSError("fixture disk failure")
    async def app(*args):
        calls.append(True)
    with pytest.raises(OSError):
        asyncio.run(observe_http(SimpleNamespace(emit=fail), app, scope(), receive, None))
    assert not calls and HTTP_OBSERVATION.get() is None
