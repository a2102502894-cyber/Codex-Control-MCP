"""Connection attribution must not borrow another request's initialize snapshot."""
import asyncio
from types import SimpleNamespace

import anyio
import httpx
import pytest
from mcp import ClientSession, types
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.shared.memory import create_client_server_memory_streams

from codex_control_mcp.http_guard import OwnerHTTP
from codex_control_mcp.server import make_server


def bridge():
    return SimpleNamespace(
        cfg=SimpleNamespace(oauth={}, browser_use_enabled=False, computer_use_enabled=False),
        execute=lambda name, args: {"ok": True, "result": {}},
    )


@pytest.mark.parametrize("capabilities", [{"sampling": {}}, {"elicitation": {"form": {}}}])
def test_stateless_request_does_not_inherit_previous_client(capabilities):
    manager = StreamableHTTPSessionManager(app=make_server(bridge()), json_response=True, stateless=True)
    gate = OwnerHTTP(manager, "fixture-owner")
    headers = {"Authorization": "Bearer fixture-owner", "Accept": "application/json, text/event-stream"}

    async def run():
        async with manager.run():
            transport = httpx.ASGITransport(app=gate)
            async with httpx.AsyncClient(transport=transport, base_url="http://localhost", headers=headers) as first:
                init = await first.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": "2025-03-26", "capabilities": capabilities,
                    "clientInfo": {"name": "FOREIGN_CLIENT_PRIVATE_MARKER", "version": "1"},
                }})
                assert init.status_code == 200
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gate), base_url="http://localhost", headers=headers) as second:
                result = await second.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                    "name": "codex_capabilities", "arguments": {},
                }})
                assert result.status_code == 200
                payload = result.json()["result"]["structuredContent"]["result"]["upstream_mcp_client"]
                assert payload["observed"] is False
                assert "FOREIGN_CLIENT_PRIVATE_MARKER" not in result.text
                assert payload.get("sampling_advertised") is not True
                assert payload.get("elicitation_advertised") is not True
    asyncio.run(run())


def test_stateful_session_still_reports_its_own_client():
    server = make_server(bridge())
    async def run():
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            async with anyio.create_task_group() as group:
                group.start_soon(server.run, *server_streams, server.create_initialization_options())
                async with ClientSession(*client_streams, client_info=types.Implementation(name="current-owned-client", version="1")) as client:
                    await client.initialize()
                    result = await client.call_tool("codex_capabilities", {})
                    data = result.structuredContent["result"]["upstream_mcp_client"]
                    assert data["observed"] and data["source"] == "stateful_server_session"
                    assert data["client_info"]["name"] == "current-owned-client"
                group.cancel_scope.cancel()
    asyncio.run(run())
