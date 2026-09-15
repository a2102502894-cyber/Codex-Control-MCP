"""Actual HTTP consent roundtrips and principal isolation, without a desktop app."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
import socket
from types import SimpleNamespace

import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
import pytest

from codex_control_mcp import server
from codex_control_mcp.auth import owner_token
from codex_control_mcp.common import ELICITATION_FORWARDER
from codex_control_mcp.config import Config, DEFAULT_CONFIG
from codex_control_mcp.lifecycle import request_stop
from codex_control_mcp.oauth import OwnerOAuth

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows service lifecycle')

REQUEST = {
    'mode': 'form', 'message': 'Allow the controlled test application?',
    'requestedSchema': {'type': 'object', 'properties': {}},
    '_meta': {'connector_id': 'computer-use', 'tool_params': {'app': 'ScopedFixture.exe'}},
}


@asynccontextmanager
async def running_fixture_server(tmp_path, monkeypatch):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    (tmp_path / 'config.toml').write_text(
        'computer_use_enabled = true\n' + DEFAULT_CONFIG.replace('8774', str(port))
        + '\n[oauth]\nenabled=true\nissuer="https://isolated-consent.test/"\n', 'utf-8')
    cfg = Config.load(tmp_path)
    assert cfg.computer_use_enabled and not cfg.local_application_consent
    cfg.initialize_storage()
    owner = owner_token(cfg, create=True)
    oauth = OwnerOAuth(cfg)
    try:
        client_a = oauth.issue_tokens('fixture-client-a', ['control']).access_token
        client_b = oauth.issue_tokens('fixture-client-b', ['control']).access_token
    finally:
        oauth.close()
    closed = []

    def execute(name, args):
        return {'ok': True, 'tool': name, 'result': ELICITATION_FORWARDER.get()(REQUEST)}

    monkeypatch.setattr(server, 'Bridge', lambda config: SimpleNamespace(
        cfg=config, execute=execute, close=lambda: closed.append(True)))
    service = asyncio.create_task(server.run_http(cfg))
    url = f'http://127.0.0.1:{port}/mcp'
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=1) as http:
            async with asyncio.timeout(10):
                while True:
                    if service.done():
                        await service
                        raise AssertionError('HTTP service exited before accepting requests')
                    try:
                        # An unauthenticated GET is rejected by OwnerHTTP once the
                        # loopback server is ready. Older transport revisions
                        # surfaced the manager's 411 here, while the hardened
                        # owner gate correctly returns 401 before delegation.
                        # Either response proves the fixture is accepting HTTP.
                        if (await http.get(url)).status_code in {401, 411}:
                            break
                    except httpx.ConnectError:
                        pass
                    await asyncio.sleep(0.05)
        yield url, {'owner': owner, 'oauth': client_a, 'alien': client_b}
    finally:
        state = tmp_path / 'state/service.json'
        if state.exists() and not service.done():
            request_stop(json.loads(state.read_text('utf-8'))['instance_id'])
        await asyncio.wait_for(service, 10)
        assert closed and not state.exists()


@pytest.mark.parametrize('action', ['accept', 'decline'])
@pytest.mark.parametrize('credential', ['owner', 'oauth'])
def test_http_consent_roundtrip_and_session_principal_isolation(tmp_path, monkeypatch, action, credential):
    received = []

    async def exercise():
        async def consent(context, params):
            received.append(params.model_dump(mode='json', by_alias=True))
            return types.ElicitResult(action=action, content={} if action == 'accept' else None)

        async with running_fixture_server(tmp_path, monkeypatch) as (url, credentials):
            async with httpx.AsyncClient(trust_env=False, timeout=8,
                    headers={'Authorization': 'Bearer ' + credentials[credential]}) as http:
                async with streamable_http_client(url, http_client=http) as (read, write, session_id):
                    async with ClientSession(read, write, elicitation_callback=consent) as client:
                        async with asyncio.timeout(12):
                            initialized = await client.initialize()
                            assert initialized.serverInfo.name == 'Codex-Control-MCP'
                            assert session_id(), 'Client consent needs a persistent session'
                            async with httpx.AsyncClient(trust_env=False, timeout=5) as alien:
                                # Another valid OAuth client may not read or answer this session.
                                response = await alien.post(url, headers={
                                    'Authorization': 'Bearer ' + credentials['alien'],
                                    'mcp-session-id': session_id(),
                                    'Accept': 'application/json, text/event-stream',
                                }, json={'jsonrpc': '2.0', 'id': 900, 'method': 'tools/list', 'params': {}})
                                # Current MCP manager deliberately hides a
                                # stateful session from a different authenticated
                                # principal with 404; older revisions used 414.
                                assert response.status_code in {404, 414}
                            result = await client.call_tool('computer_snapshot', {})
                            assert result.structuredContent['result']['action'] == action
                            assert len((await client.list_tools()).tools) == 39

    asyncio.run(exercise())
    assert len(received) == 1
    assert {key: received[0]['_meta'][key] for key in REQUEST['_meta']} == REQUEST['_meta']
    assert received[0]['requestedSchema'] == REQUEST['requestedSchema']
