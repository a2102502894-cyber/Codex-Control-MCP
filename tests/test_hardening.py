"""Regression coverage for release 0.1.2 boundaries and cached state."""

import asyncio
import json
import os
import subprocess
import sys
import pytest
from codex_control_mcp.config import Config
from codex_control_mcp.errors import BridgeError
from codex_control_mcp.idempotency import Idempotency
from codex_control_mcp.sessions import Session
from codex_control_mcp.http_guard import OwnerHTTP
from codex_control_mcp.lifecycle import process_alive


@pytest.mark.parametrize(
    "text",
    [
        'computer_use_enabled="false"',
        "experimental=1",
        "[http]\nport=true",
        "[http]\nrequests_per_minute=0",
        '[http]\nallowed_hosts=["*"]',
        "[http]\nbody_timeout_seconds=0",
        '[oauth]\nenabled="yes"',
        '[oauth]\nenabled=true\nissuer="http://insecure.test"',
    ],
)
def test_invalid_configuration_fails_closed(tmp_path, text):
    (tmp_path / "config.toml").write_text(text, "utf-8")
    with pytest.raises(BridgeError):
        Config.load(tmp_path)


def test_empty_output_deltas_do_not_exhaust_event_cache():
    session = Session("isolated", "g", ".", 1024)
    for _ in range(10000):
        session.append({"deltaBase64": "", "stream": "stdout"})
    assert not session.events and session.next_cursor == 0
    session.append({"deltaBase64": "", "capReached": True})
    assert session.truncated and not session.events


def test_idempotency_results_are_detached_from_caller_mutations(tmp_path):
    cache = Idempotency(tmp_path / "calls.sqlite3")
    try:
        assert cache.reserve("key", "tool", {}) is None
        result = {
            "ok": True,
            "result": {"value": 1},
            "_image_blocks": [{"data": "isolated"}],
        }
        cache.finish("key", result)
        result["result"]["value"] = 2
        result.pop("_image_blocks")
        first = cache.reserve("key", "tool", {})
        assert first["result"]["value"] == 1 and first["_image_blocks"]
        first["result"]["value"] = 3
        assert cache.reserve("key", "tool", {})["result"]["value"] == 1
    finally:
        cache.close()


class Sink:
    async def handle_request(self, scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def test_http_guard_rejects_slow_body_and_ambiguous_headers():
    async def run():
        app = OwnerHTTP(Sink(), "x" * 48, body_timeout=0.02)
        output = []

        async def send(msg):
            output.append(msg)

        async def receive():
            await asyncio.sleep(0.2)
            return {"type": "http.request", "body": b"x", "more_body": False}

        scope = {
            "type": "http",
            "path": "/mcp",
            "method": "POST",
            "headers": [(b"authorization", b"Bearer " + b"x" * 48)],
        }
        await app(scope, receive, send)
        assert output[0]["status"] == 408
        output.clear()
        scope["headers"].append((b"authorization", b"Bearer different"))
        await app(scope, receive, send)
        assert output[0]["status"] == 400

    asyncio.run(run())


def test_authentication_failures_do_not_consume_owner_request_budget():
    async def run():
        app = OwnerHTTP(Sink(), "x" * 48, rpm=1)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        outputs = []

        async def send(msg):
            outputs.append(msg)

        scope = {"type": "http", "path": "/mcp", "method": "GET", "headers": []}
        await app(scope, receive, send)
        assert outputs[0]["status"] == 401
        outputs.clear()
        await app(
            {**scope, "headers": [(b"authorization", b"Bearer " + b"x" * 48)]},
            receive,
            send,
        )
        assert outputs[0]["status"] == 200

    asyncio.run(run())


def test_actual_dead_process_record_is_not_treated_as_running(tmp_path):
    child = subprocess.Popen([sys._base_executable, "-c", "pass"])
    child.wait(10)
    assert process_alive(child.pid) is False
    assert process_alive(os.getpid()) is True
    assert process_alive(-1) is None
    home = tmp_path / "home"
    (home / "state").mkdir(parents=True)
    record = {
        "pid": child.pid,
        "owner": "Codex-Control-MCP",
        "graceful_stop_supported": False,
    }
    path = home / "state/tunnel-service.json"
    path.write_text(json.dumps(record), "utf-8")
    r = subprocess.run(
        [
            sys.executable,
            "-m",
            "codex_control_mcp",
            "--home",
            str(home),
            "stop",
            "--target",
            "https",
        ],
        capture_output=True,
        timeout=15,
    )
    assert r.returncode == 0, r.stdout.decode("utf-8", "replace")
    assert json.loads(r.stdout)["state"] == "already_stopped"
    assert json.loads(path.read_text("utf-8")) == record
