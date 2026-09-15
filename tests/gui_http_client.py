"""Acceptance client for the existing owner-authenticated local MCP service."""
import asyncio
import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from codex_control_mcp import __version__
from codex_control_mcp.auth import owner_token
from codex_control_mcp.config import Config


class HttpFixtureBridge:
    def __init__(self, home, consent):
        self.cfg = Config.load(home)
        self.consent = consent
        self.url = f'http://127.0.0.1:{self.cfg.http.get("port", 8767)}/mcp'

    def execute(self, name, args):
        async def call():
            async def elicit(context, params):
                decision = self.consent(params.model_dump(mode='json', by_alias=True))
                return types.ElicitResult.model_validate(decision)
            async with httpx.AsyncClient(trust_env=False, timeout=200,
                    headers={'Authorization': 'Bearer ' + owner_token(self.cfg)}) as http:
                async with streamable_http_client(self.url, http_client=http) as (read, write, _):
                    async with ClientSession(read, write, elicitation_callback=elicit) as session:
                        initialized = await session.initialize()
                        assert initialized.serverInfo.name == 'Codex-Control-MCP'
                        assert initialized.serverInfo.version == __version__
                        result = await session.call_tool(name, args)
                        out = dict(result.structuredContent)
                        out['_image_blocks'] = [item.model_dump(mode='json') for item in result.content if item.type == 'image']
                        out['acceptance_transport'] = 'streamable_http'
                        return out
        return asyncio.run(call())

    def close(self):
        # Each completed request closes its own client connection. The product
        # service remains running and is never stopped by this fixture client.
        pass
