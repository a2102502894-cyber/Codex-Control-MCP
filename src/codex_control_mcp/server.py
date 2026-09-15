from __future__ import annotations
import asyncio, json, os, sys, uuid
import anyio
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.message import ServerMessageMetadata
from . import __version__
from .auth import owner_token
from .bridge import Bridge
from .common import ELICITATION_FORWARDER, atomic_json, utc_now
from .tools import TOOL_SPECS


def make_server(bridge):
    server = Server(
        "Codex-Control-MCP",
        version=__version__,
        instructions="This server exposes structured command, file, Git, session and diagnostic operations through an installed official Codex runtime. Commands run with the Windows service account's permissions and dangerFullAccess, without a workspace sandbox. The server does not start model or agent turns. Experimental GUI availability is reported by codex_capabilities. Results include execution status, timing and backend evidence.",
    )

    @server.list_tools()
    async def list_tools():
        return [
            types.Tool(
                name=n,
                description=s["description"],
                inputSchema=s["inputSchema"],
                _meta={"securitySchemes": [{"type": "oauth2", "scopes": ["control", "offline_access"]}]}
                if bridge.cfg.oauth.get("enabled")
                else None,
                annotations=types.ToolAnnotations(
                    readOnlyHint=s["readOnlyHint"],
                    destructiveHint=not s["readOnlyHint"],
                    idempotentHint=s["readOnlyHint"],
                    openWorldHint=True,
                ),
            )
            for n, s in TOOL_SPECS.items()
            if (not n.startswith("browser_") or bridge.cfg.browser_use_enabled)
            and (not n.startswith("computer_") or bridge.cfg.computer_use_enabled)
        ]

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        context = server.request_context
        loop = asyncio.get_running_loop()

        def forward_form(request):
            from .consent import resolve_local_application_consent

            decision = resolve_local_application_consent(
                bridge.cfg, request, getattr(bridge, "audit", None)
            )
            if decision is not None:
                return decision
            if request.get("mode", "form") != "form":
                raise ValueError("Unsupported elicitation mode")
            pending = asyncio.run_coroutine_threadsafe(
                context.session.send_request(
                    types.ServerRequest(types.ElicitRequest(
                        params=types.ElicitRequestFormParams.model_validate(request)
                    )),
                    types.ElicitResult,
                    metadata=ServerMessageMetadata(related_request_id=context.request_id),
                ),
                loop,
            )
            try:
                return pending.result(120).model_dump(mode="json", by_alias=True)
            except BaseException:
                pending.cancel()
                raise

        token = ELICITATION_FORWARDER.set(forward_form)
        try:
            out = await anyio.to_thread.run_sync(bridge.execute, name, arguments)
        finally:
            ELICITATION_FORWARDER.reset(token)
        out = dict(out)
        if name == "codex_capabilities" and out.get("ok"):
            # Report only negotiated MCP client metadata/capabilities from the
            # current upstream connection. This is deliberately passive: it
            # does not issue sampling, elicitation, UI, or tool requests.
            params = getattr(context.session, "client_params", None)
            if params is None:
                # Stateless HTTP intentionally does not retain initialize params
                # on ServerSession. OwnerHTTP passively records the sanitized,
                # authenticated initialize frame so we can still report the
                # actual upstream client's negotiated declarations.
                from .http_guard import latest_mcp_initialize

                upstream = latest_mcp_initialize() or {"observed": False}
                caps = upstream.get("capabilities") or {}
            else:
                dumped = params.model_dump(mode="json", by_alias=True)
                caps = dumped.get("capabilities") or {}
                upstream = {
                    "observed": True,
                    "source": "stateful_server_session",
                    "protocol_version": dumped.get("protocolVersion"),
                    "client_info": dumped.get("clientInfo"),
                    "capabilities": caps,
                }
            if upstream.get("observed"):
                sampling = caps.get("sampling")
                upstream.update({
                    "sampling_advertised": sampling is not None,
                    "sampling_tools_advertised": bool(
                        isinstance(sampling, dict) and sampling.get("tools") is not None
                    ),
                    "elicitation_advertised": caps.get("elicitation") is not None,
                    "roots_advertised": caps.get("roots") is not None,
                    "tasks_advertised": caps.get("tasks") is not None,
                })
            result = out.get("result")
            if isinstance(result, dict):
                result["upstream_mcp_client"] = upstream
        images = out.pop("_image_blocks", [])
        content = [
            types.TextContent(type="text", text=json.dumps(out, ensure_ascii=False))
        ] + [
            types.ImageContent(type="image", data=x["data"], mimeType=x["mimeType"])
            for x in images
        ]
        return types.CallToolResult(
            content=content, structuredContent=out, isError=not out["ok"]
        )

    return server


async def run_stdio(cfg):
    bridge = Bridge(cfg)
    server = make_server(bridge)
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        await anyio.to_thread.run_sync(bridge.close)


from .http_guard import OwnerHTTP


async def run_http(cfg, host=None, port=None):
    import uvicorn

    bridge = Bridge(cfg)
    server = make_server(bridge)
    h = cfg.http.get("host", "127.0.0.1") if host is None else host
    p = cfg.http.get("port", 8774) if port is None else port
    if type(p) is not int or not 1 <= p <= 65535:
        bridge.close()
        from .errors import BridgeError

        raise BridgeError("invalid_config", "HTTP port must be between 1 and 65535")
    if h not in ("127.0.0.1", "::1", "localhost"):
        bridge.close()
        from .errors import BridgeError

        raise BridgeError(
            "invalid_config",
            "Use a TLS reverse proxy to the authenticated loopback endpoint; do not expose full-access plaintext MCP.",
        )
    try:
        token = owner_token(cfg)
    except Exception:
        bridge.close()
        raise
    hosts = set(cfg.http.get("allowed_hosts", [])) | {
        f"127.0.0.1:{p}",
        f"localhost:{p}",
        f"[::1]:{p}",
    }
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted(hosts),
        allowed_origins=cfg.http.get("allowed_origins", []),
    )
    # Client-mediated application consent needs an SSE request stream and a
    # surviving session to route the client's elicitation response back to it.
    client_consent = cfg.requires_client_elicitation
    manager = StreamableHTTPSessionManager(
        server,
        stateless=not client_consent,
        json_response=not client_consent,
        security_settings=security,
        max_request_body_size=cfg.http.get("request_limit_bytes", 2097152),
        max_sessions=128,
        session_idle_timeout=300,
    )
    oauth = None
    if cfg.oauth.get("enabled"):
        try:
            from .oauth import OwnerOAuth

            oauth = OwnerOAuth(cfg)
        except Exception:
            bridge.close()
            raise
    endpoint = OwnerHTTP(
        manager,
        token,
        cfg.http.get("request_limit_bytes", 2097152),
        cfg.http.get("requests_per_minute", 120),
        oauth=oauth,
        hosts=hosts,
        body_timeout=cfg.http.get("body_timeout_seconds", 15),
    )

    class App:
        async def __call__(self, scope, receive, send):
            if scope["type"] == "lifespan":
                msg = await receive()
                if msg["type"] != "lifespan.startup":
                    return
                try:
                    async with manager.run():
                        await send({"type": "lifespan.startup.complete"})
                        await receive()
                finally:
                    await anyio.to_thread.run_sync(bridge.close)
                await send({"type": "lifespan.shutdown.complete"})
            else:
                await endpoint(scope, receive, send)

    record_path = cfg.home / "state/service.json"
    instance_id = uuid.uuid4().hex
    from .lifecycle import StopEvent

    stop_event = None
    try:
        stop_event = StopEvent(instance_id) if os.name == "nt" else None
        atomic_json(
            record_path,
            {
                "host": h,
                "port": p,
                "pid": os.getpid(),
                "executable": sys.executable,
                "version": __version__,
                "instance_id": instance_id,
                "started_at": utc_now(),
                "transport": "streamable-http",
                "auth": "owner_bearer_or_oauth" if oauth else "bearer_required",
                "graceful_stop_supported": stop_event is not None,
            },
        )
    except BaseException:
        if stop_event is not None:
            stop_event.close()
        bridge.close()
        if oauth is not None:
            oauth.close()
        raise
    service = uvicorn.Server(
        uvicorn.Config(
            App(),
            host=h,
            port=p,
            log_level="warning",
            access_log=False,
            timeout_keep_alive=10,
            timeout_graceful_shutdown=20,
        )
    )

    async def monitor_stop():
        if stop_event is None:
            return
        while not service.should_exit:
            if stop_event.requested():
                service.should_exit = True
                return
            await asyncio.sleep(0.2)

    monitor = asyncio.create_task(monitor_stop())
    try:
        await service.serve()
    finally:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
        if stop_event is not None:
            stop_event.close()
        bridge.close()
        if oauth is not None:
            oauth.close()
        try:
            if (
                json.loads(record_path.read_text("utf-8")).get("instance_id")
                == instance_id
            ):
                record_path.unlink()
        except (OSError, ValueError):
            pass
