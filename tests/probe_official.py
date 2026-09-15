"""Bounded probe of the installed official Codex. No agent turns, no auth copying."""

from __future__ import annotations
import base64, concurrent.futures, http.server, json, os, pathlib, subprocess, threading, time, uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
EV = ROOT / "evidence"
HOME = EV / "probe-codex-home"
HOME.mkdir(exist_ok=True)
model_attempts = []


class Sentinel(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        model_attempts.append(
            {"method": "POST", "path": self.path.split("?")[0], "at": time.time()}
        )
        self.send_response(503)
        self.end_headers()
        self.wfile.write(b"Inference forbidden in execution test")

    def do_GET(self):
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args):
        pass


sentinel = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Sentinel)
threading.Thread(target=sentinel.serve_forever, daemon=True).start()
config = "\n".join(
    [
        'sandbox_mode = "danger-full-access"',
        'approval_policy = "never"',
        'model = "execution-test-only"',
        'model_provider = "execution_test"',
        "[analytics]",
        "enabled = false",
        "[feedback]",
        "enabled = false",
        "[model_providers.execution_test]",
        'name = "Execution test sentinel"',
        f'base_url = "http://127.0.0.1:{sentinel.server_port}/v1"',
        'wire_api = "responses"',
        "requires_openai_auth = false",
        "",
    ]
)
(HOME / "config.toml").write_text(config, encoding="utf-8")
roots = pathlib.Path(os.environ["LOCALAPPDATA"]) / "OpenAI" / "Codex" / "bin"
codex = max(roots.glob("*/codex.exe"), key=lambda p: p.stat().st_mtime_ns)
env = os.environ.copy()
env["CODEX_HOME"] = str(HOME)
for k in ("OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY"):
    env.pop(k, None)
p = subprocess.Popen(
    [str(codex), "app-server", "--stdio"],
    cwd=ROOT / "test-workspace",
    env=env,
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)
lock = threading.Lock()
pending = {}
calls = []
notes = []
err = []
seq = 0


def reader():
    for line in p.stdout:
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if "id" in msg and "method" not in msg:
            f = pending.pop(str(msg["id"]), None)
            if f:
                f.set_result(msg)
        elif "method" in msg:
            notes.append(msg)
            if "id" in msg:
                with lock:
                    p.stdin.write(
                        (
                            json.dumps(
                                {
                                    "id": msg["id"],
                                    "error": {
                                        "code": -32601,
                                        "message": "Probe does not authorize callbacks",
                                    },
                                }
                            )
                            + "\n"
                        ).encode()
                    )
                    p.stdin.flush()
    for f in list(pending.values()):
        if not f.done():
            f.set_exception(RuntimeError("app-server disconnected"))


threading.Thread(target=reader, daemon=True).start()
threading.Thread(
    target=lambda: [err.append(l.decode("utf-8", "replace")) for l in p.stderr],
    daemon=True,
).start()


def send(method, params=None):
    global seq
    seq += 1
    f = concurrent.futures.Future()
    pending[str(seq)] = f
    msg = {"id": seq, "method": method, "params": params}
    calls.append(
        {
            "id": seq,
            "method": method,
            "param_keys": sorted(params) if isinstance(params, dict) else [],
        }
    )
    with lock:
        p.stdin.write((json.dumps(msg) + "\n").encode())
        p.stdin.flush()
    return f


def call(method, params=None, timeout=20):
    return send(method, params).result(timeout)


report = {
    "codex_path": str(codex),
    "cli_version": subprocess.check_output([str(codex), "--version"]).decode().strip(),
    "effective_sandbox": "dangerFullAccess",
    "results": {},
    "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
}
try:
    report["results"]["initialize"] = call(
        "initialize",
        {
            "clientInfo": {"name": "codex_control_probe", "version": "0.1.0"},
            "capabilities": {"experimentalApi": True},
        },
    )
    with lock:
        p.stdin.write(b'{"method":"initialized"}\n')
        p.stdin.flush()
    base = {
        "cwd": str(ROOT / "test-workspace"),
        "sandboxPolicy": {"type": "dangerFullAccess"},
        "timeoutMs": 10000,
    }
    report["results"]["shell"] = call(
        "command/exec",
        {
            **base,
            "command": [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Write-Output 'CODEX_CONTROL_MCP_OK'; ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)",
            ],
        },
    )
    proof = ROOT / "test-workspace" / ("proof-" + uuid.uuid4().hex + ".txt")
    content = "CODEX_CONTROL_MCP_PROOF_中文\r\n"
    report["results"]["write"] = call(
        "fs/writeFile",
        {"path": str(proof), "dataBase64": base64.b64encode(content.encode()).decode()},
    )
    report["results"]["read"] = call("fs/readFile", {"path": str(proof)})
    report["results"]["list"] = call(
        "fs/readDirectory", {"path": str(ROOT / "test-workspace")}
    )
    pid = "probe-" + uuid.uuid4().hex
    f = send(
        "command/exec",
        {
            **base,
            "command": [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "[Console]::WriteLine('SESSION_READY'); $v=[Console]::ReadLine(); [Console]::WriteLine('ECHO:'+$v)",
            ],
            "processId": pid,
            "streamStdoutStderr": True,
            "streamStdin": True,
        },
    )
    deadline = time.time() + 6
    while (
        time.time() < deadline
        and not any(
            n.get("method") == "command/exec/outputDelta"
            and n.get("params", {}).get("processId") == pid
            for n in notes
        )
        and not f.done()
    ):
        time.sleep(0.05)
    if not f.done():
        report["results"]["session_write"] = call(
            "command/exec/write",
            {
                "processId": pid,
                "deltaBase64": base64.b64encode(b"INPUT_OK\n").decode(),
                "closeStdin": True,
            },
        )
    report["results"]["session_finish"] = f.result(15)
    report["results"]["session_output"] = [
        n
        for n in notes
        if n.get("method") == "command/exec/outputDelta"
        and n.get("params", {}).get("processId") == pid
    ]
    report["results"]["mcp_status"] = call("mcpServerStatus/list", {"limit": 50})
    report["results"]["remote_status"] = call("remoteControl/status/read")
except Exception as e:
    report["probe_error"] = type(e).__name__ + ": " + str(e)
finally:
    p.stdin.close()
    try:
        p.wait(5)
    except subprocess.TimeoutExpired:
        p.terminate()
        p.wait(5)
    sentinel.shutdown()
    sentinel.server_close()
    report["rpc_calls"] = calls
    report["configured_inference_endpoint_attempts"] = model_attempts
    report["inference_rpc_calls"] = sum(
        x["method"].startswith(("turn/", "review/")) for x in calls
    )
    report["network_scope"] = (
        "Configured test model provider sentinel only; other process egress not exhaustively traced"
    )
    report["stderr_line_count"] = len(err)
    report["stopped_owned_appserver"] = p.poll() is not None
    (EV / "official-probe.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=True, indent=2))
