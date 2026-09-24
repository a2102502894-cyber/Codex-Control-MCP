"""HTTP-stage telemetry without request/response bodies or authorization data."""
from __future__ import annotations

import contextvars
from dataclasses import dataclass
import time
import uuid

HTTP_OBSERVATION = contextvars.ContextVar("ccm_http_observation", default=None)


@dataclass
class HTTPObservation:
    request_id: str
    status: int | None = None
    gate_status: int | None = None
    response_complete: bool = False


def mark_http_gate_rejection(status: int) -> None:
    observation = HTTP_OBSERVATION.get()
    if observation is not None:
        observation.gate_status = status


def attach_transport_receipt(out: dict, audit=None) -> None:
    observation = HTTP_OBSERVATION.get()
    if observation is None:
        return
    out["http_request_id"] = observation.request_id
    if audit is not None:
        audit.emit("http_tool_receipt", http_request_id=observation.request_id,
                   operation_id=out.get("operation_id"), ok=out.get("ok"))


async def observe_http(audit, dispatch, scope, receive, send):
    if audit is None or scope.get("type") != "http" or scope.get("path") != "/mcp":
        return await dispatch(scope, receive, send)
    observation = HTTPObservation(uuid.uuid4().hex)
    token = HTTP_OBSERVATION.set(observation)
    started = time.monotonic()
    method = scope.get("method")
    method = method if method in {"GET", "POST", "DELETE", "OPTIONS", "HEAD"} else "OTHER"

    async def traced_send(message):
        if message.get("type") == "http.response.start":
            observation.status = message["status"]
            headers = [(k, v) for k, v in message.get("headers", [])
                       if k.lower() != b"x-ccm-request-id"]
            headers.append((b"x-ccm-request-id", observation.request_id.encode("ascii")))
            message = {**message, "headers": headers}
            audit.emit("http_response_started", http_request_id=observation.request_id,
                       status=observation.status, gate_status=observation.gate_status)
        await send(message)
        if message.get("type") == "http.response.body" and not message.get("more_body", False):
            observation.response_complete = True

    try:
        audit.emit("http_received", http_request_id=observation.request_id, http_method=method)
        return await dispatch(scope, receive, traced_send)
    except BaseException as exc:
        audit.emit("http_dispatch_exception", http_request_id=observation.request_id,
                   exception_type=type(exc).__name__)
        raise
    finally:
        try:
            audit.emit("http_finished", http_request_id=observation.request_id,
                       status=observation.status, gate_status=observation.gate_status,
                       response_complete=observation.response_complete,
                       duration_ms=round((time.monotonic() - started) * 1000, 3))
        finally:
            HTTP_OBSERVATION.reset(token)
