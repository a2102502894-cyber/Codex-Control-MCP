"""Reproduce real pipe stalls, lost status and routing errors in owned fixtures."""
import asyncio
import base64
import concurrent.futures
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from codex_control_mcp.bridge import Bridge
from codex_control_mcp.config import Config
from codex_control_mcp.errors import BridgeError
from codex_control_mcp.host_runtime import HostManager
from codex_control_mcp.rpc import AppServer
from codex_control_mcp.rpc_writer import PipeWriter


def rpc_fixture(stream=None):
    app = AppServer.__new__(AppServer)
    app.closed = app.broken = False
    app.generation = "owned-test"
    app.lock = threading.RLock()
    app.write_lock = threading.Lock()
    app.next_id = 0
    app.pending = {}
    app.audit = SimpleNamespace(emit=lambda *a, **k: None)
    app.schema = SimpleNamespace(validate=lambda *a: None, validate_response=lambda *a: None)
    app.proc = SimpleNamespace(poll=lambda: None, stdin=stream)
    return app


def test_real_pipe_dispatch_is_bounded_and_cleanup_completes():
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(write_fd, "wb")
    app = rpc_fixture(stream)
    params = {"payload": "x" * 131072}
    raw = (json.dumps({"id": 1, "method": "fs/readFile", "params": params}, separators=(",", ":")) + "\n").encode()
    started = time.monotonic()
    reader = None
    try:
        with pytest.raises(BridgeError) as caught:
            app.call("fs/readFile", params, timeout=0.03)
        assert time.monotonic() - started < 1
        assert caught.value.code == "execution_state_unknown"
        assert not app.pending and not app.uncertain
    finally:
        def drain():
            count = 0
            while count < len(raw):
                data = os.read(read_fd, 4096)
                if not data:
                    break
                count += len(data)
        # The worker may have begun writing or the request may still be queued.
        if getattr(app, "writer", None) and app.writer.active:
            reader = threading.Thread(target=drain, daemon=True)
            reader.start()
        if hasattr(app, "writer"):
            app.writer.stop()
            closer = app.writer.close_stream_when_drained()
            closer.join(2)
            assert not closer.is_alive()
        else:
            stream.close()
        if reader:
            reader.join(2)
            assert not reader.is_alive()
        os.close(read_fd)


def test_queued_expired_mutation_is_never_dispatched_later():
    entered, release = threading.Event(), threading.Event()
    writes, failures = [], []
    class Sink:
        def write(self, raw):
            writes.append(bytes(raw))
            entered.set()
            release.wait(2)
            return len(raw)
        def flush(self):
            pass
        def close(self):
            pass
    writer = PipeWriter(Sink(), timeout=0.05)
    try:
        writer.submit(b"first", lambda: None, failures.append)
        assert entered.wait(1)
        writer.submit(b"must-not-run", lambda: None, failures.append)
        deadline = time.monotonic() + 1
        while len(failures) < 2 and time.monotonic() < deadline:
            threading.Event().wait(0.005)
        assert len(failures) == 2
        assert writer.observation()["write_stalled"]
        with pytest.raises(BridgeError):
            writer.submit(b"third", lambda: None, failures.append)
    finally:
        release.set()
        writer.stop()
        writer.thread.join(2)
    assert writes == [b"first"] and writer.bytes == 0


def test_read_timeouts_do_not_fill_pending_or_pin_updates():
    app = rpc_fixture()
    app._write = lambda message: None
    for _ in range(300):
        with pytest.raises(BridgeError):
            app.call("fs/readFile", {}, timeout=0.00001)
    assert not app.pending and not app.uncertain
    assert len(app.abandoned) == 256
    next_request = app.begin("fs/readDirectory", {})
    assert next_request and len(app.pending) == 1


def test_unknown_mutations_are_retained_without_blocking_reads_and_late_result_reconciles():
    app = rpc_fixture()
    app._write = lambda message: None
    app.callback = lambda message: None
    with pytest.raises(BridgeError):
        app.call("command/exec", {}, timeout=0.00001)
    assert not app.pending and len(app.uncertain) == 1
    read = app.begin("fs/readFile", {})
    app.proc.stdout = io.BytesIO(b'{"id":1,"result":{"exitCode":0}}\n{"id":2,"result":{}}\n')
    app._read()
    assert not app.uncertain and read.result() == {}


def test_unknown_mutation_limit_does_not_disable_read_only_recovery():
    app = rpc_fixture()
    app._write = lambda message: None
    for _ in range(256):
        with pytest.raises(BridgeError):
            app.call("command/exec", {}, timeout=0.00001)
    with pytest.raises(BridgeError) as caught:
        app.begin("command/exec", {})
    assert caught.value.code == "resource_limit"
    assert app.begin("fs/readFile", {})
    assert len(app.uncertain) == 256 and len(app.pending) == 1


@pytest.mark.parametrize("tool,payload,result", [
    ("host_exec", {"host": "local", "command": "owned"}, {"result": {"exit_code": 7}}),
    ("host_files", {"host": "local", "action": "read", "path": "owned"}, {"result": {"exit_code": 5}}),
    ("mcp_tool_call", {"name": "owned:tool", "arguments": {}}, {"result": {"isError": True, "content": []}}),
    ("host_exec", {"host": "owned", "command": "owned"}, {"result": {"result": {"structuredContent": {"ok": False, "error": {"code": "owned_failure", "message": "failed"}}}}}),
])
def test_routed_failures_are_not_reported_as_success(tmp_path, tool, payload, result):
    bridge = Bridge(Config(home=tmp_path / "home", cwd=str(tmp_path)))
    bridge._do = lambda *args: result
    try:
        receipt = bridge.execute(tool, payload)
        assert not receipt["ok"] and receipt["error"]
        if result["result"].get("exit_code"):
            assert receipt["result"]["exit_code"] == result["result"]["exit_code"]
            assert receipt["diagnostics"]["execution_status"] == "failed_or_unconfirmed"
    finally:
        bridge.close()


def test_nested_running_session_keeps_next_action(tmp_path):
    bridge = Bridge(Config(home=tmp_path / "home", cwd=str(tmp_path)))
    data = {"session_id": "owned", "state": "running", "completed": False,
            "origin_operation_id": "original", "next_cursor": 4,
            "next_action": {"tool": "session_read", "arguments": {"session_id": "owned", "cursor": 4}}}
    bridge._do = lambda *args: {"host": "local", "result": data}
    try:
        receipt = bridge.execute("host_exec", {"host": "local", "command": "owned"})
        assert receipt["ok"] and receipt["result"]["next_action"] == data["next_action"]
        assert receipt["diagnostics"]["execution_status"] == "running"
        assert receipt["diagnostics"]["execution_operation_id"] == "original"
    finally:
        bridge.close()


def test_promoted_receipt_still_exposes_original_output_page():
    from codex_control_mcp.results import execution_view, promote_execution
    page = {"state": "running", "session_id": "owned", "completed": False, "stdout": "owned output", "stderr": ""}
    receipt = promote_execution({"host": "node", "result": {"result": {"structuredContent": {"ok": True, "result": page}}}})
    assert receipt["state"] == "running"
    assert execution_view(receipt) is page and execution_view(receipt)["stdout"] == "owned output"


def test_mcp_is_error_flag_does_not_hide_precise_nested_exit_code():
    from codex_control_mcp.results import execution_error
    precise = {"code": "command_failed", "message": "failed", "retryable": False, "details": {"exit_code": 8}}
    result = {"result": {"isError": True, "structuredContent": {"ok": False, "error": precise, "result": {"exit_code": 8}}}}
    assert execution_error(result) == precise


def manager_fixture(tmp_path, platform="linux", transport="ssh"):
    calls = []
    bridge = SimpleNamespace(cfg=SimpleNamespace(home=tmp_path), _do=lambda tool, args: calls.append(args) or {"exit_code": 0})
    manager = HostManager(bridge, None)
    manager.hosts["owned"] = {"name": "owned", "transport": transport, "platform": platform,
                              "user": "fixture", "address": "test.invalid", "port": 22,
                              "container": "owned", "enabled": True}
    return manager, calls


@pytest.mark.parametrize("transport", ["ssh", "docker"])
def test_remote_execution_options_are_preserved(tmp_path, transport):
    manager, calls = manager_fixture(tmp_path, transport=transport)
    manager.exec({"host": "owned", "command": "echo marker", "timeout_ms": 500,
                  "execution_mode": "auto", "yield_time_ms": 0, "output_limit_bytes": 1024,
                  "cwd": "/owned path", "env": {"OWNED": "x y", "REMOVE": None}})
    actual = calls[0]
    assert actual["timeout_ms"] == 500 and actual["execution_mode"] == "auto"
    assert actual["yield_time_ms"] == 0 and actual["output_limit_bytes"] == 1024
    remote = actual["argv"][-1]
    assert "owned path" in remote and "export OWNED=" in remote and "unset REMOVE" in remote


def test_ssh_command_survives_shell_joining_without_remote_network(tmp_path):
    shell = shutil.which("sh") or (str(Path(r"C:\Program Files\Git\usr\bin\sh.exe")) if Path(r"C:\Program Files\Git\usr\bin\sh.exe").is_file() else None)
    if not shell:
        pytest.skip("POSIX shell unavailable")
    manager, calls = manager_fixture(tmp_path)
    marker = "中文 'quoted' $HOME ; not-a-command"
    manager.exec({"host": "owned", "argv": ["printf", "%s", marker]})
    env = {**os.environ, "PATH": str(Path(shell).parent) + os.pathsep + os.environ.get("PATH", ""), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
    result = subprocess.run([shell, "-c", calls[0]["argv"][-1]], capture_output=True, timeout=5, env=env)
    assert result.returncode == 0 and result.stdout.decode() == marker


def test_windows_remote_argv_uses_powershell_quoting_and_encoded_command(tmp_path):
    manager, calls = manager_fixture(tmp_path, platform="windows")
    manager.exec({"host": "owned", "argv": ["C:\\owned path\\tool.exe", "中文 'quoted' $HOME"], "env": {"OWNED": "x'$y"}})
    remote = calls[0]["argv"][-1]
    script = base64.b64decode(remote.split()[-1]).decode("utf-16-le")
    assert "& 'C:\\owned path\\tool.exe' '中文 ''quoted'' $HOME'" in script
    assert "$ErrorActionPreference='Stop'" in script and "'x''$y'" in script


@pytest.mark.parametrize("extra", [{"tty": True}, {"wsl_cwd": "/owned"}, {"shell": "cmd"}])
def test_unsupported_remote_execution_option_is_rejected_before_dispatch(tmp_path, extra):
    manager, calls = manager_fixture(tmp_path)
    with pytest.raises(BridgeError):
        manager.exec({"host": "owned", "command": "owned", **extra})
    assert not calls


def test_remote_files_do_not_silently_ignore_range_filters_or_overwrite(tmp_path):
    manager, calls = manager_fixture(tmp_path)
    with pytest.raises(BridgeError):
        manager.files({"host": "owned", "action": "read", "path": "/owned", "offset": 1})
    assert not calls
    manager.files({"host": "owned", "action": "write", "path": "/owned", "content": "data"})
    assert "set -C" in calls[0]["argv"][-1]


def test_long_host_command_does_not_block_unrelated_file_operations(tmp_path):
    bridge = Bridge(Config(home=tmp_path / "home", cwd=str(tmp_path)))
    entered, release = threading.Event(), threading.Event()
    def execute(tool, args):
        if tool == "host_exec":
            entered.set()
            release.wait(2)
        return {"exit_code": 0}
    bridge._do = execute
    pool = concurrent.futures.ThreadPoolExecutor(2)
    try:
        host = pool.submit(bridge.execute, "host_exec", {"host": "local", "command": "owned"})
        assert entered.wait(1)
        write = pool.submit(bridge.execute, "file_add", {"path": "owned", "content": "owned"})
        assert write.result(0.5)["ok"]
    finally:
        release.set()
        pool.shutdown()
        bridge.close()


def test_running_service_overall_deadline_does_not_replay(tmp_path, monkeypatch):
    from codex_control_mcp import client
    from test_client import Context, setup_client
    cfg = setup_client(tmp_path, monkeypatch, "tool")
    calls = []
    class Session(Context):
        async def initialize(self):
            return SimpleNamespace(serverInfo=SimpleNamespace(name="Codex-Control-MCP"))
        async def call_tool(self, *args):
            calls.append(1)
            await asyncio.sleep(2)
        async def __aenter__(self):
            return self
    monkeypatch.setattr(client, "ClientSession", lambda *a: Session())
    monkeypatch.setattr(client, "LOCAL_CALL_TIMEOUT_SECONDS", 0.02)
    with pytest.raises(BridgeError) as caught:
        asyncio.run(client.call_running_service(cfg, "file_add", {"path": "owned", "content": "data"}))
    assert caught.value.code == "execution_state_unknown" and calls == [1]


def test_tabbit_dispatch_stall_cannot_hide_response_deadline(monkeypatch):
    from codex_control_mcp import browser_tabbit
    import queue
    entered, release = threading.Event(), threading.Event()
    class Sink:
        def write(self, raw):
            entered.set()
            release.wait(2)
            return len(raw)
        def flush(self):
            pass
        def close(self):
            pass
    browser = browser_tabbit.TabbitBrowser.__new__(browser_tabbit.TabbitBrowser)
    browser.closed = False
    browser.lock = threading.RLock()
    browser.sequence = 0
    browser.responses = queue.Queue(maxsize=256)
    browser.proc = SimpleNamespace(poll=lambda: None, stdin=Sink())
    closed = []
    browser.close = lambda force=False: closed.append(force)
    monkeypatch.setattr(browser_tabbit, "REQUEST_TIMEOUT_SECONDS", 0.03)
    started = time.monotonic()
    try:
        with pytest.raises(BridgeError) as caught:
            browser._request("browser_fill", {"text": "owned"})
        assert entered.is_set() and time.monotonic() - started < 1
        assert caught.value.code == "execution_state_unknown" and closed == [True]
    finally:
        release.set()
        browser.writer.stop()
        browser.writer.thread.join(2)


@pytest.mark.parametrize("frame", [[], {"id": 1, "error": "bad"}, {"id": 1, "result": {}, "error": {}}])
def test_tabbit_malformed_response_fails_fast(frame):
    from codex_control_mcp.browser_tabbit import TabbitBrowser
    import queue
    browser = TabbitBrowser.__new__(TabbitBrowser)
    browser.responses = queue.Queue(maxsize=256)
    browser.proc = SimpleNamespace(stdout=io.BytesIO((json.dumps(frame) + "\n").encode()))
    browser._read()
    assert browser.responses.get_nowait() is None


@pytest.mark.parametrize("observation", [{"write_stalled": True}, {"unresolved_operations": 1}])
def test_transport_stall_or_unknown_operation_cannot_report_healthy(observation):
    from test_health_proxy_evidence import make_bridge, GOOD
    bridge = make_bridge({"exit_code": 0, "stdout": json.dumps(GOOD)})
    bridge.rpc.observation = lambda: observation
    out = bridge.health(active=True)
    assert out["overall"] == "degraded" and not out["phase1_complete"]
    assert out["execution_transport"] == observation


def test_remote_mcp_session_is_polled_on_its_originating_node(tmp_path):
    from codex_control_mcp.dynamic_mcp import DynamicMCPManager
    manager = DynamicMCPManager(SimpleNamespace(home=tmp_path, cwd=str(tmp_path)))
    manager.servers["node"] = {"name": "node", "enabled": True, "tools": [
        {"name": "exec_command", "inputSchema": {"type": "object"}},
        {"name": "session_read", "inputSchema": {"type": "object"}}]}
    inner = {"state": "running", "session_id": "remote-owned", "completed": False,
             "next_action": {"tool": "session_read", "arguments": {"session_id": "remote-owned", "cursor": 2}}}
    manager._run = lambda *args, **kw: {"structuredContent": {"ok": True, "result": inner}}
    bridge = Bridge(Config(home=tmp_path / "bridge", cwd=str(tmp_path)))
    bridge.hosts.dynamic_mcp = manager
    bridge.hosts.hosts["remote"] = {"name": "remote", "transport": "mcp", "mcp_server": "node", "enabled": True}
    try:
        out = bridge.execute("host_exec", {"host": "remote", "command": "owned"})
        assert out["ok"] and out["result"]["state"] == "running"
        next_action = out["result"]["next_action"]
        assert next_action["tool"] == "mcp_tool_call"
        assert next_action["arguments"]["name"] == "node:session_read"
        assert next_action["arguments"]["arguments"] == {"session_id": "remote-owned", "cursor": 2}
        assert len(next_action["arguments"]["expected_connection_digest"]) == 64
        direct = manager.call({"name": "node:exec_command", "arguments": {}})
        assert direct["next_action"] == next_action
    finally:
        bridge.close()
