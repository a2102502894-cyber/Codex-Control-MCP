"""Real temporary MCP service and official backend; production is never stopped."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx
from codex_control_mcp.auth import owner_token
from codex_control_mcp.config import Config, DEFAULT_CONFIG

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "evidence/stall-repair-20261007/real"


def main():
    RUN.mkdir(parents=True, exist_ok=True)
    home = RUN / "home"
    home.mkdir(exist_ok=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    (home / "config.toml").write_text(DEFAULT_CONFIG.replace("8774", str(port)), "utf-8")
    cfg = Config.load(home)
    cfg.initialize_storage()
    token = owner_token(cfg, create=True)
    report = {"production_restart_performed": False, "scope": "temporary_loopback_mcp_and_official_backend", "port": port}
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONPATH=str(ROOT / "src"))
    with (RUN / "service.log").open("wb") as log:
        process = subprocess.Popen([sys.executable, "-m", "codex_control_mcp", "--home", str(home), "serve", "--transport", "streamable-http"], cwd=ROOT, env=env, stdout=log, stderr=log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        report["temporary_service_pid"] = process.pid
        counter = 0
        max_response_bytes = 0
        try:
            deadline = time.monotonic() + 20
            while True:
                if process.poll() is not None:
                    raise RuntimeError("temporary_service_start_failed")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    if time.monotonic() > deadline:
                        raise TimeoutError("temporary_service_start_deadline")
                    time.sleep(0.1)
            with httpx.Client(trust_env=False, timeout=25, headers={"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream"}) as client:
                url = f"http://127.0.0.1:{port}/mcp"
                def rpc(method, params):
                    nonlocal counter, max_response_bytes
                    counter += 1
                    response = client.post(url, json={"jsonrpc": "2.0", "id": counter, "method": method, "params": params})
                    response.raise_for_status()
                    max_response_bytes = max(max_response_bytes, len(response.content))
                    if "text/event-stream" in response.headers.get("content-type", ""):
                        frames = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]
                        payload = next(frame for frame in frames if frame.get("id") == counter)
                        notifications = sum(frame.get("method") == "notifications/progress" for frame in frames)
                    else:
                        payload = response.json()
                        notifications = 0
                    assert "error" not in payload
                    return payload["result"], notifications

                def call(name, arguments, progress=False):
                    params = {"name": name, "arguments": arguments}
                    if progress:
                        params["_meta"] = {"progressToken": "owned-probe"}
                    result, count = rpc("tools/call", params)
                    return result["structuredContent"], count

                init, _ = rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "stall-regression", "version": "1"}})
                client.headers["MCP-Protocol-Version"] = init["protocolVersion"]
                notified = client.post(url, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
                notified.raise_for_status()
                health, _ = call("codex_health", {})
                assert health["ok"]
                report["runtime_version"] = health["evidence"]["runtime_version"]

                failed, _ = call("exec_command", {"command": "Write-Error 'CCM_OWNED_ERROR'; Write-Output 'UNREACHABLE'"})
                assert not failed["ok"] and failed["result"]["exit_code"] != 0
                assert "UNREACHABLE" not in failed["result"]["stdout"]
                report["powershell_error_propagation"] = {"ok_is_false": True, "exit_code": failed["result"]["exit_code"], "following_command_executed": False}

                started = time.monotonic()
                out, count = call("exec_command", {"argv": [sys.executable, "-c", "import time; print('BEGIN',flush=True); time.sleep(33); print('END',flush=True)"]})
                assert out["ok"] and count == 0
                assert out["result"]["effective_timeout_ms"] == 3600000
                text = out["result"]["stdout"]
                receipt = out["result"]
                while not receipt["completed"] or receipt["has_more"]:
                    assert time.monotonic() - started < 50
                    time.sleep(1)
                    out, _ = call("session_read", {"session_id": receipt["session_id"], "cursor": receipt["next_cursor"]})
                    assert out["ok"]
                    receipt = out["result"]
                    text += receipt["stdout"]
                assert receipt["exit_code"] == 0 and "END" in text
                report["default_33_second_task"] = {"exit_code": 0, "end_marker_received": True, "elapsed_seconds": round(time.monotonic() - started, 2)}

                out, _ = call("exec_command", {"argv": [sys.executable, "-c", "import time; time.sleep(3)"], "timeout_ms": 500})
                receipt = out["result"]
                while not receipt["completed"]:
                    time.sleep(0.2)
                    out, _ = call("session_read", {"session_id": receipt["session_id"], "cursor": receipt["next_cursor"]})
                    receipt = out["result"]
                assert receipt["exit_code"] == 124 and out["error"]["code"] == "command_exit_124"
                assert out["error"]["details"]["effective_timeout_ms"] == 500
                report["explicit_deadline"] = {"exit_code": 124, "timeout_ms": 500, "error_code": out["error"]["code"]}

                out, _ = call("exec_command", {"argv": [sys.executable, "-c", "import sys; sys.stdout.write('中😀x'*32768)"], "env": {"PYTHONIOENCODING": "utf-8"}})
                receipt = out["result"]
                text = receipt["stdout"]
                pages = 1
                while not receipt["completed"] or receipt["has_more"]:
                    out, _ = call("session_read", {"session_id": receipt["session_id"], "cursor": receipt["next_cursor"]})
                    assert out["ok"] and "chunks" not in out["result"]
                    receipt = out["result"]
                    text += receipt["stdout"]
                    pages += 1
                assert text == "中😀x"*32768
                report["unicode_256k_output"] = {"bytes": len(text.encode()), "pages": pages, "complete_and_identical": True}

                out, count = call("exec_command", {"argv": [sys.executable, "-c", "import time; time.sleep(7); print('HEARTBEAT_OK')"], "execution_mode": "buffered", "timeout_ms": 15000}, progress=True)
                assert out["ok"] and count >= 2 and out["progress_observation"]["sent"] == count
                report["progress_requested"] = {"notifications_received": count, "result_received": True}
                report["maximum_mcp_response_bytes"] = max_response_bytes
                report["ok"] = True
        finally:
            # Only the temporary process belongs to this verifier.
            if process.poll() is None:
                stopped = subprocess.run([sys.executable, "-m", "codex_control_mcp", "--home", str(home), "stop"], cwd=ROOT, env=env, capture_output=True, timeout=35, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                report["temporary_stop_exit_code"] = stopped.returncode
                try:
                    process.wait(10)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.wait(10)
            report["temporary_service_stopped"] = process.poll() is not None
            (RUN / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
