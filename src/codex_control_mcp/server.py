from __future__ import annotations
import asyncio, json, os, sys, time, uuid
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
    progress_observation = {"requested": progress_token is not None, "sent": 0, "failed_or_timed_out": 0, "client_display_confirmed": False}
    async def notify(progress, message):
        if progress_token is None:
            return
        try:
            with anyio.move_on_after(1) as scope:
                await context.session.send_progress_notification(
                    progress_token, progress, message=message,
                    related_request_id=context.request_id,
                )
            progress_observation["failed_or_timed_out" if scope.cancel_called else "sent"] += 1
        except Exception:
            progress_observation["failed_or_timed_out"] += 1
            # A disconnected progress consumer must not replay the action.

    def progress_message(elapsed):
        if name == "session_read" and isinstance(arguments, dict):
            try:
                state = bridge.sessions.get(arguments["session_id"]).metadata()
                if state["state"] == "exited":
                    return "进程已结束，正在返回剩余输出与最终退出码。"
                if state["state"] in {"lost", "failed"}:
                    return "会话结果出现异常，正在返回已知状态；不会自动重发原命令。"
                idle = state["heartbeat"].get("output_idle_ms") or 0
                return f"正在等待同一会话的最终结果；已等待约 {state['elapsed_ms'] / 1000:.1f} 秒，最近约 {idle / 1000:.1f} 秒无终端输出。"
            except Exception:
                pass  # Progress observation must not replace the actual result.
        return "正在执行工具，请稍候。" if elapsed == 0 else f"工具仍在执行，已等待约 {elapsed:.1f} 秒。"

    async def heartbeat():
        started = time.monotonic()
        await notify(0, progress_message(0))
        while True:
            await asyncio.sleep(PROGRESS_INTERVAL_SECONDS)
            elapsed = time.monotonic() - started
            await notify(elapsed, progress_message(elapsed))

    task = asyncio.create_task(heartbeat()) if progress_token is not None else None
    operation_id = uuid.uuid4().hex
    incoming_token = INCOMING_OPERATION.set(operation_id)
    try:
        audit = getattr(bridge, "audit", None)
        if audit is not None:
            audit.emit("mcp_received", tool=name, operation_id=operation_id)
        out = await anyio.to_thread.run_sync(bridge.execute, name, arguments)
    finally:
        INCOMING_OPERATION.reset(incoming_token)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    out = dict(out)
    out["progress_observation"] = dict(progress_observation)
    if audit is not None:
        try:
            audit.emit("mcp_progress_summary", operation_id=operation_id, tool=name, **progress_observation)
        except Exception:
            out["progress_observation"]["audit_status"] = "degraded"
    return out


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
        instructions=(
            "Use official Codex runtime operations. Before working, tell the user the next action. "
            "For a multi-step or full-project acceptance request, use task_manage to get/resume the matching task "
            "or create one with the user's full goal, all required steps and explicit completion_conditions. "
            "A successful tool call or a completed subprocess does not complete the user's task. "
            "Keep executing independent authorized remaining work after each stage summary; do not end with "
            "a next-step proposal when you can perform it. Checkpoint progress and evidence, recover revision "
            "conflicts by reading the latest task, and never overwrite or re-execute completed work blindly. "
            "Only stop for completion, a user stop instruction, or a concrete blocker that prevents further "
            "authorized progress. Record a blocker and exact resumption point; do not invent a blocker. "
            "Before final delivery, get the current task, finish all steps, resolve missing_checks, "
            "and submit final_review with each completion condition verbatim in verified plus supporting facts. "
            "Use expected_revision for updates. A passing review is not completion: call complete and verify "
            "task_completed=true before reporting the goal achieved. Changes after review require a new review. "
            "exec_command defaults to auto: running is NOT completion. Poll session_read using session_id "
            "and the complete next_action until continuation_required=false. A process exit is not full "
            "receipt delivery while has_more=true: drain every output page, check final_receipt_ready, "
            "exit_code and output gaps, then immediately report results and continue remaining user work. "
            "session_start also returns an initial output page; consume it before continuation. "
            "session_read waits at most 10 seconds for new output or exit and returns a heartbeat even "
            "for silent programs. With progressToken, the active poll request also receives progress. "
            "A heartbeat observes the bridge session, not proof of application-level progress. "
            "Report meaningful progress at least every 60 seconds during long work, even when program "
            "output is redirected to a task log; retain the log path and final exit status in checkpoints. "
            "Recover listed sessions using read_action; metadata.next_cursor alone is the stream end. "
            "Historical exit status may survive restart, but missing cached output must be verified "
            "from the original task log. Never resubmit a command because output or its response is absent. "
            "Use computer_close in finally after each desktop workflow; "
            "the bridge also releases idle control after 120 seconds. A new workflow needs a fresh snapshot. "
            "Commands run as the service account with dangerFullAccess. No model turns are started by this server; "
            "task state and continuation guidance cannot intercept a client's final reply or restart its model loop."
        ),
    )

    server._ccm_audit = getattr(bridge, "audit", None)

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
        from .http_observation import attach_transport_receipt
        attach_transport_receipt(out, getattr(bridge, "audit", None))
        if name == "codex_capabilities" and out.get("ok"):
            # Report only negotiated MCP client metadata/capabilities from the
            # current upstream connection. This is deliberately passive: it
            # does not issue sampling, elicitation, UI, or tool requests.
            params = getattr(context.session, "client_params", None)
            if params is None:
                # A stateless request has no negotiated client binding. The last
                # initialize observed by this process may belong to someone else,
                # even when the two requests share an authenticated principal.
                upstream = {
                    "observed": False,
                    "source": "stateless_http_no_client_binding",
                    "reason": "No initialize parameters are bound to this request.",
                }
                caps = {}
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
            types.TextContent(type="text", text=json.dumps(out, ensure_ascii=False, separators=(",", ":")))
        ] + [
            types.ImageContent(type="image", data=x["data"], mimeType=x["mimeType"])
            for x in images
        ]
        result = types.CallToolResult(
            content=content, structuredContent=out, isError=not out["ok"]
        )
        audit = getattr(bridge, "audit", None)
        if audit is not None:
            try:
                audit.emit("mcp_result_serialized", operation_id=out.get("operation_id"), tool=name,
                           result_json_bytes=len(result.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8")))
            except Exception:
                pass  # Serialization telemetry must never discard an action's receipt.
        return result

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
