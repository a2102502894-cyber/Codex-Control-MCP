"""Controlled adapter contracts; these fixtures are not an official Runtime.

No child command, PTY, proxy request, or model is executed. Real acceptance
remains in the five explicitly marked integration cases.
"""
import base64
from types import SimpleNamespace

import pytest

from codex_control_mcp.bridge import Bridge
from codex_control_mcp.config import Config
from codex_control_mcp.errors import BridgeError


@pytest.fixture
def bridge(tmp_path):
    b = Bridge(Config(home=tmp_path / 'home', cwd=str(tmp_path)))
    yield b
    b.close()


def test_refresh_defers_live_fixture_without_replacing_connection(bridge, monkeypatch):
    b = bridge
    closed = []
    b.runtime = SimpleNamespace(file_fingerprint='old')
    b.rpc = SimpleNamespace(alive=True, pending={}, generation='fixture-generation',
                            close=lambda: closed.append(True))
    b.sessions.create(b.rpc.generation, b.cfg.cwd)
    before = b.rpc
    monkeypatch.setattr('codex_control_mcp.bridge.Discovery.find',
                        lambda self: SimpleNamespace(file_fingerprint='new', cli_version='fixture-next'))
    with pytest.raises(BridgeError) as caught:
        b.ensure_ready(force=True)
    assert caught.value.code == 'update_pending'
    assert b.rpc is before and b.rpc.generation == 'fixture-generation'
    assert not closed and b.pending_update['reason'] == 'active_operations_or_sessions'


@pytest.mark.parametrize('tool,args,method', [
    ('session_write', {'text': '中文\r\n'}, 'command/exec/write'),
    ('session_resize', {'rows': 36, 'cols': 100}, 'command/exec/resize'),
    ('session_kill', {}, 'command/exec/terminate'),
])
@pytest.mark.parametrize('same_generation', [True, False])
def test_controls_require_owned_generation_and_forward_exact_payload(bridge, monkeypatch, tool, args, method, same_generation):
    b = bridge
    b.rpc = SimpleNamespace(generation='current', close=lambda: None)
    s = b.sessions.create('current' if same_generation else 'previous', b.cfg.cwd, tty=True)
    monkeypatch.setattr(b, 'ensure_ready', lambda: None)
    calls = []
    monkeypatch.setattr(b, '_rpc', lambda method, params: calls.append((method, params)))
    if not same_generation:
        with pytest.raises(BridgeError) as caught:
            b._do(tool, {'session_id': s.id, **args})
        assert caught.value.code == 'session_lost' and calls == []
        return
    out = b._do(tool, {'session_id': s.id, **args})
    assert out['control_request_acknowledged'] and len(calls) == 1
    assert calls[0][0] == method and calls[0][1]['processId'] == s.id
    if tool == 'session_write':
        assert base64.b64decode(calls[0][1]['deltaBase64']).decode() == args['text']
        assert calls[0][1]['closeStdin'] is False
    elif tool == 'session_resize':
        assert calls[0][1]['size'] == {'rows': 36, 'cols': 100}


def test_proxy_override_is_serialized_once_without_local_execution(bridge, monkeypatch):
    b = bridge
    monkeypatch.setattr(b, 'ensure_ready', lambda: None)
    calls = []
    def rpc(method, params, **kwargs):
        calls.append((method, params))
        return {'exitCode': 0, 'stdout': 'synthetic output', 'stderr': ''}
    monkeypatch.setattr(b, '_rpc', rpc)
    overrides = {'HTTP_PROXY': 'http://127.0.0.1:43210', 'NO_PROXY': 'localhost'}
    out = b._do('exec_command', {'argv': ['owned-fixture'], 'execution_mode': 'blocking', 'env': overrides})
    assert len(calls) == 1 and calls[0][0] == 'command/exec'
    assert calls[0][1]['command'] == ['owned-fixture'] and calls[0][1]['env'] == overrides
    assert out['stdout'] == 'synthetic output'
