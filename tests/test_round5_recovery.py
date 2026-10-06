"""Failure recovery, connection pinning and lossless session continuation."""
import base64
import concurrent.futures
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from codex_control_mcp.dynamic_mcp import DynamicMCPManager
from codex_control_mcp.errors import BridgeError
from codex_control_mcp.host_runtime import HostManager
from codex_control_mcp.idempotency import Idempotency
from codex_control_mcp.sessions import Session, SessionStore
from codex_control_mcp.task_runtime import RecoverableTaskStore
from codex_control_mcp.results import promote_execution
from codex_control_mcp.dynamic_mcp import validation_argv
from codex_control_mcp.tools import validate_tool
from codex_control_mcp.bridge import Bridge
from codex_control_mcp.config import Config


class CommitFault:
    """Fail one commit on a real SQLite transaction, then permit recovery."""
    def __init__(self, connection):
        self.connection = connection
        self.fail = True
    def __getattr__(self, name):
        return getattr(self.connection, name)
    def commit(self):
        if self.fail:
            self.fail = False
            raise OSError('synthetic commit fault')
        return self.connection.commit()


def test_session_creation_save_failure_does_not_consume_slot(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / 'sessions.json', 4096, 1)
    original = store.save
    monkeypatch.setattr(store, 'save', lambda: (_ for _ in ()).throw(OSError('synthetic')))
    with pytest.raises(OSError):
        store.create('g', '.')
    assert store.list() == []
    monkeypatch.setattr(store, 'save', original)
    assert store.create('g', '.').state == 'starting'


@pytest.mark.parametrize('mode', ['text', 'raw', 'chunks', 'legacy'])
def test_session_next_action_preserves_page_and_format(mode):
    session = Session('fixture', 'g', '.', 8192)
    raw = b'x' * 3000 + b'\x80'
    session.append({'deltaBase64': base64.b64encode(raw).decode()})
    future = concurrent.futures.Future(); future.set_result({'exitCode': 0})
    session.finish(future)
    first = session.read(max_bytes=1024, output_format=mode)
    action = first['next_action']['arguments']
    assert action['max_bytes'] == 1024 and action['output_format'] == mode
    action.pop('session_id')
    second = session.read(**action)
    assert second['output_format'] == mode and second['page_bytes'] <= 1024


@pytest.mark.parametrize('action', ['create', 'checkpoint', 'complete'])
def test_task_commit_failure_rolls_back_before_next_request(tmp_path, action):
    store = RecoverableTaskStore(tmp_path / 'tasks.db')
    args = {'title': 'fixture', 'goal': 'fixture', 'steps': [{'id': 'work', 'title': 'work'}]}
    created = store.manage({'action': 'create', **args})
    tid = created['task_id']
    if action == 'complete':
        store.manage({'action': 'checkpoint', 'task_id': tid, 'completed_step_ids': ['work']})
        store.manage({'action': 'final_review', 'task_id': tid, 'review_status': 'pass',
                      'summary': 'verified', 'verified': ['fixture verified']})
    before = store.manage({'action': 'get', 'task_id': tid})['task']
    store.db = CommitFault(store.db)
    try:
        with pytest.raises(OSError):
            store.manage({'action': 'create', **args} if action == 'create' else
                         {'action': 'checkpoint', 'task_id': tid, 'summary': 'must roll back'} if action == 'checkpoint' else
                         {'action': 'complete', 'task_id': tid})
        assert not store.db.in_transaction
        assert store.manage({'action': 'get', 'task_id': tid})['task'] == before
        store.manage({'action': 'create', **args})
        assert store.manage({'action': 'list'})['count'] == 2
    finally:
        store.close()


@pytest.mark.parametrize('action', ['reserve', 'finish'])
def test_idempotency_commit_failure_keeps_durable_state(tmp_path, action):
    store = Idempotency(tmp_path / 'calls.db')
    if action == 'finish':
        assert store.reserve('fixture', 'echo', {}) is None
    store.db = CommitFault(store.db)
    try:
        with pytest.raises(OSError):
            store.reserve('fixture', 'echo', {}) if action == 'reserve' else store.finish('fixture', {'ok': True})
        assert not store.db.in_transaction
        if action == 'reserve':
            assert store.reserve('fixture', 'echo', {}) is None
        else:
            assert store.db.execute('SELECT state FROM calls').fetchone()[0] == 'started'
            with pytest.raises(BridgeError) as error:
                store.reserve('fixture', 'echo', {})
            assert error.value.code == 'execution_state_unknown'
            store.finish('fixture', {'ok': True})
            assert store.reserve('fixture', 'echo', {})['ok']
    finally:
        store.close()


def dynamic(tmp_path):
    manager = DynamicMCPManager(SimpleNamespace(home=tmp_path))
    manager.manage({'action': 'register', 'name': 'node', 'transport': 'stdio', 'command': 'python'})
    manager.servers['node'].update(status='ready', tools=[
        {'name': name, 'inputSchema': {'type': 'object'}} for name in ['echo', 'session_read']])
    return manager


@pytest.mark.parametrize('change', ['update', 'disable', 'remove', 'schema'])
def test_dynamic_change_during_validation_rejected_before_dispatch(tmp_path, monkeypatch, change):
    manager = dynamic(tmp_path)
    def validation(*args, **kwargs):
        if change == 'schema':
            manager.servers['node']['tools'] = [{'name': 'echo', 'inputSchema': {'type': 'object'}}]
        else:
            manager.manage({'action': change, 'name': 'node', **({'command': 'replacement'} if change == 'update' else {})})
        return SimpleNamespace(returncode=0, stdout=b'{"valid":true}')
    monkeypatch.setattr('codex_control_mcp.dynamic_mcp.subprocess.run', validation)
    monkeypatch.setattr(manager, '_run', lambda *args, **kwargs: pytest.fail('stale target dispatched'))
    with pytest.raises(BridgeError) as info:
        manager.call({'name': 'node:echo', 'arguments': {}})
    assert info.value.code == 'mcp_registry_changed'


@pytest.mark.parametrize('change', ['update', 'disable', 'remove'])
def test_dynamic_change_after_dispatch_preserves_result_without_wrong_continuation(tmp_path, monkeypatch, change):
    manager = dynamic(tmp_path)
    monkeypatch.setattr('codex_control_mcp.dynamic_mcp.subprocess.run',
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout=b'{"valid":true}'))
    calls = []
    def invoke(item, operation, **kwargs):
        calls.append(item['command'])
        manager.manage({'action': change, 'name': 'node', **({'command': 'replacement'} if change == 'update' else {})})
        return {'structuredContent': {'ok': True, 'result': {'state': 'running', 'session_id': 'remote-session',
            'next_action': {'tool': 'session_read', 'arguments': {'session_id': 'remote-session'}}}}}
    monkeypatch.setattr(manager, '_run', invoke)
    out = manager.call({'name': 'node:echo', 'arguments': {}})
    assert calls == ['python'] and out['result']['structuredContent']['ok']
    assert out['next_action'] is None and out['continuation_unavailable']['code'] == 'mcp_registry_changed'
    host = HostManager(SimpleNamespace(cfg=SimpleNamespace(home=tmp_path)), manager)
    host.manage({'action': 'register', 'name': 'remote', 'transport': 'mcp', 'mcp_server': 'node'})
    monkeypatch.setattr(manager, 'call', lambda _: out)
    routed = host.exec({'host': 'remote', 'command': 'fixture'})
    assert routed['next_action'] is None and routed['continuation_unavailable'] == out['continuation_unavailable']
    assert promote_execution(out)['next_action'] is None


def test_dynamic_unknown_continuation_never_promoted_as_local(tmp_path, monkeypatch):
    manager = dynamic(tmp_path)
    monkeypatch.setattr('codex_control_mcp.dynamic_mcp.subprocess.run',
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout=b'{"valid":true}'))
    monkeypatch.setattr(manager, '_run', lambda *a, **k: {'structuredContent': {'ok': True, 'result': {
        'next_action': {'tool': 'unknown', 'arguments': {'session_id': 'remote-session'}}}}})
    out = manager.call({'name': 'node:echo', 'arguments': {}})
    assert promote_execution(out)['next_action'] is None
    assert out['continuation_unavailable']['code'] == 'mcp_tool_not_found'


def test_dynamic_accepted_connection_is_immutable_during_execution(tmp_path, monkeypatch):
    manager = dynamic(tmp_path)
    monkeypatch.setattr('codex_control_mcp.dynamic_mcp.subprocess.run',
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout=b'{"valid":true}'))
    def invoke(item, operation, **kwargs):
        manager.servers['node']['args'].append('later-change')
        assert item['args'] == []
        return {'structuredContent': {'ok': True}}
    monkeypatch.setattr(manager, '_run', invoke)
    assert manager.call({'name': 'node:echo', 'arguments': {}})['result']['structuredContent']['ok']


def test_source_schema_worker_works_without_pythonpath_or_project_cwd(tmp_path):
    result = subprocess.run(validation_argv(), input=b'[{"type":"object"},{}]',
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=tmp_path,
                            env={**os.environ, 'PYTHONPATH': str(tmp_path / 'missing')}, timeout=10)
    assert result.returncode == 0 and json.loads(result.stdout)['valid']


@pytest.mark.parametrize('change', ['update', 'remove_register', 'disable_enable'])
def test_returned_continuation_refuses_replaced_connection(tmp_path, monkeypatch, change):
    manager = dynamic(tmp_path)
    monkeypatch.setattr('codex_control_mcp.dynamic_mcp.subprocess.run',
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout=b'{"valid":true}'))
    count = []
    def invoke(*args, **kwargs):
        count.append(1)
        return {'structuredContent': {'result': {
            'next_action': {'tool': 'session_read', 'arguments': {'session_id': 'owned'}}}}}
    monkeypatch.setattr(manager, '_run', invoke)
    followup = manager.call({'name': 'node:echo', 'arguments': {}})['next_action']['arguments']
    validate_tool('mcp_tool_call', followup)
    if change == 'remove_register':
        manager.manage({'action': 'remove', 'name': 'node'})
        manager.manage({'action': 'register', 'name': 'node', 'transport': 'stdio', 'command': 'python'})
    elif change == 'disable_enable':
        manager.manage({'action': 'disable', 'name': 'node'})
        manager.manage({'action': 'enable', 'name': 'node'})
    else:
        manager.manage({'action': 'update', 'name': 'node', 'description': 'same endpoint, new registration'})
    with pytest.raises(BridgeError) as info:
        manager.call(followup)
    assert info.value.code == 'mcp_registry_changed' and count == [1]


def test_connection_digest_survives_registry_reload(tmp_path, monkeypatch):
    manager = dynamic(tmp_path)
    manager._save()
    reloaded = DynamicMCPManager(SimpleNamespace(home=tmp_path))
    assert reloaded._connection_digest(reloaded.servers['node']) == manager._connection_digest(manager.servers['node'])


@pytest.mark.parametrize('tool,args', [
    ('task_manage', {'action': 'list'}), ('task_manage', {'action': 'get', 'task_id': 'fixture'}),
    ('host_manage', {'action': 'status', 'name': 'local'}),
    ('skill_package', {'action': 'list'}), ('skill_package', {'action': 'inspect', 'skill': 'fixture'}),
])
def test_readonly_management_queries_never_replay_stale_snapshot(tmp_path, tool, args):
    bridge = Bridge(Config(home=tmp_path / 'home', cwd=str(tmp_path)))
    calls = []
    bridge._do = lambda *a: calls.append(1) or {'observed_revision': len(calls)}
    try:
        request = {**args, 'idempotency_key': 'repeated-read'}
        first = bridge.execute(tool, request); second = bridge.execute(tool, request)
        assert first['ok'] and second['ok'] and second['result']['observed_revision'] == 2
        assert second['risk_level'] == 'read' and not second.get('idempotent_replay')
    finally:
        bridge.close()


@pytest.mark.parametrize('tool,args', [
    ('task_manage', {'action': 'get', 'task_id': 'fixture'}),
    ('host_manage', {'action': 'get', 'name': 'local'}),
    ('mcp_manage', {'action': 'list'}),
])
def test_independently_locked_registry_reads_do_not_wait_on_file_mutations(tmp_path, tool, args):
    bridge = Bridge(Config(home=tmp_path / 'home', cwd=str(tmp_path)))
    class HeldFileLock:
        def __enter__(self):
            pytest.fail('readonly registry request took unrelated file lock')
        def __exit__(self, *a):
            pass
    bridge.write_lock = HeldFileLock()
    bridge._do = lambda *a: {'observed': True}
    try:
        assert bridge.execute(tool, args)['ok']
    finally:
        bridge.close()


@pytest.mark.parametrize('action', [{'tool': []}, {'tool': 'session_read', 'arguments': []}])
def test_malformed_remote_continuation_preserves_executed_result(tmp_path, monkeypatch, action):
    manager = dynamic(tmp_path)
    monkeypatch.setattr('codex_control_mcp.dynamic_mcp.subprocess.run',
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout=b'{"valid":true}'))
    monkeypatch.setattr(manager, '_run', lambda *a, **k: {'structuredContent': {'ok': True,
                        'result': {'exit_code': 0, 'next_action': action}}})
    out = manager.call({'name': 'node:echo', 'arguments': {}})
    assert out['result']['structuredContent']['ok'] and promote_execution(out)['next_action'] is None
    assert out['continuation_unavailable']['code'] == 'mcp_continuation_invalid'
