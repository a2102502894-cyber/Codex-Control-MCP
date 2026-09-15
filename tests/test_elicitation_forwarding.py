"""Real MCP protocol roundtrip for scoped application consent metadata."""
import asyncio
from types import SimpleNamespace

import anyio
import pytest
from mcp import ClientSession, types
from mcp.shared.memory import create_client_server_memory_streams

from codex_control_mcp.common import ELICITATION_FORWARDER
from codex_control_mcp.server import make_server


@pytest.mark.parametrize('action', ['accept', 'decline'])
def test_external_client_receives_original_app_scope_and_controls_decision(action):
    request = {'mode': 'form', 'message': 'Allow the controlled test application?',
               'requestedSchema': {'type': 'object', 'properties': {}},
               '_meta': {'connector_id': 'computer-use', 'tool_params': {'app': 'ScopedFixture.exe'}}}
    received = []

    def execute(name, args):
        decision = ELICITATION_FORWARDER.get()(request)
        return {'ok': True, 'tool': name, 'result': decision}

    bridge = SimpleNamespace(cfg=SimpleNamespace(oauth={}, local_application_consent=False,
                              browser_use_enabled=False, computer_use_enabled=True), execute=execute)
    server = make_server(bridge)

    async def run():
        async def consent(context, params):
            received.append(params.model_dump(mode='json', by_alias=True))
            return types.ElicitResult(action=action, content={} if action == 'accept' else None)
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            async with anyio.create_task_group() as group:
                group.start_soon(server.run, *server_streams, server.create_initialization_options())
                async with ClientSession(*client_streams, elicitation_callback=consent) as client:
                    await client.initialize()
                    result = await client.call_tool('computer_snapshot', {})
                    assert result.structuredContent['result']['action'] == action
                group.cancel_scope.cancel()

    asyncio.run(run())
    assert len(received) == 1
    assert {key: received[0]['_meta'][key] for key in request['_meta']} == request['_meta']
    assert received[0]['requestedSchema'] == request['requestedSchema']
