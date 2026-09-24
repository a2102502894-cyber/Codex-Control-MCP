"""Regression tests for truthful receipts and correlation, not platform bypasses.

Protocol error frames below are synthetic unit-test fixtures. They must never be
reported as live OpenAI safety decisions.
"""
import asyncio
import concurrent.futures
import io
import json
import threading
from types import SimpleNamespace

import anyio
import pytest
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from codex_control_mcp.bridge import Bridge
from codex_control_mcp.common import Audit, CURRENT_OPERATION
from codex_control_mcp.errors import BridgeError
from codex_control_mcp.idempotency import Idempotency
from codex_control_mcp.rpc import AppServer
from codex_control_mcp.server import make_server


@pytest.fixture
def bridge(tmp_path):
    b = Bridge.__new__(Bridge)
    b.audit = Audit(tmp_path / 'audit.jsonl')
    b.idempotency = Idempotency(tmp_path / 'idempotency.sqlite3')
    b.write_lock = threading.RLock()
    b.cfg = SimpleNamespace(browser={}, oauth={}, browser_use_enabled=False,
                            computer_use_enabled=False)
    b.runtime = None
    b.schema = None
    b.rpc = None
    b.proxy = {}
    b._do = lambda *args: {'exit_code': 0}
    yield b
    b.idempotency.close()


def rows(b):
    return [json.loads(line) for line in b.audit.path.read_text('utf-8').splitlines()]


def test_receipt_exposes_operation_id_and_matches_audit(bridge):
    out = bridge.execute('exec_command', {'argv': ['test-fixture']})
    assert out['ok']
    assert len(out['operation_id']) == 32
    assert out['diagnostics']['bridge_received'] is True
    assert {r['operation_id'] for r in rows(bridge)} == {out['operation_id']}
    assert out['diagnostics']['upstream_safety_decision'] == 'not_observable'


def test_invalid_input_has_received_record_and_known_local_origin(bridge):
    out = bridge.execute('read_file', {})
    assert not out['ok'] and out['error']['code'] == 'invalid_arguments'
    received = [r for r in rows(bridge) if r['event'] == 'tool_received']
    assert len(received) == 1
    assert received[0]['operation_id'] == out['operation_id']
    assert out['diagnostics']['failure_origin'] == 'bridge_validation'
    assert out['diagnostics']['rpc_dispatched_count'] == 0


def test_replay_gets_its_own_receipt_and_never_dispatches_twice(bridge):
    calls = []
    bridge._do = lambda *args: calls.append(args) or {'exit_code': 0}
    args = {'argv': ['test-fixture'], 'idempotency_key': 'diagnostic-unit-fixture'}
    first = bridge.execute('exec_command', args)
    second = bridge.execute('exec_command', args)
    assert second['idempotent_replay'] and len(calls) == 1
    assert second['operation_id'] != first['operation_id']
    assert second['diagnostics']['replayed_operation_id'] == first['operation_id']
    assert len([r for r in rows(bridge) if r['event'] == 'tool_finish']) == 2


@pytest.mark.parametrize('result', [
    {'state': 'exited', 'exit_code': 7, 'error': None},
    {'state': 'lost', 'exit_code': None,
     'error': {'code': 'execution_state_unknown', 'message': 'Synthetic disconnect'}},
])
def test_polling_failed_session_is_not_a_success(bridge, result):
    bridge._do = lambda *args: result
    out = bridge.execute('session_read', {'session_id': 'test-session'})
    assert out['ok'] is False
    assert out['error']


def test_running_session_is_pending_not_completed(bridge):
    bridge._do = lambda *args: {'state': 'running', 'exit_code': None, 'error': None}
    out = bridge.execute('session_read', {'session_id': 'test-session'})
    assert out['ok']
    assert out['diagnostics']['execution_status'] == 'running'


def rpc_fixture(tmp_path):
    rpc = AppServer.__new__(AppServer)
    rpc.closed = False
    rpc.broken = False
    rpc.generation = 'fixture-generation'
    rpc.lock = threading.RLock()
    rpc.write_lock = threading.Lock()
    rpc.next_id = 0
    rpc.pending = {}
    rpc.proc = SimpleNamespace(stdout=io.BytesIO(), poll=lambda: None)
    rpc.audit = Audit(tmp_path / 'rpc-audit.jsonl')
    rpc.schema = SimpleNamespace(validate=lambda *a: None,
                                 validate_response=lambda *a: None)
    rpc.callback = lambda *a: None
    rpc._write = lambda *a: None
    return rpc


def test_rpc_error_is_correlated_across_reader_thread(tmp_path):
    rpc = rpc_fixture(tmp_path)
    token = CURRENT_OPERATION.set('unit-test-operation')
    try:
        f = rpc.begin('command/exec', {})
    finally:
        CURRENT_OPERATION.reset(token)
    rpc.proc.stdout = io.BytesIO((json.dumps({
        'id': 1, 'error': {'code': -32602, 'message': 'SYNTHETIC_SECRET_NOT_FOR_LOG'}
    }) + '\n').encode())
    thread = threading.Thread(target=rpc._read)
    thread.start()
    thread.join(2)
    assert not thread.is_alive()
    with pytest.raises(BridgeError) as failure:
        f.result()
    events = [json.loads(x) for x in rpc.audit.path.read_text('utf-8').splitlines()]
    error = next(e for e in events if e['event'] == 'rpc_error')
    assert error['operation_id'] == 'unit-test-operation'
    assert error['request_id'] == 1
    assert failure.value.details['origin'] == 'codex_app_server'
    assert failure.value.details['operation_id'] == 'unit-test-operation'
    assert 'SYNTHETIC_SECRET_NOT_FOR_LOG' not in json.dumps(events)


def test_mcp_ingress_and_bridge_receipt_share_one_id(bridge):
    server = make_server(bridge)
    async def run():
        async with create_client_server_memory_streams() as (cs, ss):
            async with anyio.create_task_group() as group:
                group.start_soon(server.run, *ss, server.create_initialization_options())
                async with ClientSession(*cs) as client:
                    await client.initialize()
                    response = await client.call_tool('exec_command', {'argv': ['fixture']})
                    out = response.structuredContent
                group.cancel_scope.cancel()
                return out
    out = asyncio.run(run())
    ingress = [r for r in rows(bridge) if r['event'] == 'mcp_received']
    assert len(ingress) == 1
    assert ingress[0]['operation_id'] == out['operation_id']
    assert out['diagnostics']['mcp_received'] is True


def test_auto_session_error_preserves_backend_details():
    b = Bridge.__new__(Bridge)
    b.cfg = SimpleNamespace(output_limit_bytes=1024)
    error = {'code': 'backend_error', 'message': 'Synthetic protocol error',
             'retryable': False, 'details': {'origin': 'codex_app_server', 'rpc_code': -1}}
    s = SimpleNamespace(id='fixture-session', finished=threading.Event(),
                        read=lambda **kw: {'error': error})
    s.finished.set()
    b.sessions = SimpleNamespace(get=lambda *a: s)
    b._session_start = lambda a: {'session_id': s.id}
    with pytest.raises(BridgeError) as caught:
        b._command_auto({'argv': ['fixture']})
    assert caught.value.details['origin'] == 'codex_app_server'
    assert caught.value.details['session_id'] == s.id


def health_fixture():
    b = Bridge.__new__(Bridge)
    b.ensure_ready = lambda **kw: None
    b.cfg = SimpleNamespace(browser_use_enabled=False, computer_use_enabled=False,
                            application_access_policy='owner_preapproved',
                            requires_client_elicitation=False)
    b.browser = b.gui = None
    b.runtime = SimpleNamespace(as_dict=lambda: {})
    b.rpc = SimpleNamespace(proc=SimpleNamespace(pid=1), generation='generation-current')
    b.audit = SimpleNamespace(methods={})
    b.proxy = {}
    b.schema_info = {}
    b.pending_update = None
    b.verified = {'command/exec': {'at': '2026-09-24T00:00:00+00:00',
                                  'result': 'PASS', 'generation': 'generation-current'}}
    return b


def test_passive_health_explicitly_labels_cached_evidence():
    out = health_fixture().health(active=False)
    assert out['checks']['shell'] == 'PASS'
    assert out['health_observation']['mode'] == 'passive'
    ev = out['health_observation']['checks']['shell']
    assert ev['source'] == 'cached_rpc_success'
    assert ev['observed_at'] == '2026-09-24T00:00:00+00:00'
    assert ev['runtime_generation'] == 'generation-current'
    assert out['health_observation']['guarantees_future_call_authorization'] is False


def test_passive_health_does_not_reuse_previous_runtime_generation():
    b = health_fixture()
    b.verified['command/exec']['generation'] = 'generation-old'
    assert b.health(active=False)['checks']['shell'] == 'NOT_TESTED'
