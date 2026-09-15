"""Deterministic protocol-fault tests. No official runtime or user process is run."""
import concurrent.futures
import io
import json
import threading
from types import SimpleNamespace

import pytest
from codex_control_mcp.rpc import AppServer
from codex_control_mcp.errors import BridgeError


def reader_for(frame):
    rpc=AppServer.__new__(AppServer)
    rpc.closed=False;rpc.broken=False;rpc.generation='fault-test'
    rpc.lock=threading.RLock();rpc.write_lock=threading.Lock();rpc.next_id=1
    future=concurrent.futures.Future();rpc.pending={1:('command/exec',future)}
    rpc.proc=SimpleNamespace(stdout=io.BytesIO((json.dumps(frame)+'\n').encode()),poll=lambda:None)
    rpc.audit=SimpleNamespace(emit=lambda *a,**k:None)
    rpc.schema=SimpleNamespace(validate_response=lambda *a:None)
    rpc.callback=lambda m:None;rpc._write=lambda m:None
    return rpc,future


@pytest.mark.parametrize('frame',[
    [],42,'not-an-object',{'id':1,'error':'not-an-error-object'},
    {'id':1,'result':{},'error':{'code':-1,'message':'conflict'}},
    {'id':1},{'id':[1],'result':{}},{'id':1,'error':{'code':True,'message':'bad code'}},
    {'method':22,'params':{}},{'method':'','params':{}},
])
def test_malformed_response_fails_pending_request_instead_of_hanging(frame):
    rpc,future=reader_for(frame)
    rpc._read()
    assert future.done(), 'Malformed upstream response left its request pending forever'
    with pytest.raises(BridgeError):future.result()
    assert rpc.broken and not rpc.pending


def test_valid_response_completes_before_stream_eof():
    rpc,future=reader_for({'id':1,'result':{'exitCode':0}})
    rpc._read()
    assert future.result()=={'exitCode':0}


def test_closed_or_broken_transport_rejects_new_request_before_write():
    rpc,_=reader_for({'id':1,'result':{}})
    rpc.broken=True
    rpc.schema=SimpleNamespace(validate=lambda *a:None)
    writes=[];rpc._write=writes.append
    with pytest.raises(BridgeError):rpc.begin('command/exec',{})
    assert not writes


def test_pending_request_bookkeeping_is_bounded():
    rpc,_=reader_for({'id':1,'result':{}})
    rpc.schema=SimpleNamespace(validate=lambda *a:None)
    rpc.pending={i:('command/exec',concurrent.futures.Future()) for i in range(256)}
    with pytest.raises(BridgeError) as error:rpc.begin('command/exec',{})
    assert error.value.code=='resource_limit'
    assert len(rpc.pending)==256
