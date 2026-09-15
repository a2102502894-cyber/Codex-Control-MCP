"""No-GUI regression cases using isolated state; no production services touched."""
import base64
import concurrent.futures
from types import SimpleNamespace

import pytest
from codex_control_mcp.bridge import Bridge
from codex_control_mcp.config import Config
from codex_control_mcp.errors import BridgeError
from codex_control_mcp.sessions import SessionStore


def test_cached_session_output_read_does_not_need_official_runtime(tmp_path):
    bridge=Bridge(Config(home=tmp_path/'home',cwd=str(tmp_path)))
    try:
        s=bridge.sessions.create('old-runtime',str(tmp_path))
        s.append({'deltaBase64':base64.b64encode(b'ALREADY_BUFFERED\n').decode(),'stream':'stdout'})
        done=concurrent.futures.Future();done.set_result({'exitCode':0});s.finish(done)
        def unavailable(*args,**kwargs):
            raise BridgeError('runtime_missing','Isolated test: official installation unavailable')
        bridge.ensure_ready=unavailable
        result=bridge.execute('session_read',{'session_id':s.id})
        assert result['ok'], result['error']
        assert result['result']['stdout']=='ALREADY_BUFFERED\n'
    finally:bridge.close()


def test_invalid_session_request_does_not_leak_starting_slot(tmp_path):
    bridge=Bridge(Config(home=tmp_path/'home',cwd=str(tmp_path),max_sessions=2))
    bridge.ensure_ready=lambda *a,**kw: None
    bridge.rpc=SimpleNamespace(generation='isolated',close=lambda:None)
    try:
        for _ in range(5):
            result=bridge.execute('session_start',{})
            assert not result['ok'] and result['error']['code']=='invalid_arguments',result
        assert not any(s['state'] in ('starting','running') for s in bridge.sessions.list())
    finally:bridge.close()


def test_recent_session_metadata_survives_multiple_bridge_restarts(tmp_path):
    path=tmp_path/'sessions.json'
    first=SessionStore(path,4096,2)
    previous=first.create('generation-one',str(tmp_path));previous.state='exited';first.save()
    second=SessionStore(path,4096,2)
    latest=[]
    for i in range(2):
        s=second.create('generation-two',str(tmp_path));s.created_at=f'2099-01-0{i+1}T00:00:00Z';s.state='exited';latest.append(s.id)
    second.save()
    third=SessionStore(path,4096,2)
    assert {x['session_id'] for x in third.list()}==set(latest)


def test_session_read_chunks_cannot_mutate_cached_history(tmp_path):
    store=SessionStore(tmp_path/'sessions.json',4096,2)
    s=store.create('isolated',str(tmp_path))
    s.append({'deltaBase64':base64.b64encode(b'original').decode(),'stream':'stdout'})
    response=s.read()
    response['chunks'][0]['text']='tampered'
    assert s.read()['stdout']=='original'


def test_session_cursor_above_available_output_is_rejected(tmp_path):
    s=SessionStore(tmp_path/'sessions.json',4096,2).create('isolated',str(tmp_path))
    with pytest.raises(BridgeError,match='cursor'):
        s.read(cursor=100)


def test_many_tiny_output_events_have_bounded_object_count(tmp_path):
    s=SessionStore(tmp_path/'sessions.json',262144,2).create('isolated',str(tmp_path))
    for _ in range(10000):
        s.append({'deltaBase64':'eA==','stream':'stdout'})
    assert len(s.events)==8192 and s.bytes_cached==8192
    assert s.total_output_bytes==10000 and s.dropped_output_bytes==1808
    result=s.read()
    assert result['cursor_gap'] is True and result['output_truncated'] is True


def test_rejected_dispatch_does_not_leave_active_session(tmp_path):
    bridge=Bridge(Config(home=tmp_path/'home',cwd=str(tmp_path),max_sessions=2))
    bridge.ensure_ready=lambda *a,**k:None
    def rejected(*a,**k):
        raise BridgeError('version_incompatible','Isolated contract rejection')
    bridge.rpc=SimpleNamespace(generation='isolated',begin=rejected,close=lambda:None)
    try:
        for _ in range(5):
            result=bridge.execute('session_start',{'command':'does not execute'})
            assert not result['ok'] and result['error']['code']=='version_incompatible'
        assert not any(s['state'] in ('starting','running') for s in bridge.sessions.list())
    finally:
        bridge.close()


def test_long_session_cursor_and_errors_are_detached(tmp_path):
    bridge=Bridge(Config(home=tmp_path/'home',cwd=str(tmp_path)))
    try:
        s=bridge.sessions.create('isolated',str(tmp_path))
        s.next_cursor=2_000_000
        s.error={'code':'fixture','details':{'value':'original'}}
        result=bridge.execute('session_read',{'session_id':s.id,'cursor':2_000_000})
        assert result['ok'] and result['result']['next_cursor']==2_000_000
        result['result']['error']['details']['value']='changed'
        assert s.error['details']['value']=='original'
        bridge.sessions.save()
        previous=SessionStore(bridge.sessions.path,4096,2)
        values=previous.list()
        values[0]['cwd']='changed'
        assert previous.list()[0]['cwd']!= 'changed'
    finally:
        bridge.close()
