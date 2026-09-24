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
    audit_error_count: int = 0


def _record_after_dispatch(audit, observation, event, **fields):
    """Report secondary logging failures without losing an action's result.

    Initial arrival logging is still mandatory before dispatch. This helper is
    only for result and cleanup records after a request has been accepted.
    """
    try:
        audit.emit(event, http_request_id=observation.request_id, **fields)
    except Exception:
        observation.audit_error_count += 1


def _audit_summary(observation):
    return {"audit_status": "degraded" if observation.audit_error_count else "recorded",
            "audit_error_count": observation.audit_error_count,
            "client_receipt_confirmed": False}


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
        _record_after_dispatch(audit, observation, "http_tool_receipt",
                               operation_id=out.get("operation_id"), ok=out.get("ok"))
        out["transport_observation"] = {**_audit_summary(observation),
                                        "scope": "at_tool_receipt_creation"}


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
                       if k.lower() not in {b"x-ccm-request-id", b"x-ccm-audit-status"}]
            headers.append((b"x-ccm-request-id", observation.request_id.encode("ascii")))
            _record_after_dispatch(audit, observation, "http_response_started",
                                   status=observation.status, gate_status=observation.gate_status)
            if observation.audit_error_count:
                headers.append((b"x-ccm-audit-status", b"degraded"))
            message = {**message, "headers": headers}
        await send(message)
        if message.get("type") == "http.response.body" and not message.get("more_body", False):
            observation.response_complete = True

    try:
        # Fail closed if receipt logging is unavailable BEFORE any dispatch.
        audit.emit("http_received", http_request_id=observation.request_id, http_method=method)
        return await dispatch(scope, receive, traced_send)
    except BaseException as exc:
        _record_after_dispatch(audit, observation, "http_dispatch_exception",
                               exception_type=type(exc).__name__)
        raise
    finally:
        try:
            _record_after_dispatch(
                audit, observation, "http_finished", status=observation.status,
                gate_status=observation.gate_status,
                response_complete=observation.response_complete,
                completion_scope="asgi_send_returned_not_client_acknowledgement",
                **_audit_summary(observation),
                duration_ms=round((time.monotonic() - started) * 1000, 3))
        finally:
            HTTP_OBSERVATION.reset(token)
