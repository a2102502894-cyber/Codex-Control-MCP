"""Actual process lifecycle, PTY and controlled proxy-route verification."""

import http.server, json, os, pathlib, sys, threading, time
import pytest
from codex_control_mcp.bridge import Bridge
from codex_control_mcp.config import Config

ROOT = pathlib.Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="Requires official Windows runtime"
)


@pytest.fixture
def bridge(tmp_path):
    b = Bridge(Config(home=tmp_path / "home", cwd=str(tmp_path)))
    yield b
    b.close()


def ok(b, name, args=None):
    r = b.execute(name, args)
    assert r["ok"], json.dumps(r, ensure_ascii=False)
    return r["result"]


def stopped(b, sid):
    end = time.monotonic() + 8
    while time.monotonic() < end:
        r = ok(b, "session_read", {"session_id": sid})
        if r["state"] not in ("starting", "running"):
            return r
        time.sleep(0.05)
    pytest.fail("Owned process did not stop")


def test_refresh_defers_active_session_and_disconnect_is_lost(bridge):
    b = bridge
    s = ok(
        b,
        "session_start",
        {
            "command": "Write-Output 'ACTIVE';Start-Sleep -Seconds 60",
            "timeout_ms": 65000,
        },
    )
    pid = b.rpc.proc.pid
    generation = b.rpc.generation
    r = b.execute("codex_health", {"refresh": True})
    assert not r["ok"] and r["error"]["code"] == "update_pending"
    assert b.rpc.proc.pid == pid
    b.rpc.close()
    receipt = b.execute("session_read", {"session_id": s["session_id"]})
    r = receipt["result"]
    assert r["state"] in ("lost", "exited")
    if r["state"] == "lost":
        assert not receipt["ok"] and receipt["error"]["code"] == "execution_state_unknown"
        assert receipt["diagnostics"]["failure_origin"] == "execution_transport"
    else:
        assert receipt["ok"] == (r["exit_code"] == 0)
    assert b.rpc.generation == generation  # Cached read must not spawn a new runtime.
    ok(b, "exec_command", {"command": "Write-Output 'NEW_CONNECTION_OK'"})
    assert b.rpc.generation != generation  # A new execution may reconnect.


def test_pty_resize_and_input(bridge):
    b = bridge
    s = ok(
        b,
        "session_start",
        {
            "command": "[Console]::WriteLine('PTY_READY');$x=[Console]::ReadLine();[Console]::WriteLine('PTY_ECHO:'+$x)",
            "tty": True,
            "timeout_ms": 20000,
        },
    )
    sid = s["session_id"]
    end = time.monotonic() + 7
    while time.monotonic() < end:
        r = ok(b, "session_read", {"session_id": sid})
        if "PTY_READY" in r["stdout"]:
            break
        time.sleep(0.05)
    else:
        pytest.fail("PTY did not emit ready marker")
    ok(b, "session_resize", {"session_id": sid, "rows": 36, "cols": 100})
    ok(b, "session_write", {"session_id": sid, "text": "PTY_INPUT_OK\r\n"})
    r = stopped(b, sid)
    assert r["exit_code"] == 0 and "PTY_ECHO:PTY_INPUT_OK" in r["stdout"]
    (ROOT / "evidence/pty-verification.json").write_text(
        json.dumps(
            {
                "backend": "official command/exec",
                "sandbox": "dangerFullAccess",
                "pty": True,
                "resize_acknowledged": True,
                "stdin_echo_verified": True,
                "exit_code": r["exit_code"],
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def test_controlled_proxy_route_through_official_child(bridge):
    b = bridge
    observations = []

    class Proxy(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            expected = self.path == "http://ccm-proxy-proof.invalid/probe"
            observations.append({"expected_target": expected, "method": "GET"})
            body = b"CCM_PROXY_ROUTE_OK" if expected else b"Unexpected destination"
            self.send_response(200 if expected else 502)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    proxy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{proxy.server_port}"
    b.env.update(
        {"HTTP_PROXY": url, "HTTPS_PROXY": url, "NO_PROXY": "localhost,127.0.0.1,::1"}
    )
    b.env.pop("REQUEST_METHOD", None)
    try:
        result = ok(
            b,
            "exec_command",
            {
                "argv": [
                    sys.executable,
                    "-c",
                    "import urllib.request;print(urllib.request.urlopen('http://ccm-proxy-proof.invalid/probe',timeout=5).read().decode())",
                ]
            },
        )
        assert "CCM_PROXY_ROUTE_OK" in result["stdout"]
        assert any(x["expected_target"] for x in observations)
    finally:
        proxy.shutdown()
        proxy.server_close()
    (ROOT / "evidence/proxy-route-verification.json").write_text(
        json.dumps(
            {
                "scope": "official App Server spawned execution child inherits HTTP_PROXY and uses a controlled HTTP proxy",
                "verified": True,
                "observed_requests": observations,
                "system_proxy_modified": False,
                "actual_user_proxy_route": "not_tested",
                "remote_websocket_route": "not_tested",
                "browser_route": "not_tested",
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def test_non_object_arguments_rejected_without_crash(bridge):
    r = bridge.execute("read_file", ["invalid"])
    assert not r["ok"] and r["error"]["code"] == "invalid_arguments"


def test_wsl_availability_is_reported_not_assumed(bridge):
    b = bridge
    result = b.execute(
        "exec_command", {"argv": ["wsl.exe", "--list", "--quiet"], "timeout_ms": 10000}
    )
    report = {
        "discovery_ok": result["ok"],
        "wsl_execution_verified": False,
        "error": result.get("error"),
    }
    if result["ok"] and result["result"]["stdout"].replace("\x00", "").strip():
        execution = b.execute(
            "exec_command",
            {"shell": "wsl", "command": "printf CCM_WSL_OK", "timeout_ms": 15000},
        )
        report["execution"] = execution
        report["wsl_execution_verified"] = bool(
            execution["ok"] and "CCM_WSL_OK" in execution["result"]["stdout"]
        )
        assert report["wsl_execution_verified"]
    else:
        report["status"] = "unavailable_in_current_environment"
    (ROOT / "evidence/wsl-verification.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
