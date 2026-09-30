import base64
import concurrent.futures
import json
import sqlite3
import threading

import pytest

from codex_control_mcp.common import digest
from codex_control_mcp.errors import BridgeError
from codex_control_mcp.idempotency import Idempotency
from codex_control_mcp.sessions import SessionStore


def delta(session, raw, stream='stdout'):
    session.append({'deltaBase64': base64.b64encode(raw).decode(), 'stream': stream})


def test_receipt_disk_eviction_independent_unicode(tmp_path):
    path = tmp_path / 'calls.db'
    store = Idempotency(path)
    result = {'operation_id': 'first', 'result': {'中文': ['😀', {'a': [1, None]}]}}
    for i in range(258):
        assert store.reserve(str(i), 'tool', {}) is None
        store.finish(str(i), result)
    assert len(store.cache) == 256
    replay = store.reserve('0', 'tool', {})
    replay['result']['中文'][1]['a'].append(2)
    assert store.reserve('0', 'tool', {}) == result
    store.close()
    reopened = Idempotency(path)
    assert reopened.reserve('0', 'tool', {}) == result
    reopened.close()


def test_reserve_two_connections_at_most_once(tmp_path):
    path = tmp_path / 'calls.db'
    stores = [Idempotency(path), Idempotency(path)]
    barrier = threading.Barrier(2)
    executed = []
    def call(store):
        barrier.wait()
        try:
            old = store.reserve('same', 'tool', {})
            if old is None:
                executed.append(1)
                store.finish('same', {'ok': True})
        except BridgeError as exc:
            assert exc.code == 'execution_state_unknown'
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        list(pool.map(call, stores))
    assert len(executed) == 1
    for store in stores:
        assert store.reserve('same', 'tool', {}) == {'ok': True}
        store.close()


@pytest.mark.parametrize('state,receipt', [('started', None), ('complete', None), ('complete', '{broken'), ('complete', '{"version":99,"result":{}}'), ('complete', '[]')])
def test_legacy_corrupt_unknown_never_reexecutes(tmp_path, state, receipt):
    path = tmp_path / 'calls.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE calls(key_hash TEXT PRIMARY KEY,args_hash TEXT NOT NULL,state TEXT NOT NULL,created_at REAL NOT NULL)')
        db.execute('INSERT INTO calls VALUES(?,?,?,0)', (digest('key'), digest({'tool': 'tool', 'args': {}}), state))
    store = Idempotency(path)
    with store.db:
        store.db.execute('UPDATE calls SET receipt=?', (receipt,))
    with pytest.raises(BridgeError) as exc:
        store.reserve('key', 'tool', {})
    assert exc.value.code == 'execution_state_unknown'
    with pytest.raises(BridgeError) as exc:
        store.reserve('key', 'tool', {'changed': True})
    assert exc.value.code == 'idempotency_conflict'
    store.close()


def test_finish_failure_rolls_back_state_and_receipt(tmp_path):
    store = Idempotency(tmp_path / 'calls.db')
    store.reserve('key', 'tool', {})
    store.db.execute("CREATE TRIGGER fail_finish BEFORE UPDATE ON calls BEGIN SELECT RAISE(ABORT, 'fixture failure'); END")
    store.db.commit()
    with pytest.raises(sqlite3.DatabaseError):
        store.finish('key', {'ok': True})
    assert store.db.execute('SELECT state,receipt FROM calls').fetchone() == ('started', None)
    store.close()
    reopened = Idempotency(tmp_path / 'calls.db')
    with pytest.raises(BridgeError):
        reopened.reserve('key', 'tool', {})
    reopened.close()


def test_sessions_restart_completed_lost_tail_and_streams(tmp_path):
    path = tmp_path / 'sessions.json'
    store = SessionStore(path, 2048, 4)
    done = store.create('generation', '.')
    raw = '中文😀'.encode()
    for chunk in (raw[:1], raw[1:4], raw[4:]):
        delta(done, chunk)
    delta(done, b'error', 'stderr')
    delta(done, b'\xe4', 'stderr')
    future = concurrent.futures.Future()
    future.set_result({'exitCode': 7})
    done.finish(future)
    lost = store.create('generation', '.')
    delta(lost, b'persisted\xe4')
    # No save() or exit hook: reopen the continuously committed snapshots.
    reopened = SessionStore(path, 2048, 4)
    read = reopened.get(done.id).read()
    assert read['stdout'] == '中文😀' and read['stderr'] == 'error�'
    assert read['exit_code'] == 7 and read['state'] == 'exited'
    read = reopened.get(lost.id).read()
    assert read['stdout'] == 'persisted�'
    assert read['state'] == 'lost' and read['exit_code'] is None and not read['reconnectable']
    with pytest.raises(BridgeError):
        reopened.get(lost.id, 'generation')
    reopened.close()
    store.close()


def test_session_ring_persisted_cursors(tmp_path):
    path = tmp_path / 'sessions.json'
    store = SessionStore(path, 1024, 2)
    session = store.create('g', '.')
    delta(session, b'a' * 4096)
    previous = session.read()
    reopened = SessionStore(path, 1024, 2)
    read = reopened.get(session.id).read()
    assert read['oldest_cursor'] == previous['oldest_cursor'] == 3
    assert read['cursor_gap'] and read['dropped_output_bytes'] == 3072
    assert read['next_cursor'] == 4 and read['stdout'] == 'a' * 1024
    reopened.close()
    store.close()


def test_legacy_import_once_preserves_file_and_unavailable(tmp_path):
    path = tmp_path / 'sessions.json'
    raw = json.dumps([{'session_id': 'old', 'runtime_generation': 'g', 'cwd': '.', 'state': 'running', 'exit_code': 0, 'next_cursor': 20}])
    path.write_text(raw)
    store = SessionStore(path, 2048, 2)
    read = store.get('old').read()
    assert read['output_availability'] == 'unavailable' and read['oldest_cursor'] == 20
    assert read['exit_code'] is None and read['state'] == 'lost'
    store.close()
    assert path.read_text() == raw
    path.write_text('[]')
    reopened = SessionStore(path, 2048, 2)
    assert len(reopened.list()) == 1
    reopened.close()


def test_session_persistence_fault_preserves_live_result(tmp_path):
    store = SessionStore(tmp_path / 'sessions.json', 2048, 2)
    session = store.create('g', '.')
    delta(session, b'last durable')
    store.db.execute("CREATE TRIGGER fail_save BEFORE INSERT ON sessions BEGIN SELECT RAISE(ABORT, 'fixture failure'); END")
    store.db.commit()
    delta(session, b' live tail')
    future = concurrent.futures.Future()
    future.set_result({'exitCode': 0})
    session.finish(future)
    assert session.read()['stdout'] == 'last durable live tail' and session.exit_code == 0
    assert session.persistence_error
    reopened = SessionStore.__new__(SessionStore)
    with sqlite3.connect(store.path.with_suffix('.sqlite3')) as db:
        snapshot = json.loads(db.execute('SELECT doc FROM sessions').fetchone()[0])
        events = [json.loads(row[0]) for row in db.execute('SELECT event FROM session_events ORDER BY cursor')]
    assert snapshot['metadata']['state'] == 'running'
    assert snapshot['metadata']['exit_code'] is None
    assert events[0]['text'] == 'last durable'
    store.db.execute('DROP TRIGGER fail_save')
    store.db.commit()
    store.close()

from codex_control_mcp.task_runtime import RecoverableTaskStore
from codex_control_mcp.bridge import Bridge
from codex_control_mcp.common import Audit
from types import SimpleNamespace


def task_fixture(path):
    store = RecoverableTaskStore(path)
    created = store.create({'title': 'task', 'goal': 'verify', 'steps': [{'id': 's', 'title': 'step'}]})
    return store, created['task_id']


def test_task_database_cas_two_connections(tmp_path):
    a, tid = task_fixture(tmp_path / 'tasks.db')
    b = RecoverableTaskStore(a.path)
    barrier = threading.Barrier(2)
    for store in (a, b):
        load = store._load
        def paused(tid, load=load):
            doc = load(tid)
            barrier.wait(timeout=5)
            return doc
        store._load = paused
    def mutate(store):
        try:
            store.checkpoint({'task_id': tid, 'summary': 'done', 'expected_revision': 1})
            return 'ok'
        except BridgeError as exc:
            return exc.code
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(mutate, (a, b))) == ['ok', 'task_revision_conflict']
    a.close()
    b.close()


def test_task_checkpoint_invalidates_review_and_legacy_resume(tmp_path):
    store, tid = task_fixture(tmp_path / 'tasks.db')
    store.checkpoint({'task_id': tid, 'step_id': 's', 'step_status': 'completed'})
    store.final_review({'task_id': tid, 'review_status': 'pass', 'summary': 'verified'})
    store.checkpoint({'task_id': tid, 'summary': 'changed'})
    with pytest.raises(BridgeError) as exc:
        store.complete({'task_id': tid})
    assert exc.value.code == 'task_review_required'
    store.block({'task_id': tid, 'summary': 'old task blocker'})
    store.resume({'task_id': tid})
    store.final_review({'task_id': tid, 'review_status': 'pass', 'summary': 'verified again'})
    store.complete({'task_id': tid})
    with pytest.raises(BridgeError) as exc:
        store.resume({'task_id': tid})
    assert exc.value.code == 'task_completed'
    store.close()


def fake_bridge(tmp_path):
    b = Bridge.__new__(Bridge)
    b.audit = Audit(tmp_path / 'audit.jsonl')
    b.idempotency = Idempotency(tmp_path / 'calls.db')
    b.tasks, tid = task_fixture(tmp_path / 'tasks.db')
    b.cfg = SimpleNamespace(browser={}, oauth={}, browser_use_enabled=False, computer_use_enabled=False)
    b.runtime = b.schema = b.rpc = None
    b.proxy = {}
    b.write_lock = threading.RLock()
    return b, tid


def test_task_association_failure_no_dispatch_and_recover_readonly(tmp_path):
    b, tid = fake_bridge(tmp_path)
    calls = []
    b._do = lambda *a: calls.append(a) or {'exit_code': 0}
    b.tasks.db.execute("CREATE TRIGGER fail_association BEFORE INSERT ON executions BEGIN SELECT RAISE(ABORT, 'fixture failure'); END")
    b.tasks.db.commit()
    result = b.execute('exec_command', {'argv': ['fixture'], 'task_id': tid, 'step_id': 's'})
    assert not result['ok'] and calls == []
    b.tasks.db.execute('DROP TRIGGER fail_association')
    b.tasks.db.commit()
    result = b.execute('exec_command', {'argv': ['fixture'], 'task_id': tid, 'step_id': 's', 'idempotency_key': 'one'})
    assert result['ok'] and len(calls) == 1
    record = b.tasks.recover({'task_id': tid})['executions'][0]
    assert record['operation_id'] == result['operation_id'] and record['step_id'] == 's'
    assert 'fixture' not in json.dumps(record)  # command is not a recovery script
    b.execute('exec_command', {'argv': ['fixture'], 'task_id': tid, 'step_id': 's', 'idempotency_key': 'one'})
    assert len(calls) == 1
    b.tasks.recover({'task_id': tid})
    assert len(calls) == 1
    b.tasks.close()
    b.idempotency.close()


def test_task_lost_session_recovery_requires_verification(tmp_path):
    store, tid = task_fixture(tmp_path / 'tasks.db')
    store.begin_execution(tid, 's', 'operation', 'exec_command')
    store.link_execution('operation', session_id='sid', runtime_generation='g')
    store.session_reader = lambda sid: {'state': 'lost', 'exit_code': None, 'next_cursor': 3, 'stdout': 'stored output'}
    recovered = store.recover({'task_id': tid})
    assert recovered['unknown_terminal_operations'] == ['operation']
    assert recovered['executions'][0]['session_output']['stdout'] == 'stored output'
    with pytest.raises(BridgeError):
        store.resume({'task_id': tid})
    store.resolve_execution({'task_id': tid, 'execution_operation_id': 'operation', 'execution_resolution': 'verified_completed', 'summary': 'external effects checked', 'evidence': ['fixture effect exists']})
    store.resume({'task_id': tid})
    store.close()


def test_late_idempotency_failure_keeps_true_response(tmp_path):
    b, tid = fake_bridge(tmp_path)
    calls = []
    b._do = lambda *a: calls.append(a) or {'exit_code': 0, 'stdout': 'real result'}
    b.idempotency.db.execute("CREATE TRIGGER fail_finish BEFORE UPDATE ON calls BEGIN SELECT RAISE(ABORT, 'fixture failure'); END")
    b.idempotency.db.commit()
    args = {'argv': ['fixture'], 'idempotency_key': 'once'}
    out = b.execute('exec_command', args)
    assert out['ok'] and out['result']['stdout'] == 'real result'
    assert out['diagnostics']['idempotency_persistence']['operation_result_preserved']
    assert b.execute('exec_command', args)['error']['code'] == 'execution_state_unknown'
    assert len(calls) == 1
    b.idempotency.close()
    b.tasks.close()


def test_bridge_disk_replay_preserves_origin_after_reopen(tmp_path):
    b, _ = fake_bridge(tmp_path)
    calls = []
    b._do = lambda *a: calls.append(a) or {'exit_code': 0, 'stdout': '中文结果'}
    args = {'argv': ['fixture'], 'idempotency_key': 'disk-once'}
    first = b.execute('exec_command', args)
    b.idempotency.close()
    b.idempotency = Idempotency(tmp_path / 'calls.db')
    replay = b.execute('exec_command', args)
    assert len(calls) == 1 and replay['idempotent_replay']
    assert replay['result'] == first['result']
    assert replay['diagnostics']['replayed_operation_id'] == first['operation_id']
    assert replay['operation_id'] != first['operation_id']
    b.tasks.close()
    b.idempotency.close()


def test_legacy_task_database_additive_migration(tmp_path):
    path = tmp_path / 'tasks.db'
    store, tid = task_fixture(path)
    store.db.execute('DROP TABLE executions')
    store.db.commit()
    store.close()
    reopened = RecoverableTaskStore(path)
    reopened.resume({'task_id': tid})
    assert reopened.recover({'task_id': tid})['executions'] == []
    reopened.close()


def test_final_output_event_transaction_failure_keeps_previous_state(tmp_path):
    store = SessionStore(tmp_path / 'sessions.json', 2048, 2)
    session = store.create('g', '.')
    delta(session, b'\xe4')
    store.db.execute("CREATE TRIGGER fail_tail BEFORE INSERT ON session_events WHEN NEW.cursor=1 BEGIN SELECT RAISE(ABORT, 'tail failure'); END")
    store.db.commit()
    future = concurrent.futures.Future()
    future.set_result({'exitCode': 0})
    session.finish(future)
    assert session.state == 'exited' and session.read()['stdout'] == '�'
    snapshot = json.loads(store.db.execute('SELECT doc FROM sessions').fetchone()[0])
    assert snapshot['metadata']['state'] == 'running' and snapshot['metadata']['exit_code'] is None
    assert store.db.execute('SELECT COUNT(*) FROM session_events').fetchone()[0] == 1
    store.db.execute('DROP TRIGGER fail_tail')
    store.db.commit()
    store.close()


def test_runtime_session_association_update_failure_no_backend_begin(tmp_path):
    from codex_control_mcp.common import CURRENT_TASK_EXECUTION
    b, tid = fake_bridge(tmp_path)
    b.tasks.begin_execution(tid, 's', 'operation', 'exec_command')
    b.tasks.db.execute("CREATE TRIGGER fail_link BEFORE UPDATE ON executions BEGIN SELECT RAISE(ABORT, 'fixture link failure'); END")
    b.tasks.db.commit()
    b.sessions = SessionStore(tmp_path / 'sessions.json', 2048, 2)
    b.cfg.cwd = str(tmp_path)
    b.ready_lock = threading.RLock()
    b.ensure_ready = lambda: None
    calls = []
    b.rpc = SimpleNamespace(generation='g', begin=lambda *a: calls.append(a))
    token = CURRENT_TASK_EXECUTION.set('operation')
    try:
        with pytest.raises(sqlite3.DatabaseError):
            b._session_start({'argv': ['fixture']})
        assert calls == []
        with pytest.raises(sqlite3.DatabaseError):
            b._rpc('command/exec', {})
        assert calls == []
    finally:
        CURRENT_TASK_EXECUTION.reset(token)
        b.sessions.close()
        b.tasks.close()
        b.idempotency.close()


def test_terminal_execution_survives_bounded_session_history_eviction(tmp_path):
    store, tid = task_fixture(tmp_path / 'tasks.db')
    store.begin_execution(tid, 's', 'operation', 'exec_command')
    store.link_execution('operation', session_id='evicted', state='completed')
    def unavailable(sid):
        raise BridgeError('session_lost', 'History evicted')
    store.session_reader = unavailable
    recovered = store.recover({'task_id': tid})
    assert recovered['unknown_terminal_operations'] == []
    assert recovered['executions'][0]['session_output_availability'] == 'unavailable'
    store.resume({'task_id': tid})
    store.close()
