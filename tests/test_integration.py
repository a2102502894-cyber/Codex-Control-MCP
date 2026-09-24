"""Real official Codex tests in test-owned directories; no model turns."""

import base64, concurrent.futures, hashlib, http.server, json, os, pathlib, statistics, threading, time, uuid
import pytest
from codex_control_mcp.bridge import Bridge
from codex_control_mcp.common import digest, ps_argv
from codex_control_mcp.config import Config
from codex_control_mcp.errors import BridgeError
from codex_control_mcp.schema import ALLOWED_METHODS

ROOT = pathlib.Path(__file__).resolve().parents[1]
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.name != "nt", reason="Real Windows runtime required"),
]


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    base = tmp_path_factory.mktemp("official-core")
    work = base / "project"
    work.mkdir()
    home = base / "bridge-home"
    runtime_home = base / "runtime-home"
    runtime_home.mkdir()
    attempts = []

    class Guard(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            attempts.append({"method": "POST", "path": self.path.split("?")[0]})
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b"Model inference is forbidden in this test")

        def do_GET(self):
            self.send_response(404)
            self.end_headers()

        def log_message(self, *args):
            pass

    guard = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Guard)
    threading.Thread(target=guard.serve_forever, daemon=True).start()
    (runtime_home / "config.toml").write_text(
        "\n".join(
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
                'name = "No inference test sentinel"',
                f'base_url = "http://127.0.0.1:{guard.server_port}/v1"',
                'wire_api = "responses"',
                "requires_openai_auth = false",
                "",
            ]
        ),
        encoding="utf-8",
    )
    b = Bridge(
        Config(home=home, cwd=str(work), runtime_home_override=str(runtime_home))
    )
    b.ensure_ready()
    records = []
    original = b.execute

    def observed(tool, args=None):
        out = original(tool, args)
        records.append(
            {
                "tool": tool,
                "ok": out["ok"],
                "error_code": (out.get("error") or {}).get("code"),
                "timing": out.get("timing"),
                "risk_level": out.get("risk_level"),
            }
        )
        return out

    b.execute = observed
    yield b, work
    owned_pid = b.rpc.proc.pid if b.rpc else None
    b.close()
    guard.shutdown()
    guard.server_close()
    report = {
        "tests_scope": "actual official runtime, synthetic test-owned project, no agent turns",
        "runtime": b.runtime.as_dict(),
        "effective_sandbox": "dangerFullAccess",
        "configured_model_endpoint_request_count": len(attempts),
        "configured_model_endpoint_requests": attempts,
        "unreviewed_rpc_calls": {
            k: v for k, v in b.audit.methods.items() if k not in ALLOWED_METHODS
        },
        "outgoing_rpc_counts": b.audit.methods,
        "tool_calls": records,
        "owned_appserver_pid": owned_pid,
        "owned_appserver_stopped": b.rpc.proc.poll() is not None,
        "full_process_network_trace": "not_performed",
        "workspace": str(work),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    profile = os.environ.get('CCM_TEST_PROFILE')
    assert profile in (None, 'standard', 'admin')
    name = 'integration-' + profile + '.json' if profile else 'integration-evidence.json'
    report['acceptance_profile'] = profile or 'unprofiled'
    from codex_control_mcp import __version__
    report['bridge_version'] = __version__
    (ROOT / 'evidence' / name).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    assert not attempts, "The configured model provider received a request attempt"
    assert not report["unreviewed_rpc_calls"]


def ok(b, tool, args=None):
    out = b.execute(tool, args)
    assert out["ok"], json.dumps(out, ensure_ascii=False)
    return out["result"]


def wait_session(b, sid, needle=None, seconds=8, terminated=False):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        receipt = b.execute("session_read", {"session_id": sid})
        d = receipt["result"]
        if not receipt["ok"]:
            assert terminated and d is not None, receipt
            assert d["state"] == "exited" and d["exit_code"] not in (None, 0), receipt
            assert receipt["error"]["code"] == "command_failed", receipt
            assert receipt["diagnostics"]["failure_origin"] == "command_process", receipt
        elif d["state"] == "exited":
            assert d["exit_code"] == 0, receipt
        if needle is not None and needle in d["stdout"]:
            return d
        if needle is None and d["state"] not in ("starting", "running"):
            return d
        time.sleep(0.05)
    pytest.fail("Owned session did not reach the expected observed state")


def test_actual_health_full_permissions(live):
    b, w = live
    d = ok(b, "codex_health", {"active": True})
    assert d["checks"]["shell"] == "PASS"
    from codex_control_mcp.common import is_admin
    if os.environ.get("CCM_REQUIRE_ADMIN") == "1":
        assert is_admin(), "This acceptance profile requires an elevated owner process"
    assert d["child_is_admin_verified"] is is_admin()
    assert d["effective_sandbox"] == "dangerFullAccess"
    assert d["proxy"]["child_environment_verified"] is True


@pytest.mark.parametrize(
    "shell,command",
    [
        ("powershell", "Write-Output 'CODEX_CONTROL_MCP_OK'"),
        ("cmd", "echo CODEX_CONTROL_MCP_OK"),
    ],
)
def test_shell_marker(live, shell, command):
    b, w = live
    d = ok(b, "exec_command", {"shell": shell, "command": command})
    assert "CODEX_CONTROL_MCP_OK" in d["stdout"]


def test_nonzero_is_not_success(live):
    b, w = live
    d = b.execute("exec_command", {"command": "exit 7"})
    assert not d["ok"] and d["result"]["exit_code"] == 7


def test_files_utf8_crlf_bom_and_no_overwrite(live):
    b, w = live
    p = w / "中文 ' 文件.txt"
    text = "\ufeff第一行\r\n第二行\n"
    d = ok(b, "file_add", {"path": str(p), "content": text})
    assert d["sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert ok(b, "read_file", {"path": str(p)})["content"] == text
    assert not b.execute("file_add", {"path": str(p), "content": "must not replace"})[
        "ok"
    ]
    assert ok(b, "read_file", {"path": str(p)})["content"] == text
    one = ok(b, "read_file", {"path": str(p), "start_line": 2, "end_line": 2})
    assert one["content"] == "第二行\n"
    moved = w / "moved.txt"
    ok(b, "file_move", {"source": str(p), "destination": str(moved)})
    assert not p.exists() and moved.exists()
    ok(b, "file_delete", {"path": str(moved)})
    assert not moved.exists()


def test_large_native_file_write(live):
    b, w = live
    p = w / "large.txt"
    text = "中文 LARGE FILE\n" * 10000
    d = ok(b, "file_add", {"path": str(p), "content": text})
    assert d["bytes"] == len(text.encode())
    r = ok(b, "read_file", {"path": str(p), "max_lines": 20000, "max_bytes": 1048576})
    assert r["content"] == text


def test_read_limits_and_pagination(live):
    b, w = live
    p = w / "lines.txt"
    ok(b, "file_add", {"path": str(p), "content": "line\n" * 450})
    d = ok(b, "read_file", {"path": str(p), "max_lines": 200})
    assert d["truncated"] and d["next_line"] == 201
    directory = ok(b, "list_dir", {"path": str(w), "limit": 1})
    assert len(directory["entries"]) == 1 and directory["next_offset"] == 1


def test_native_search(live):
    b, w = live
    d = ok(b, "search_text", {"path": str(w), "query": "LARGE FILE", "max_results": 3})
    assert len(d["matches"]) == 3 and d["truncated"]
    f = ok(b, "search_files", {"path": str(w), "glob": "*.txt"})
    assert any(x.endswith("large.txt") for x in f["matches"])


def test_atomic_concurrent_create(live):
    b, w = live
    p = str(w / "concurrent.txt")
    with concurrent.futures.ThreadPoolExecutor(2) as ex:
        r = list(
            ex.map(
                lambda text: b.execute("file_add", {"path": p, "content": text}),
                ["first", "second"],
            )
        )
    assert sum(x["ok"] for x in r) == 1
    assert ok(b, "read_file", {"path": p})["content"] in ("first", "second")


def test_idempotent_mutation(live):
    b, w = live
    a = {
        "path": str(w / "idempotent.txt"),
        "content": "one",
        "idempotency_key": "integration-create-key",
    }
    d = b.execute("file_add", a)
    assert d["ok"]
    again = b.execute("file_add", a)
    assert again["ok"] and again["idempotent_replay"]
    conflict = b.execute("file_add", {**a, "content": "two"})
    assert not conflict["ok"] and conflict["error"]["code"] == "idempotency_conflict"


def test_stream_stdin_and_output(live):
    b, w = live
    s = ok(
        b,
        "session_start",
        {
            "command": "[Console]::InputEncoding=[Text.UTF8Encoding]::new($false);[Console]::WriteLine('READY');$s=[Console]::ReadLine();[Console]::WriteLine('ECHO:'+$s)",
            "timeout_ms": 15000,
        },
    )
    sid = s["session_id"]
    wait_session(b, sid, "READY")
    ok(
        b,
        "session_write",
        {"session_id": sid, "text": "中文输入\n", "close_stdin": True},
    )
    r = wait_session(b, sid)
    assert r["exit_code"] == 0 and "ECHO:中文输入" in r["stdout"]


def test_terminate_and_session_ownership(live):
    b, w = live
    s = ok(
        b,
        "session_start",
        {
            "command": "Write-Output 'WAITING';Start-Sleep -Seconds 60",
            "timeout_ms": 65000,
        },
    )
    sid = s["session_id"]
    wait_session(b, sid, "WAITING")
    ok(b, "session_kill", {"session_id": sid})
    r = wait_session(b, sid, terminated=True)
    assert r["state"] == "exited"
    assert not b.execute("session_kill", {"session_id": "not-owned"})["ok"]


def test_timeout_does_not_fake_success(live):
    b, w = live
    r = b.execute(
        "exec_command", {"command": "Start-Sleep -Seconds 10", "timeout_ms": 100}
    )
    assert not r["ok"]
    assert r.get("error")


def test_git_patch_commit_and_staged_isolation(live):
    b, w = live
    repo = w / "repo"
    repo.mkdir()
    git = b.runtime.git_path
    assert git
    for argv in (
        [git, "init", str(repo)],
        [git, "-C", str(repo), "config", "user.name", "Codex-Control-MCP Test"],
        [git, "-C", str(repo), "config", "user.email", "test@example.invalid"],
        [git, "-C", str(repo), "config", "core.autocrlf", "false"],
        [git, "-C", str(repo), "config", "commit.gpgsign", "false"],
        [git, "-C", str(repo), "config", "core.hooksPath", str(repo / "empty-hooks")],
    ):
        ok(b, "exec_command", {"argv": argv, "cwd": str(repo)})
    ok(b, "file_add", {"path": str(repo / "a.txt"), "content": "one\n"})
    ok(
        b,
        "file_add",
        {"path": str(repo / "b.txt"), "content": "staged but not committed\n"},
    )
    ok(b, "exec_command", {"argv": [git, "add", "b.txt"], "cwd": str(repo)})
    ok(
        b,
        "git_commit",
        {"cwd": str(repo), "message": "test: first explicit file", "paths": ["a.txt"]},
    )
    tree = ok(
        b,
        "exec_command",
        {"argv": [git, "ls-tree", "--name-only", "HEAD"], "cwd": str(repo)},
    )["stdout"]
    assert "a.txt" in tree and "b.txt" not in tree
    status = ok(b, "git_status", {"cwd": str(repo)})["stdout"]
    assert "A  b.txt" in status
    patch = "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-one\n+two\n"
    ok(
        b,
        "file_patch",
        {
            "cwd": str(repo),
            "patch": patch,
            "expected_sha256": {"a.txt": digest(b"one\n")},
        },
    )
    assert ok(b, "read_file", {"path": str(repo / "a.txt")})["content"] == "two\n"
    diff = ok(b, "git_diff", {"cwd": str(repo)})["stdout"]
    assert "+two" in diff and "-one" in diff
    conflict = b.execute(
        "file_patch",
        {"cwd": str(repo), "patch": patch, "expected_sha256": {"a.txt": "0" * 64}},
    )
    assert not conflict["ok"] and conflict["error"]["code"] == "concurrent_modification"
    ok(b, "git_branch", {"cwd": str(repo), "action": "create", "name": "test-branch"})
    assert "test-branch" in ok(b, "git_branch", {"cwd": str(repo)})["stdout"]
    assert "first explicit file" in ok(b, "git_log", {"cwd": str(repo)})["stdout"]


def test_rpc_denial_and_gui_unavailable(live):
    b, w = live
    for method in ("turn/start", "review/start", "process/spawn"):
        with pytest.raises(BridgeError):
            b.rpc.begin(method, {})
    d = b.execute("computer_snapshot", {})
    assert not d["ok"] and d["error"]["code"] == "capability_unavailable"


def test_secret_not_logged(live):
    b, w = live
    canary = "CCM_SECRET_" + uuid.uuid4().hex
    d = ok(
        b,
        "exec_command",
        {
            "command": "[Console]::Write($env:CCM_TEST_SECRET.Length)",
            "env": {"CCM_TEST_SECRET": canary},
        },
    )
    assert d["stdout"] == str(len(canary))
    for p in (b.cfg.home / "logs").glob("*"):
        if p.is_file():
            assert canary.encode() not in p.read_bytes()


def test_schema_refresh_same_version_and_reconnect(live):
    b, w = live
    old = b.schema.hash
    pid = b.rpc.proc.pid
    d = ok(b, "codex_health", {"refresh": True, "active": True})
    assert b.schema.hash == old and b.rpc.proc.pid != pid
    assert d["schema"]["difference"]["structural_status"] == "unchanged"
    previous = b.rpc.generation
    b.rpc.close()
    d = ok(b, "exec_command", {"command": "Write-Output 'RECONNECTED'"})
    assert "RECONNECTED" in d["stdout"] and b.rpc.generation != previous


def test_warm_conversion_overhead(live):
    b, w = live
    values = []
    for i in range(12):
        r = b.execute("list_dir", {"path": str(w), "limit": 5})
        assert r["ok"]
        values.append(r["timing"]["bridge_overhead_ms"])
    p95 = sorted(values)[int(0.95 * (len(values) - 1))]
    (ROOT / "evidence/performance.json").write_text(
        json.dumps(
            {
                "scope": "warm list_dir protocol conversion, actual official RPC wait subtracted",
                "samples_ms": values,
                "p95_ms": p95,
                "target_ms": 100,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    assert p95 < 100
