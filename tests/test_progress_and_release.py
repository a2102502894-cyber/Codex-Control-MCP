"""Progress visibility and desktop lifecycle regressions; no user input injected."""
import asyncio
import concurrent.futures
import threading
import time
from types import SimpleNamespace

import anyio
import pytest
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from codex_control_mcp.bridge import Bridge
from codex_control_mcp.computer import OfficialComputer
from codex_control_mcp.errors import BridgeError
from codex_control_mcp.server import make_server, execute_with_progress
from codex_control_mcp.sessions import Session
from codex_control_mcp.tools import validate_tool


def auto_bridge(session):
    b = Bridge.__new__(Bridge)
    b.cfg = SimpleNamespace(output_limit_bytes=1024)
    b.sessions = SimpleNamespace(get=lambda sid: session)
    calls = []
    def start(args):
        calls.append(args)
        return session.metadata()
    b._session_start = start
    return b, calls


def test_auto_returns_partial_output_and_continuation_without_replaying():
    s = Session('probe', 'g', '.', 4096)
    s.append({'deltaBase64': 'c3RhcnQK'})
    b, calls = auto_bridge(s)
    r = b._do('exec_command', {'argv': ['fixture'], 'yield_time_ms': 0})
    assert len(calls) == 1 and calls[0]['timeout_ms'] == 30000
    assert r['stdout'] == 'start\n' and not r['completed']
    assert r['exit_code'] is None and r['next_action']['arguments']['cursor'] == 1
    s.append({'deltaBase64': 'ZW5kCg=='})
    f = concurrent.futures.Future(); f.set_result({'exitCode': 7}); s.finish(f)
    page = s.read(r['next_cursor'])
    assert page['stdout'] == 'end\n' and page['exit_code'] == 7
    assert len(calls) == 1


def test_auto_preserves_completed_failure_and_buffered_compatibility():
    s = Session('probe', 'g', '.', 4096)
    f = concurrent.futures.Future(); f.set_result({'exitCode': 9}); s.finish(f)
    b, calls = auto_bridge(s)
    r = b._do('exec_command', {'argv': ['fixture']})
    assert r['completed'] and r['exit_code'] == 9 and r['next_action'] is None
    b._argv = lambda a: a['argv']
    b._command_environment = lambda a: None
    b._command = lambda *args: {'exit_code': 11}
    assert b._do('exec_command', {'argv': ['fixture'], 'execution_mode': 'buffered'})['exit_code'] == 11
    assert len(calls) == 1


def test_failed_session_is_never_reported_as_success():
    s = Session('probe', 'g', '.', 4096)
    s.error = {'code': 'execution_state_unknown', 'message': 'lost'}
    s.state = 'lost'; s.finished.set()
    b, _ = auto_bridge(s)
    with pytest.raises(BridgeError, match='lost'):
        b._command_auto({'argv': ['fixture']})


def test_new_contracts_reject_invalid_waits():
    validate_tool('computer_close', {})
    validate_tool('exec_command', {'argv': ['fixture'], 'execution_mode': 'auto', 'yield_time_ms': 0})
    for bad in [-1, 10001, True]:
        with pytest.raises(BridgeError):
            validate_tool('exec_command', {'argv': ['fixture'], 'yield_time_ms': bad})


def test_progress_arrives_before_result_and_metadata_is_present(monkeypatch):
    import codex_control_mcp.server as module
    monkeypatch.setattr(module, 'PROGRESS_INTERVAL_SECONDS', 0.02)
    events = []
    calls = []
    def execute(name, args):
        calls.append(name); time.sleep(0.12)
        events.append('finished')
        return {'ok': True, 'result': {'exit_code': 0}}
    bridge = SimpleNamespace(cfg=SimpleNamespace(oauth={}, browser_use_enabled=False,
        computer_use_enabled=True), execute=execute)
    server = make_server(bridge)
    async def run():
        async with create_client_server_memory_streams() as (cs, ss):
            async with anyio.create_task_group() as group:
                group.start_soon(server.run, *ss, server.create_initialization_options())
                async with ClientSession(*cs) as client:
                    await client.initialize()
                    tools = await client.list_tools()
                    command = next(t for t in tools.tools if t.name == 'exec_command')
                    assert command.meta['openai/toolInvocation/invoking']
                    async def progress(value, total, message):
                        events.append(('progress', value, message))
                    r = await client.call_tool('exec_command', {'argv': ['fixture']}, progress_callback=progress)
                    assert r.structuredContent['ok']
                group.cancel_scope.cancel()
    asyncio.run(run())
    assert calls == ['exec_command']
    assert events[0][0] == 'progress' and len(events) > 2
    assert events[-1] == 'finished'


def test_broken_progress_consumer_does_not_cancel_or_replay():
    calls = []
    async def broken(*args, **kw):
        raise RuntimeError('client disconnected')
    ctx = SimpleNamespace(meta=SimpleNamespace(progressToken=0), request_id=1,
                          session=SimpleNamespace(send_progress_notification=broken))
    bridge = SimpleNamespace(execute=lambda *args: calls.append(args) or {'ok': True})
    assert asyncio.run(execute_with_progress(bridge, ctx, 'exec_command', {}))['ok']
    assert len(calls) == 1


def gui_bridge(monkeypatch):
    b = Bridge.__new__(Bridge)
    b.cfg = SimpleNamespace(computer_use_enabled=True)
    b.gui = None; b.gui_timer = None; b.gui_lock = threading.RLock()
    b.gui_idle_seconds = 0.1; b.ensure_ready = lambda: None
    b.audit = SimpleNamespace(emit=lambda *args, **kw: None)
    made = []
    class GUI:
        def __init__(self, cfg):
            self.closed = False; self.thread = SimpleNamespace(is_alive=lambda: not self.closed)
            self.closed_event = threading.Event(); self.close_count = 0; made.append(self)
        def call(self, tool, args, forwarder):
            return {'snapshot_id': 'fixture'}
        def close(self):
            self.close_count += 1; self.closed = True; self.closed_event.set()
    monkeypatch.setattr('codex_control_mcp.computer.OfficialComputer', GUI)
    return b, made


def test_close_is_idempotent_and_next_snapshot_reconnects(monkeypatch):
    b, made = gui_bridge(monkeypatch)
    try:
        b._do('computer_snapshot', {})
        assert b._do('computer_close', {})['closed'] and made[0].closed
        assert b._do('computer_close', {})['already_closed']
        b._do('computer_snapshot', {})
        assert len(made) == 2 and made[0].close_count == 1
    finally:
        b._close_computer()


def test_idle_release_after_success_and_failure(monkeypatch):
    b, made = gui_bridge(monkeypatch)
    b._do('computer_snapshot', {})
    assert made[0].closed_event.wait(2)
    b._do('computer_snapshot', {})
    def fail(*args):
        raise BridgeError('execution_state_unknown', 'fixture')
    made[1].call = fail
    with pytest.raises(BridgeError):
        b._do('computer_click', {})
    assert made[1].closed_event.wait(2)
    with b.gui_lock:
        assert b.gui is None


def test_old_timer_cannot_close_a_new_connection(monkeypatch):
    b, made = gui_bridge(monkeypatch)
    b._do('computer_snapshot', {}); old = made[0]
    b._close_computer(); b._do('computer_snapshot', {})
    try:
        b._idle_close_computer(old)
        assert not made[1].closed
    finally:
        b._close_computer()


def test_runtime_failure_does_not_cancel_idle_cleanup(monkeypatch):
    b, made = gui_bridge(monkeypatch)
    b._do('computer_snapshot', {})
    def fail():
        raise BridgeError('runtime_missing', 'fixture')
    b.ensure_ready = fail
    with pytest.raises(BridgeError):
        b._do('computer_snapshot', {})
    assert made[0].closed_event.wait(2)


def test_close_failure_keeps_connection_and_does_not_claim_exit(monkeypatch):
    b, made = gui_bridge(monkeypatch)
    b._do('computer_snapshot', {})
    def fail():
        raise BridgeError('execution_state_unknown', 'still alive')
    made[0].close = fail
    with pytest.raises(BridgeError):
        b._close_computer()
    assert b.gui is made[0] and b.gui_timer is None


def test_adapter_reports_unfinished_shutdown_instead_of_false_success():
    c = OfficialComputer.__new__(OfficialComputer)
    c.closed = True; c.loop = None; c.main_task = None
    c.thread = SimpleNamespace(join=lambda seconds: None, is_alive=lambda: True)
    with pytest.raises(BridgeError):
        c.close()
