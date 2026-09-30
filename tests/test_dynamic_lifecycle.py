import asyncio
import concurrent.futures
import json
import sys
import threading
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from mcp import types

from codex_control_mcp.dynamic_mcp import DynamicMCPManager
from codex_control_mcp.errors import BridgeError


def manager(tmp_path):
    m = DynamicMCPManager(SimpleNamespace(home=tmp_path))
    m.manage({'action': 'register', 'name': 'fixture', 'transport': 'stdio', 'command': sys.executable, 'timeout_ms': 1000})
    return m


class Session:
    def __init__(self, pages=None):
        self.calls = 0
        self.pages = pages or {None: types.ListToolsResult(tools=[types.Tool(name='count', inputSchema={'type': 'object'})])}
    async def list_tools(self, cursor=None):
        return self.pages[cursor]
    async def call_tool(self, name, args):
        self.calls += 1
        return types.CallToolResult(content=[], structuredContent={'count': self.calls, 'name': name})


def fake_transport(m, session):
    owners, entered, exited = [], [], []
    async def connect(item, operation):
        task = asyncio.current_task()
        owners.append(task)
        entered.append(item['name'])
        try:
            return await operation(session)
        finally:
            assert asyncio.current_task() is task
            exited.append(item['name'])
    m._with_session = connect
    return owners, entered, exited


def test_owner_reuse_pagination_last_page_and_close(tmp_path):
    m = manager(tmp_path)
    pages = {None: types.ListToolsResult(tools=[types.Tool(name='first', inputSchema={'type': 'object'})], nextCursor='last'), 'last': types.ListToolsResult(tools=[types.Tool(name='last', inputSchema={'type': 'object'})])}
    session = Session(pages)
    owners, entered, exited = fake_transport(m, session)
    assert len(m.refresh('fixture')['tools']) == 2
    assert m.call({'name': 'fixture:last', 'arguments': {}})['result']['structuredContent']['count'] == 1
    assert m.call({'name': 'fixture:last', 'arguments': {}})['result']['structuredContent']['count'] == 2
    assert len(owners) == 1
    m.close()
    m.close()
    assert entered == exited == ['fixture'] and not m.thread.is_alive()


@pytest.mark.parametrize('invalid', ['loop', 'duplicate', 'pages', 'count'])
def test_bad_pagination_preserves_complete_cache(tmp_path, invalid):
    m = manager(tmp_path)
    session = Session()
    fake_transport(m, session)
    m.refresh('fixture')
    old = m.servers['fixture']['tools']
    if invalid == 'loop':
        session.pages = {None: types.ListToolsResult(tools=[], nextCursor='same'), 'same': types.ListToolsResult(tools=[], nextCursor='same')}
    elif invalid == 'duplicate':
        tool = types.Tool(name='duplicated', inputSchema={})
        session.pages = {None: types.ListToolsResult(tools=[tool], nextCursor='next'), 'next': types.ListToolsResult(tools=[tool])}
    elif invalid == 'pages':
        session.pages = {None: types.ListToolsResult(tools=[], nextCursor='0'), **{str(i): types.ListToolsResult(tools=[], nextCursor=str(i+1)) for i in range(128)}}
    else:
        session.pages = {None: types.ListToolsResult(tools=[types.Tool(name=f't{i}', inputSchema={}) for i in range(10001)])}
    with pytest.raises(BridgeError):
        m.refresh('fixture')
    assert m.servers['fixture']['tools'] == old
    m.close()


def test_empty_list_loaded_once(tmp_path):
    m = manager(tmp_path)
    session = Session({None: types.ListToolsResult(tools=[])})
    fake_transport(m, session)
    requests = []
    original = session.list_tools
    async def list_tools(cursor=None):
        requests.append(1)
        return await original(cursor)
    session.list_tools = list_tools
    assert m.search({})['count'] == 0
    assert m.search({})['count'] == 0 and requests == [1]
    m.close()


def test_post_dispatch_timeout_once_no_retry(tmp_path):
    m = manager(tmp_path)
    session = Session()
    owners, entered, exited = fake_transport(m, session)
    async def slow(name, args):
        session.calls += 1
        await asyncio.Event().wait()
    session.call_tool = slow
    m.refresh('fixture')
    with pytest.raises(BridgeError) as exc:
        m.call({'name': 'fixture:count', 'arguments': {}})
    assert exc.value.code == 'execution_state_unknown' and session.calls == 1
    m.close()
    assert entered == exited


def test_refresh_update_generation_race(tmp_path):
    m = manager(tmp_path)
    session = Session()
    fake_transport(m, session)
    started = threading.Event()
    async def slow(cursor=None):
        started.set()
        await asyncio.Event().wait()
    session.list_tools = slow
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        refreshed = pool.submit(m.refresh, 'fixture')
        assert started.wait(3)
        m.manage({'action': 'update', 'name': 'fixture', 'args': ['changed']})
        with pytest.raises(BridgeError):
            refreshed.result(3)
    assert m.servers['fixture']['generation'] == 1
    assert not m.servers['fixture']['tools'] and m.servers['fixture']['status'] == 'never_refreshed'
    m.close()


def test_stateful_real_stdio_across_refresh_calls(tmp_path):
    script = tmp_path / 'stateful.py'
    script.write_text('''import asyncio, os
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
s=Server("counter")
n=0
@s.list_tools()
async def tools():
    return [types.Tool(name="count",inputSchema={"type":"object"})]
@s.call_tool()
async def call(name,args):
    global n
    n+=1
    return types.CallToolResult(content=[],structuredContent={"count":n,"pid":os.getpid()})
async def main():
    async with stdio_server() as (r,w):
        await s.run(r,w,s.create_initialization_options())
asyncio.run(main())
''')
    m = manager(tmp_path)
    m.manage({'action': 'update', 'name': 'fixture', 'args': [str(script)], 'timeout_ms': 5000})
    m.refresh('fixture')
    for count in (1, 2):
        result = m.call({'name': 'fixture:count', 'arguments': {}})['result']['structuredContent']
        assert result['count'] == count
        pid = result['pid']
    m.refresh('fixture')
    assert m.call({'name': 'fixture:count', 'arguments': {}})['result']['structuredContent']['count'] == 3
    m.manage({'action': 'disable', 'name': 'fixture'})
    assert m.owners == {}
    import os
    if os.name == 'posix':
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    m.close()
    assert not m.thread.is_alive()


def test_http_context_and_session_reused(tmp_path, monkeypatch):
    import codex_control_mcp.dynamic_mcp as module
    m = DynamicMCPManager(SimpleNamespace(home=tmp_path))
    m.manage({'action': 'register', 'name': 'fixture', 'transport': 'streamable_http', 'url': 'http://localhost:9999', 'timeout_ms': 1000})
    opened, closed = [], []
    @asynccontextmanager
    async def transport(url, **kwargs):
        opened.append(asyncio.current_task())
        try:
            yield None, None, None
        finally:
            closed.append(asyncio.current_task())
    class Client(Session):
        def __init__(self, *a):
            super().__init__()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            pass
        async def initialize(self):
            pass
    monkeypatch.setattr(module, 'streamable_http_client', transport)
    monkeypatch.setattr(module, 'ClientSession', Client)
    m.refresh('fixture')
    assert m.call({'name': 'fixture:count', 'arguments': {}})['result']['structuredContent']['count'] == 1
    assert m.call({'name': 'fixture:count', 'arguments': {}})['result']['structuredContent']['count'] == 2
    m.close()
    assert len(opened) == 1 and opened == closed


def test_initialization_deadline_cleans_owner(tmp_path):
    m = manager(tmp_path)
    attempts, cleaned = [], []
    async def never_ready(item, operation):
        attempts.append(1)
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append(1)
    m._with_session = never_ready
    with pytest.raises(BridgeError) as exc:
        m.refresh('fixture')
    assert exc.value.code == 'mcp_unavailable'
    m.close()
    assert attempts == cleaned == [1] and not m.thread.is_alive()


def test_bounded_queue_and_expired_requests_never_dispatch(tmp_path):
    m = manager(tmp_path)
    m._start_loop()
    async def never_ready(item, operation):
        await asyncio.Event().wait()
    m._with_session = never_ready
    calls = []
    async def operation(session):
        calls.append(1)
    async def many():
        item = dict(m.servers['fixture'])
        return await asyncio.gather(*(m._submit(item, operation, True, None) for _ in range(66)), return_exceptions=True)
    results = asyncio.run_coroutine_threadsafe(many(), m.loop).result(4)
    assert sum(isinstance(x, BridgeError) and x.code == 'resource_limit' for x in results) == 2
    assert calls == []
    m.close()


def test_servers_are_isolated_and_remove_register_has_new_generation(tmp_path):
    m = manager(tmp_path)
    m.manage({'action': 'register', 'name': 'second', 'transport': 'stdio', 'command': sys.executable})
    sessions = {}
    async def connect(item, operation):
        session = sessions.setdefault((item['name'], item['generation']), Session())
        return await operation(session)
    m._with_session = connect
    for name in ('fixture', 'second'):
        m.refresh(name)
        assert m.call({'name': f'{name}:count', 'arguments': {}})['result']['structuredContent']['count'] == 1
    old_generation = m.servers['fixture']['generation']
    m.manage({'action': 'remove', 'name': 'fixture'})
    m.manage({'action': 'register', 'name': 'fixture', 'transport': 'stdio', 'command': sys.executable})
    assert m.servers['fixture']['generation'] != old_generation
    m.refresh('fixture')
    assert m.call({'name': 'fixture:count', 'arguments': {}})['result']['structuredContent']['count'] == 1
    assert m.call({'name': 'second:count', 'arguments': {}})['result']['structuredContent']['count'] == 2
    m.close()


def test_real_loopback_http_session_reuse(tmp_path):
    import socket
    import uvicorn
    from mcp.server.fastmcp import FastMCP
    server = FastMCP('http-counter')
    counts = {}
    @server.tool()
    def count() -> dict:
        sid = id(server._mcp_server.request_context.session)
        counts[sid] = counts.get(sid, 0) + 1
        return {'count': counts[sid]}
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    app = server.streamable_http_app()
    runner = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error'))
    thread = threading.Thread(target=lambda: asyncio.run(runner.serve(sockets=[sock])), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not runner.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runner.started
    m = DynamicMCPManager(SimpleNamespace(home=tmp_path))
    try:
        m.manage({'action': 'register', 'name': 'fixture', 'transport': 'streamable_http', 'url': f'http://127.0.0.1:{port}/mcp', 'timeout_ms': 5000})
        m.refresh('fixture')
        for expected in (1, 2):
            result = m.call({'name': 'fixture:count', 'arguments': {}})['result']
            assert not result.get('isError'), result
            data = result.get('structuredContent') or json.loads(result['content'][0]['text'])
            assert data['count'] == expected
        m.refresh('fixture')
        result = m.call({'name': 'fixture:count', 'arguments': {}})['result']
        assert not result.get('isError'), result
        data = result.get('structuredContent') or json.loads(result['content'][0]['text'])
        assert data['count'] == 3
        assert len(counts) == 1
    finally:
        m.close()
        runner.should_exit = True
        thread.join(5)
        sock.close()
    assert not m.thread.is_alive() and not thread.is_alive()


def test_dynamic_association_failure_prevents_tool_dispatch(tmp_path):
    from codex_control_mcp.common import CURRENT_TASK_EXECUTION
    m = manager(tmp_path)
    session = Session()
    fake_transport(m, session)
    m.refresh('fixture')
    def reject(*args):
        raise BridgeError('task_state_unavailable', 'Fixture association failed')
    m.before_dispatch = reject
    token = CURRENT_TASK_EXECUTION.set('operation')
    try:
        with pytest.raises(BridgeError) as exc:
            m.call({'name': 'fixture:count', 'arguments': {}})
        assert exc.value.code == 'task_state_unavailable'
        assert session.calls == 0
    finally:
        CURRENT_TASK_EXECUTION.reset(token)
        m.close()
