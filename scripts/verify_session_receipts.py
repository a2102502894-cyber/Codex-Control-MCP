"""Owned HTTP/official-runtime probes for silent jobs and final receipts."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid

import httpx
from codex_control_mcp.auth import owner_token
from codex_control_mcp.config import Config, DEFAULT_CONFIG

ROOT = Path(__file__).resolve().parents[1]


class Client:
    def __init__(self, cfg, token):
        self.url = f"http://127.0.0.1:{cfg.http.get('port', 8774)}/mcp"
        self.client = httpx.Client(trust_env=False, timeout=30, headers={
            "Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream"})
        self.counter = 0
        init, _ = self.rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "owned-session-receipt-verifier", "version": "1"}})
        self.client.headers["MCP-Protocol-Version"] = init["protocolVersion"]
        self.client.post(self.url, json={"jsonrpc": "2.0", "method": "notifications/initialized"}).raise_for_status()

    def rpc(self, method, params):
        self.counter += 1
        received, result = [], None
        started = time.monotonic()
        with self.client.stream("POST", self.url, json={"jsonrpc": "2.0", "id": self.counter,
                "method": method, "params": params}) as response:
            response.raise_for_status()
            if response.headers.get("Mcp-Session-Id"):
                self.client.headers["Mcp-Session-Id"] = response.headers["Mcp-Session-Id"]
            if "text/event-stream" not in response.headers.get("content-type", ""):
                frame = json.loads(response.read())
                assert "error" not in frame, frame.get("error", {}).get("code")
                return frame["result"], received
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                frame = json.loads(line[5:])
                if frame.get("method") == "notifications/progress":
                    received.append({"progress": frame["params"]["progress"],
                                     "at_seconds": round(time.monotonic() - started, 3)})
                    print("PROGRESS_RECEIVED", params.get("name"), received[-1]["at_seconds"], flush=True)
                if frame.get("id") == self.counter:
                    assert "error" not in frame, frame.get("error", {}).get("code")
                    result = frame["result"]
        assert result is not None, "missing JSON-RPC final result"
        return result, received

    def call(self, name, args, progress=False):
        params = {"name": name, "arguments": args}
        if progress:
            params["_meta"] = {"progressToken": f"owned-{self.counter + 1}"}
        result, notices = self.rpc("tools/call", params)
        return result["structuredContent"], notices

    def close(self):
        self.client.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--production", action="store_true")
    args = parser.parse_args()
    run = ROOT / "evidence/progress-receipt-20261008" / ("owned-" + uuid.uuid4().hex[:8])
    run.mkdir(parents=True)
    report = {"scope": "production_owned_commands" if args.production else "temporary_http_official_runtime",
              "production_restart_performed": False, "cases": {}}
    process = log = client = None
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONPATH=str(ROOT / "src"))
    if args.production:
        cfg = Config.load()
    else:
        home = run / "home"
        home.mkdir()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        (home / "config.toml").write_text(DEFAULT_CONFIG.replace("8774", str(port)), "utf-8")
        cfg = Config.load(home)
        cfg.initialize_storage()
    token = owner_token(cfg, create=not args.production)

    def start():
        nonlocal process, log
        log = (run / "service.log").open("ab")
        process = subprocess.Popen([sys.executable, "-m", "codex_control_mcp", "--home", str(cfg.home),
            "serve", "--transport", "streamable-http"], cwd=ROOT, env=env, stdout=log, stderr=log,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        deadline = time.monotonic() + 20
        while True:
            assert process.poll() is None, "owned temporary service exited"
            try:
                with socket.create_connection(("127.0.0.1", cfg.http["port"]), timeout=0.2):
                    return
            except OSError:
                if time.monotonic() > deadline:
                    raise TimeoutError("owned temporary service start deadline")
                time.sleep(0.1)

    def stop():
        nonlocal process, log
        if process is not None and process.poll() is None:
            result = subprocess.run([sys.executable, "-m", "codex_control_mcp", "--home", str(cfg.home), "stop"],
                cwd=ROOT, env=env, capture_output=True, timeout=55,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                process.terminate()  # Only the PID created by this verifier.
                process.wait(10)
            report.setdefault("temporary_stops", []).append({"stop_exit_code": result.returncode,
                                                              "service_exit_code": process.returncode})
        if log:
            log.close()
            log = None

    try:
        if not args.production:
            start()
        client = Client(cfg, token)
        specs, _ = client.rpc("tools/list", {})
        read = next(t for t in specs["tools"] if t["name"] == "session_read")
        assert read["inputSchema"]["properties"]["wait_ms"]["maximum"] == 10000
        report["tool_count"] = len(specs["tools"])
        health, _ = client.call("codex_health", {})
        assert health["ok"]
        report["runtime_version"] = health["evidence"]["runtime_version"]

        print("CASE silent_log_only", flush=True)
        task_log = run / "silent-task.log"
        script = "from pathlib import Path; import time; p=Path(" + repr(str(task_log)) + "); p.write_text('BEGIN\\n'); time.sleep(12); p.write_text(p.read_text()+'END\\n')"
        out, _ = client.call("exec_command", {"argv": [sys.executable, "-c", script], "yield_time_ms": 0})
        receipt = out["result"]
        silent_id = receipt["session_id"]
        notices, polls = [], 0
        while receipt["continuation_required"]:
            assert polls < 5
            action = receipt["next_action"]
            out, updates = client.call(action["tool"], action["arguments"], progress=True)
            assert out["ok"]
            notices.extend(updates)
            receipt = out["result"]
            assert receipt["stdout"] == receipt["stderr"] == ""
            polls += 1
        assert receipt["exit_code"] == 0 and receipt["final_receipt_ready"]
        assert task_log.read_text() == "BEGIN\nEND\n" and len(notices) >= 2
        assert any(n["at_seconds"] >= 4 for n in notices)
        report["cases"]["silent_log_only"] = {"polls": polls, "progress_notifications": notices,
            "elapsed_ms": receipt["elapsed_ms"], "exit_code": 0, "dispatches": 1, "no_fabricated_stdout": True}

        print("CASE early_output_and_final_drain", flush=True)
        payload = "首批🙂x" * 900 + "\nFINAL_SUMMARY\n"
        script = "import sys; sys.stdout.buffer.write((" + repr("首批🙂x") + " * 900 + '\\nFINAL_SUMMARY\\n').encode('utf-8')); sys.stdout.buffer.flush()"
        out, _ = client.call("session_start", {"argv": [sys.executable, "-c", script], "output_limit_bytes": 1024})
        receipt = out["result"]
        output, pages = receipt["stdout"], 1
        output_id = receipt["session_id"]
        exited_with_pages = False
        while receipt["continuation_required"]:
            assert pages < 100
            if receipt["process_completed"] and receipt["has_more"]:
                assert not receipt["completed"] and not receipt["final_receipt_ready"]
                exited_with_pages = True
            action = receipt["next_action"]
            out, _ = client.call(action["tool"], action["arguments"])
            assert out["ok"]
            receipt = out["result"]
            output += receipt["stdout"]
            pages += 1
        assert output == payload and receipt["exit_code"] == 0 and receipt["final_receipt_ready"]
        assert exited_with_pages
        report["cases"]["early_output_and_final_drain"] = {"bytes": len(output.encode()), "pages": pages,
            "identical": True, "premature_completion_prevented": True, "exit_code": 0}

        print("CASE failure_receipt", flush=True)
        out, _ = client.call("exec_command", {"argv": [sys.executable, "-c", "print('OWNED_FAILURE'); raise SystemExit(7)"]})
        receipt = out["result"]
        while receipt["continuation_required"]:
            action = receipt["next_action"]
            out, _ = client.call(action["tool"], action["arguments"])
            receipt = out["result"]
        assert not out["ok"] and receipt["exit_code"] == 7 and receipt["final_receipt_ready"]
        report["cases"]["failure_receipt"] = {"exit_code": 7, "ok_is_false": True}

        if not args.production:
            print("CASE restart_receipt_recovery", flush=True)
            client.close()
            client = None
            stop()
            start()
            client = Client(cfg, token)
            restored, _ = client.call("session_read", {"session_id": silent_id})
            assert restored["ok"] and restored["result"]["history_only"]
            assert restored["result"]["exit_code"] == 0 and restored["result"]["final_receipt_ready"]
            missing, _ = client.call("session_read", {"session_id": output_id})
            assert missing["result"]["exit_code"] == 0 and not missing["result"]["final_receipt_ready"]
            assert missing["result"]["recovery_warning"]["code"] == "session_output_unavailable"
            report["cases"]["restart_receipt_recovery"] = {"saved_exit_readable": True,
                "missing_output_explicit": True, "reattachment_or_replay": False}
        report["ok"] = True
    finally:
        if client:
            client.close()
        if not args.production:
            stop()
            report["temporary_service_stopped"] = process is None or process.poll() is not None
            report["ok"] = bool(report.get("ok")) and report["temporary_service_stopped"] and all(
                s["stop_exit_code"] == s["service_exit_code"] == 0 for s in report.get("temporary_stops", []))
        path = run / "summary.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
        print(json.dumps({"ok": report.get("ok", False), "summary": str(path), "cases": report["cases"]}, ensure_ascii=True), flush=True)
    if not report.get("ok"):
        raise RuntimeError("Owned receipt verification or cleanup failed; see saved summary.")


if __name__ == "__main__":
    main()
