"""Local CLI access to the owner's already-running MCP service."""

import json
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from .auth import owner_token
from .errors import BridgeError


async def call_running_service(cfg, tool, args):
    path = cfg.home / "state/service.json"
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    host = record.get("host")
    port = record.get("port")
    if (
        host not in ("127.0.0.1", "localhost", "::1")
        or type(port) is not int
        or not 1 <= port <= 65535
    ):
        return None
    formatted = "[" + host + "]" if ":" in host else host
    url = f"http://{formatted}:{port}/mcp"
    token = owner_token(cfg)
    dispatched = False
    try:
        async with httpx.AsyncClient(
            headers={"Authorization": "Bearer " + token},
            trust_env=False,
            timeout=httpx.Timeout(180, connect=2),
        ) as http:
            async with streamable_http_client(url, http_client=http) as (
                read,
                write,
                _,
            ):
                async with ClientSession(read, write) as session:
                    init = await session.initialize()
                    if init.serverInfo.name != "Codex-Control-MCP":
                        raise BridgeError(
                            "version_incompatible",
                            "The recorded local port does not host Codex-Control-MCP.",
                        )
                    dispatched = True
                    result = await session.call_tool(tool, args)
                    data = result.structuredContent
                    if data is None:
                        text = next(
                            (x.text for x in result.content if x.type == "text"), None
                        )
                        data = json.loads(text) if text else None
                    if not isinstance(data, dict):
                        raise BridgeError(
                            "version_incompatible",
                            "The running service returned an unexpected result.",
                        )
                    data["via_running_service"] = True
                    return data
    except BridgeError:
        raise
    except Exception as exc:
        if dispatched:
            raise BridgeError(
                "execution_state_unknown",
                "The running service may have executed the request; no local fallback or replay is allowed.",
            ) from exc
        # A stale service record does not imply that a server is alive. The caller
        # may attempt the local instance lock; it must never kill the recorded PID.
        return None
