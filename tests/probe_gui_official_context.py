"""Probe official context-thread -> MCP call without starting a model turn.
Only reads GUI service availability. Never fabricates Desktop thread/turn identifiers.
"""

import concurrent.futures, http.server, json, os, pathlib, subprocess, threading, time, uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
HOME = ROOT / "evidence" / ("gui-context-home-" + uuid.uuid4().hex[:8])
HOME.mkdir()
manifest = max(
    (
        pathlib.Path.home() / ".codex/plugins/cache/openai-bundled/unified-computer-use"
    ).glob("*/.mcp.json"),
    key=lambda p: p.stat().st_mtime_ns,
)
plugin = json.loads(manifest.read_text("utf-8"))["mcpServers"]["cua_repl"]
calls = []
attempts = []
notes = []
pending = {}
lock = threading.Lock()
seq = 0


class Sentinel(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        attempts.append({"method": "POST", "path": self.path.split("?")[0]})
        self.send_response(503)
        self.end_headers()

    def do_GET(self):
        self.send_response(404)
        self.end_headers()

    def log_message(self, *a):
        pass


sentinel = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Sentinel)
threading.Thread(target=sentinel.serve_forever, daemon=True).start()
config = [
    'sandbox_mode = "danger-full-access"',
    'approval_policy = "never"',
    'model = "execution-context-test"',
    'model_provider = "execution_test"',
    "[analytics]",
    "enabled=false",
    "[feedback]",
    "enabled=false",
    "[model_providers.execution_test]",
    'name="Execution-only endpoint sentinel"',
    f'base_url="http://127.0.0.1:{sentinel.server_port}/v1"',
    'wire_api="responses"',
    "requires_openai_auth=false",
    "[mcp_servers.cua_repl]",
    f"command={json.dumps(plugin['command'])}",
    f"args={json.dumps(plugin['args'])}",
    f"cwd={json.dumps(str(manifest.parent))}",
    "enabled=true",
    'enabled_tools=["js","js_reset"]',
    "startup_timeout_sec=35",
    "tool_timeout_sec=25",
    "[mcp_servers.cua_repl.env]",
]
penv = dict(plugin.get("env", {}))
penv["CODEX_HOME"] = str(HOME)
penv["CUA_REPL_ENABLED_SURFACES"] = "browser,computer"
for k, v in penv.items():
    config.append(f"{k}={json.dumps(v)}")
(HOME / "config.toml").write_text("\n".join(config) + "\n", encoding="utf-8")
env = os.environ.copy()
env["CODEX_HOME"] = str(HOME)
for k in ("OPENAI_API_KEY", "CODEX_API_KEY"):
    env.pop(k, None)
codex = max(
    (pathlib.Path(os.environ["LOCALAPPDATA"]) / "OpenAI/Codex/bin").glob("*/codex.exe"),
    key=lambda p: p.stat().st_mtime_ns,
)
p = subprocess.Popen(
    [str(codex), "app-server", "--stdio"],
    cwd=ROOT / "test-workspace",
    env=env,
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)


def write(msg):
    with lock:
        p.stdin.write((json.dumps(msg) + "\n").encode())
        p.stdin.flush()


def reader():
    for line in p.stdout:
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if "id" in m and "method" not in m:
            f = pending.pop(m["id"], None)
            if f:
                f.set_result(m)
        elif "method" in m:
            notes.append(m.get("method"))
            if "id" in m:
                write(
                    {
                        "id": m["id"],
                        "error": {
                            "code": -32601,
                            "message": "No agent or approval workflow is implemented in this execution probe",
                        },
                    }
                )


threading.Thread(target=reader, daemon=True).start()
errcount = []
threading.Thread(
    target=lambda: [errcount.append(1) for _ in p.stderr], daemon=True
).start()


def call(method, params=None, timeout=45):
    global seq
    seq += 1
    f = concurrent.futures.Future()
    pending[seq] = f
    calls.append(method)
    write({"id": seq, "method": method, "params": params})
    return f.result(timeout)


report = {
    "manifest": str(manifest),
    "own_runtime_home": str(HOME),
    "surfaces": "browser,computer",
    "model_turn_started": False,
    "steps": [],
}


def add(name, data):
    report["steps"].append({"name": name, "data": data})
    print(json.dumps({"step": name, "data": data}, ensure_ascii=True), flush=True)


def compact(data):
    if isinstance(data, dict):
        result = {}
        for k, v in data.items():
            if k in ("text", "output") and isinstance(v, str):
                result[k] = v[-7000:]
            elif k in ("data", "dataBase64") and isinstance(v, str) and len(v) > 1000:
                result[k] = "[binary omitted]"
            elif k == "content" and isinstance(v, list):
                result[k] = [compact(x) for x in v][-2:]
            else:
                result[k] = compact(v)
        return result
    if isinstance(data, list):
        return [compact(x) for x in data]
    return data


try:
    add(
        "initialize",
        call(
            "initialize",
            {
                "clientInfo": {"name": "codex_control_gui_probe", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            },
        ),
    )
    write({"method": "initialized"})
    t = call(
        "thread/start",
        {
            "cwd": str(ROOT / "test-workspace"),
            "ephemeral": True,
            "model": "execution-context-test",
            "modelProvider": "execution_test",
            "sandbox": "danger-full-access",
            "approvalPolicy": "never",
        },
        timeout=45,
    )
    if "error" in t:
        add("thread_context_error", t)
    else:
        thread = t["result"]["thread"]["id"]
        add(
            "thread_context_created",
            {
                "thread_id": thread,
                "source": "actual thread/start response",
                "turn_start_called": False,
            },
        )
        status = call("mcpServerStatus/list", {"limit": 50})
        add(
            "mcp_status",
            {
                "error": status.get("error"),
                "servers": [
                    {"name": s.get("name"), "tool_names": list(s.get("tools", {}))}
                    for s in status.get("result", {}).get("data", [])
                ],
            },
        )
        for name, code in [
            ("browsers", "await cua.listBrowsers();"),
            (
                "native_windows",
                'try { const {sky}=await import("@oai/sky"); nodeRepl.write(JSON.stringify((await sky.list_windows()).map(w=>({id:w.id,app:w.app})))); } catch(e) { nodeRepl.write(JSON.stringify({error:String(e)})); }',
            ),
        ]:
            result = call(
                "mcpServer/tool/call",
                {
                    "threadId": thread,
                    "server": "cua_repl",
                    "tool": "js",
                    "arguments": {
                        "code": code,
                        "timeout_ms": 12000,
                        "title": "Codex-Control-MCP read-only " + name,
                    },
                },
                timeout=30,
            )
            add(name, compact(result))
except BaseException as e:
    report["probe_error"] = type(e).__name__ + ": " + str(e)[:1200]
finally:
    p.stdin.close()
    try:
        p.wait(6)
    except subprocess.TimeoutExpired:
        p.terminate()
        p.wait(5)
    sentinel.shutdown()
    sentinel.server_close()
    report["outgoing_methods"] = calls
    report["configured_model_endpoint_attempts"] = attempts
    report["owned_appserver_stopped"] = p.poll() is not None
    report["stderr_line_count"] = len(errcount)
    report["notifications"] = sorted(set(notes))
    report["full_process_network_trace"] = "not_performed"
    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    (ROOT / "evidence/gui-official-context-probe.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "finished": True,
                "error": report.get("probe_error"),
                "model_endpoint_attempts": len(attempts),
            },
            ensure_ascii=True,
        )
    )
