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
from .diagnostics import INCOMING_OPERATION
from .tools import TOOL_SPECS

PROGRESS_INTERVAL_SECONDS = 5


async def execute_with_progress(bridge, context, name, arguments):
    """Best-effort status; never replay or cancel an action on delivery failure."""
    progress_token = getattr(context.meta, "progressToken", None)
    async def notify(progress, message):
        if progress_token is None:
            return
        try:
            with anyio.move_on_after(1):
                await context.session.send_progress_notification(
                    progress_token, progress, message=message,
                    related_request_id=context.request_id,
                )
        except Exception:
            pass  # A disconnected progress consumer must not replay the action.

    async def heartbeat():
        elapsed = 0
        await notify(0, "正在执行工具，请稍候。")
        while True:
            await asyncio.sleep(PROGRESS_INTERVAL_SECONDS)
            elapsed += PROGRESS_INTERVAL_SECONDS
            await notify(elapsed, f"工具仍在执行，已等待约 {elapsed:g} 秒。")

    task = asyncio.create_task(heartbeat()) if progress_token is not None else None
    operation_id = uuid.uuid4().hex
    incoming_token = INCOMING_OPERATION.set(operation_id)
    try:
        audit = getattr(bridge, "audit", None)
        if audit is not None:
            audit.emit("mcp_received", tool=name, operation_id=operation_id)
        return await anyio.to_thread.run_sync(bridge.execute, name, arguments)
    finally:
        INCOMING_OPERATION.reset(incoming_token)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def tool_metadata(name, oauth_enabled):
    meta = {
        "openai/toolInvocation/invoking": "正在释放桌面控制…" if name == "computer_close" else "正在执行，请稍候…",
        "openai/toolInvocation/invoked": "桌面控制已返回状态" if name == "computer_close" else "已返回执行状态",
    }
    if oauth_enabled:
        meta["securitySchemes"] = [{"type": "oauth2", "scopes": ["control", "offline_access"]}]
    return meta


def make_server(bridge):
    server = Server(
        "Codex-Control-MCP",
        version=__version__,
        instructions="Use official Codex runtime operations. Before working, tell the user the next action. exec_command defaults to auto: running is NOT completion. Poll session_read using session_id and next_cursor until a final exit code; report meaningful progress and never resubmit the same command because output is absent. Use computer_close in finally after each desktop workflow; the bridge also releases idle control after 120 seconds. A new workflow needs a fresh snapshot. Commands run as the service account with dangerFullAccess. No model turns are started by this server.",
    )

    @server.list_tools()
    async def list_tools():
        return [
            types.Tool(
                name=n,
                description=s["description"],
                inputSchema=s["inputSchema"],
                _meta=tool_metadata(n, bridge.cfg.oauth.get("enabled")),
                outputSchema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": True},
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
            out = await execute_with_progress(bridge, context, name, arguments)
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
    # Progress must travel on the tool request's SSE stream even when application
    # access is preapproved. Stateful sessions are needed only for elicitation.
    client_consent = cfg.requires_client_elicitation
    manager = StreamableHTTPSessionManager(
        server,
        stateless=not client_consent,
        json_response=False,
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
