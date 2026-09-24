"""Bounded HTTP transport gate shared by MCP and owner OAuth routes."""

from __future__ import annotations

import asyncio
import collections
import hmac
import json
import re
import threading
import time
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from .http_validation import bearer_credential


_LATEST_MCP_INITIALIZE = None
_LATEST_MCP_INITIALIZE_LOCK = threading.Lock()


def _record_mcp_initialize(body, auth_info):
    """Capture a sanitized initialize snapshot without changing transport semantics."""
    try:
        payload = json.loads(body)
    except (TypeError, ValueError, UnicodeDecodeError):
        return
    if not isinstance(payload, dict) or payload.get("method") != "initialize":
        return
    params = payload.get("params")
    if not isinstance(params, dict):
        return
    caps = params.get("capabilities")
    if not isinstance(caps, dict):
        return
    info = params.get("clientInfo")
    if not isinstance(info, dict):
        info = {}
    sanitized_caps = {
        key: caps[key]
        for key in ("sampling", "elicitation", "roots", "tasks")
        if key in caps
    }
    observation = {
        "observed": True,
        "source": "authenticated_http_initialize",
        "observed_at_unix": time.time(),
        "protocol_version": params.get("protocolVersion"),
        "client_info": {
            key: info.get(key) for key in ("name", "version", "title") if info.get(key) is not None
        },
        "auth_client_id": getattr(auth_info, "client_id", None),
        "capabilities": sanitized_caps,
    }
    with _LATEST_MCP_INITIALIZE_LOCK:
        global _LATEST_MCP_INITIALIZE
        _LATEST_MCP_INITIALIZE = observation


def latest_mcp_initialize():
    with _LATEST_MCP_INITIALIZE_LOCK:
        if _LATEST_MCP_INITIALIZE is None:
            return None
        return json.loads(json.dumps(_LATEST_MCP_INITIALIZE))


class OwnerHTTP:
    def __init__(
        self,
        manager,
        token,
        limit=2097152,
        rpm=120,
        *,
        oauth=None,
        hosts=None,
        body_timeout=15,
    ):
        self.manager, self.token, self.limit, self.rpm = manager, token, limit, rpm
        self.audit = getattr(getattr(manager, "app", None), "_ccm_audit", None)
        self.oauth, self.hosts, self.body_timeout = (
            oauth,
            {host.lower() for host in (hosts or ())},
            body_timeout,
        )
        self.requests = collections.deque()
        self.public_requests = collections.deque()
        self.auth_failures = collections.deque()
        self.consent_requests = collections.deque()

    @staticmethod
    def rate(queue, limit):
        now = time.monotonic()
        while queue and queue[0] <= now - 60:
            queue.popleft()
        if len(queue) >= limit:
            return False
        queue.append(now)
        return True

    async def error(self, send, status, message):
        from .http_observation import mark_http_gate_rejection
        mark_http_gate_rejection(status)
        payload = json.dumps({"error": message}).encode()
        headers = [
            (b"content-type", b"application/json"),
            (b"cache-control", b"no-store"),
            (b"content-length", str(len(payload)).encode()),
        ]
        if status == 401:
            challenge = (
                self.oauth.challenge
                if self.oauth
                else 'Bearer realm="Codex-Control-MCP"'
            )
            headers.append((b"www-authenticate", challenge.encode("ascii")))
        if status == 429:
            headers.append((b"retry-after", b"60"))
        await send(
            {"type": "http.response.start", "status": status, "headers": headers}
        )
        await send({"type": "http.response.body", "body": payload})

    async def __call__(self, scope, receive, send):
        from .http_observation import observe_http
        return await observe_http(self.audit, self._dispatch, scope, receive, send)

    async def _dispatch(self, scope, receive, send):
        if scope["type"] != "http":
            return
        path = scope.get("path")
        public = bool(self.oauth and path in self.oauth.paths)
        if path != "/mcp" and not public:
            return await self.error(send, 404, "Not found")
        headers = {}
        for key, value in scope.get("headers", []):
            key = key.lower()
            if key in headers and key in {
                b"authorization",
                b"host",
                b"origin",
                b"content-length",
                b"content-type",
                b"transfer-encoding",
                b"mcp-session-id",
            }:
                return await self.error(send, 400, "Ambiguous request headers")
            headers[key] = value
        if (
            self.hosts
            and headers.get(b"host", b"").decode("latin1").lower() not in self.hosts
        ):
            return await self.error(send, 421, "Untrusted Host")
        if public:
            origin = headers.get(b"origin")
            if origin and origin.decode("latin1") != self.oauth.origin:
                return await self.error(send, 403, "Untrusted OAuth Origin")
            if not self.rate(self.public_requests, 240):
                return await self.error(send, 429, "OAuth request limit exceeded")
            if scope.get("method") == "POST" and path in {
                "/consent",
                "/register",
                "/authorize",
            }:
                if not self.rate(self.consent_requests, 20):
                    return await self.error(
                        send, 429, "Owner enrollment request limit exceeded"
                    )
        else:
            auth = bearer_credential(headers.get(b"authorization", b""))
            local_owner = bool(auth) and hmac.compare_digest(
                auth, self.token.encode("ascii")
            )
            auth_info = AccessToken(
                token=auth.decode("ascii"), client_id="codex-control-local-owner",
                scopes=["control"], subject="windows_owner",
                claims={"iss": "urn:codex-control-mcp:local-owner"},
            ) if local_owner else None
            if auth_info is None and self.oauth and auth:
                try:
                    auth_info = await self.oauth.verify_token(auth.decode("ascii"))
                except UnicodeError:
                    auth_info = None
            if auth_info is None:
                if not self.rate(self.auth_failures, 120):
                    return await self.error(
                        send, 429, "Authentication attempt limit exceeded"
                    )
                return await self.error(send, 401, "Authentication required")
            if not self.rate(self.requests, self.rpm):
                return await self.error(send, 429, "Owner request limit exceeded")
            # The official HTTP manager binds stateful sessions to this
            # authenticated principal, including OAuth issuer and subject.
            scope = dict(scope, user=AuthenticatedUser(auth_info))
        maximum = min(self.limit, 65536) if public else self.limit
        if b"transfer-encoding" in headers and (
            b"content-length" in headers
            or headers[b"transfer-encoding"].lower() != b"chunked"
        ):
            return await self.error(send, 400, "Ambiguous body framing")
        raw_length = headers.get(b"content-length", b"0")
        if not re.fullmatch(rb"[0-9]{1,20}", raw_length):
            return await self.error(send, 400, "Invalid Content-Length")
        length = int(raw_length)
        if length > maximum:
            return await self.error(send, 413, "Request too large")
        endpoint = self.oauth if public else self.manager.handle_request
        if (
            scope.get("method") != "POST"
            and not length
            and b"transfer-encoding" not in headers
        ):
            return await endpoint(scope, receive, send)
        body = bytearray()
        total = 0
        deadline = time.monotonic() + self.body_timeout
        while True:
            try:
                msg = await asyncio.wait_for(
                    receive(), timeout=max(0, deadline - time.monotonic())
                )
            except TimeoutError:
                return await self.error(send, 408, "Request body timeout")
            if msg["type"] == "http.disconnect":
                return
            if msg["type"] != "http.request":
                return await self.error(send, 400, "Invalid request body event")
            chunk = msg.get("body", b"")
            total += len(chunk)
            if total > maximum:
                return await self.error(send, 413, "Request too large")
            if chunk:
                body.extend(chunk)
            if not msg.get("more_body", False):
                break
        if b"content-length" in headers and total != length:
            return await self.error(send, 400, "Content-Length mismatch")
        body = bytes(body)
        if not public and scope.get("method") == "POST":
            _record_mcp_initialize(body, auth_info)
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        return await endpoint(scope, bounded_receive, send)
