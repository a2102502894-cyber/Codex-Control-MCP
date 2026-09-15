"""Owner-authorized OAuth using the MCP SDK's protocol handlers.

Enrollment is closed until the owner explicitly runs `connect --copy` locally.
The one-time pairing secret is never printed or embedded in a URL. Client and
credential records are DPAPI encrypted; opaque token lookup keys are SHA-256.
This authenticates one Windows owner, not a multi-tenant execution service.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import secrets
import sqlite3
import threading
import time
import uuid
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route, Router
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.server.auth.routes import create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.client_auth import (
    ClientAuthenticator,
    AuthenticationError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from .auth import _crypt
from .errors import BridgeError
from .http_validation import ascii_equal, validate_oauth_request

SCOPES = ["control", "offline_access"]
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    "X-Frame-Options": "DENY",
}


def valid_issuer(value: str) -> str:
    if not isinstance(value, str):
        raise BridgeError("invalid_config", "OAuth issuer must be an HTTPS origin")
    u = urlsplit(value)
    if (
        u.scheme != "https"
        or not u.hostname
        or u.username
        or u.password
        or u.query
        or u.fragment
        or u.path not in ("", "/")
        or u.port not in (None, 443)
    ):
        raise BridgeError(
            "invalid_config",
            "OAuth issuer must be an HTTPS origin without credentials, paths or query",
        )
    return str(AnyHttpUrl(value.rstrip("/") + "/"))


def valid_redirect(value: str) -> bool:
    try:
        u = urlsplit(value)
        return bool(
            u.scheme == "https"
            and u.hostname == "chatgpt.com"
            and not u.username
            and not u.password
            and u.port in (None, 443)
            and not u.query
            and not u.fragment
            and (
                u.path == "/connector_platform_oauth_redirect"
                or re.fullmatch(r"/connector/oauth/[A-Za-z0-9_-]{8,200}", u.path)
            )
        )
    except ValueError:
        return False


class _OAuthTransaction:
    """Class-based context: SDK frozen exceptions cannot accept __traceback__."""

    def __init__(self, store):
        self.store = store

    def __enter__(self):
        store = self.store
        store.lock.acquire()
        self.level = store.depth
        self.savepoint = f"oauth_{self.level}"
        try:
            store.db.execute(
                f"SAVEPOINT {self.savepoint}" if self.level else "BEGIN IMMEDIATE"
            )
        except BaseException:
            store.lock.release()
            raise
        store.depth += 1
        return store

    def rollback(self):
        if self.level:
            self.store.db.execute(f"ROLLBACK TO SAVEPOINT {self.savepoint}")
            self.store.db.execute(f"RELEASE SAVEPOINT {self.savepoint}")
        else:
            self.store.db.rollback()

    def __exit__(self, exc_type, exc, tb):
        store = self.store
        try:
            if exc_type is not None:
                self.rollback()
            else:
                try:
                    if self.level:
                        store.db.execute(f"RELEASE SAVEPOINT {self.savepoint}")
                    else:
                        store.db.commit()
                except BaseException:
                    self.rollback()
                    raise
        finally:
            store.depth -= 1
            store.lock.release()
        return False


class OAuthStore:
    def __init__(self, home):
        (home / "state").mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.depth = 0
        self.db = sqlite3.connect(
            home / "state/oauth.sqlite3", timeout=10, check_same_thread=False
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS entries(kind TEXT,key TEXT,payload BLOB,expires REAL,family TEXT,PRIMARY KEY(kind,key))"
        )
        self.db.commit()

    @staticmethod
    def key(value):
        return hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()

    def transaction(self):
        """Serialize a complete grant transition, including across processes.

        The lock is held through synchronous database work only. Nested store
        operations use savepoints and cannot commit their caller's transaction.
        """
        return _OAuthTransaction(self)

    def put(self, kind, key, value, expires, family=""):
        payload = _crypt(json.dumps(value, ensure_ascii=True).encode("utf-8"))
        with self.transaction():
            self.db.execute("DELETE FROM entries WHERE expires<?", (time.time(),))
            count = self.db.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            replacing = self.db.execute(
                "SELECT 1 FROM entries WHERE kind=? AND key=?", (kind, self.key(key))
            ).fetchone()
            if count >= 4096 and not replacing:
                raise BridgeError(
                    "resource_limit",
                    "OAuth record limit reached; revoke unused enrollments locally",
                )
            self.db.execute(
                "INSERT OR REPLACE INTO entries VALUES(?,?,?,?,?)",
                (kind, self.key(key), payload, expires, family),
            )

    def get(self, kind, key, consume=False):
        with self.transaction() if consume else self.lock:
            row = self.db.execute(
                "SELECT payload,expires FROM entries WHERE kind=? AND key=?",
                (kind, self.key(key)),
            ).fetchone()
            if not row or row[1] <= time.time():
                return None
            value = json.loads(_crypt(row[0], True))
            if consume:
                changed = self.db.execute(
                    "DELETE FROM entries WHERE kind=? AND key=?", (kind, self.key(key))
                ).rowcount
                if changed != 1:
                    return None
            return value

    def remove_kinds(self, *kinds):
        with self.transaction():
            for kind in kinds:
                self.db.execute("DELETE FROM entries WHERE kind=?", (kind,))

    def revoke_family(self, family):
        with self.transaction():
            self.db.execute(
                "DELETE FROM entries WHERE family=? AND kind IN ('access','refresh')",
                (family,),
            )

    def count(self, kind):
        with self.lock:
            return self.db.execute(
                "SELECT COUNT(*) FROM entries WHERE kind=? AND expires>?",
                (kind, time.time()),
            ).fetchone()[0]

    def close(self):
        with self.lock:
            self.db.close()


def create_pairing_secret(home):
    store = OAuthStore(home)
    try:
        value = secrets.token_urlsafe(24)
        expiry = time.time() + 300
        with store.transaction():
            store.remove_kinds("pairing", "enrollment")
            store.put("pairing", value, {"owner_issued": True}, expiry)
            store.put("enrollment", "active", {"opened_at": time.time()}, expiry)
        return value
    finally:
        store.close()


class OwnerClientAuthenticator(ClientAuthenticator):
    async def authenticate_request(self, request):
        client = await super().authenticate_request(request)
        form = await request.form()
        has_basic = bool(request.headers.get("authorization"))
        if has_basic != (client.token_endpoint_auth_method == "client_secret_basic"):
            raise AuthenticationError(
                "Authentication method does not match client registration"
            )
        if client.token_endpoint_auth_method == "none" and form.get("client_secret"):
            raise AuthenticationError(
                "Public client does not use client_secret authentication"
            )
        return client


class OwnerOAuth:
    def __init__(self, cfg):
        self.issuer = valid_issuer(cfg.oauth["issuer"])
        self.origin = self.issuer.rstrip("/")
        self.resource = self.issuer + "mcp"
        self.store = OAuthStore(cfg.home)
        self.challenge = (
            'Bearer resource_metadata="'
            + self.issuer
            + '.well-known/oauth-protected-resource/mcp", scope="control"'
        )
        self.token_handler = TokenHandler(self, OwnerClientAuthenticator(self))
        routes = create_auth_routes(
            self,
            AnyHttpUrl(self.issuer),
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=SCOPES, default_scopes=SCOPES
            ),
            revocation_options=RevocationOptions(enabled=True),
        )
        routes = [
            r
            for r in routes
            if r.path
            not in ("/.well-known/oauth-authorization-server", "/token", "/revoke")
        ]
        routes += create_protected_resource_routes(
            AnyHttpUrl(self.resource),
            [AnyHttpUrl(self.issuer)],
            SCOPES,
            "Codex-Control-MCP owner computer",
        )
        routes += [
            Route(
                "/.well-known/oauth-authorization-server",
                self.metadata,
                methods=["GET"],
            ),
            Route(
                "/.well-known/oauth-protected-resource",
                self.resource_metadata,
                methods=["GET"],
            ),
            Route("/token", self.token_endpoint, methods=["POST"]),
            Route("/revoke", self.revoke_endpoint, methods=["POST"]),
            Route("/consent", self.consent, methods=["GET", "POST"]),
        ]
        self.paths = {r.path for r in routes}
        self.router = Router(routes=routes)

    async def metadata(self, request):
        return JSONResponse(
            {
                "issuer": self.issuer,
                "authorization_endpoint": self.issuer + "authorize",
                "token_endpoint": self.issuer + "token",
                "registration_endpoint": self.issuer + "register",
                "revocation_endpoint": self.issuer + "revoke",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_methods_supported": [
                    "none",
                    "client_secret_post",
                    "client_secret_basic",
                ],
                "revocation_endpoint_auth_methods_supported": [
                    "none",
                    "client_secret_post",
                    "client_secret_basic",
                ],
                "code_challenge_methods_supported": ["S256"],
                "authorization_response_iss_parameter_supported": True,
                "scopes_supported": SCOPES,
            },
            headers=SECURITY_HEADERS,
        )

    async def resource_metadata(self, request):
        return JSONResponse(
            {
                "resource": self.resource,
                "authorization_servers": [self.issuer],
                "scopes_supported": SCOPES,
                "bearer_methods_supported": ["header"],
            },
            headers=SECURITY_HEADERS,
        )

    async def get_client(self, client_id):
        data = self.store.get("client", client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info):
        with self.store.transaction():
            if not self.store.get("enrollment", "active"):
                raise RegistrationError(
                    "invalid_client_metadata",
                    "Owner enrollment is closed. Run connect --copy locally before creating the ChatGPT connector.",
                )
            if self.store.count("client") >= 64:
                raise RegistrationError(
                    "invalid_client_metadata",
                    "Registered client limit reached; manage clients locally.",
                )
            if not client_info.redirect_uris or not all(
                valid_redirect(str(u)) for u in client_info.redirect_uris
            ):
                raise RegistrationError(
                    "invalid_redirect_uri",
                    "Only exact ChatGPT HTTPS OAuth callbacks are supported.",
                )
            if client_info.token_endpoint_auth_method not in (
                "none",
                "client_secret_post",
                "client_secret_basic",
            ):
                raise RegistrationError(
                    "invalid_client_metadata",
                    "Unsupported client authentication method.",
                )
            self.store.put(
                "client",
                client_info.client_id,
                client_info.model_dump(mode="json"),
                time.time() + 366 * 86400,
            )

    async def authorize(self, client, params: AuthorizationParams):
        with self.store.transaction():
            scopes = params.scopes or SCOPES
            if params.resource != self.resource:
                raise AuthorizeError(
                    "invalid_request",
                    "The resource must exactly match the published MCP resource.",
                )
            if "control" not in scopes or not set(scopes).issubset(SCOPES):
                raise AuthorizeError(
                    "invalid_scope", "Explicit control scope is required."
                )
            if not valid_redirect(str(params.redirect_uri)) or str(
                params.redirect_uri
            ) not in [str(u) for u in client.redirect_uris]:
                raise AuthorizeError("invalid_request", "Unregistered redirect URI.")
            if not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.code_challenge):
                raise AuthorizeError("invalid_request", "PKCE S256 is required.")
            if self.store.count("pending") >= 32:
                raise AuthorizeError(
                    "temporarily_unavailable",
                    "Too many pending authorization requests.",
                )
            rid = secrets.token_urlsafe(32)
            csrf = secrets.token_urlsafe(32)
            self.store.put(
                "pending",
                rid,
                {
                    "client_id": client.client_id,
                    "client_name": client.client_name,
                    "params": params.model_dump(mode="json"),
                    "csrf": csrf,
                },
                time.time() + 600,
            )
            return self.issuer + "consent?" + urlencode({"request": rid})

    def redirect(self, pending, **values):
        params = pending["params"]
        u = urlsplit(params["redirect_uri"])
        values["iss"] = self.issuer
        if params.get("state") is not None:
            values["state"] = params["state"]
        return urlunsplit((u.scheme, u.netloc, u.path, urlencode(values), ""))

    async def consent(self, request: Request):
        rid = request.query_params.get("request", "")
        pending = self.store.get("pending", rid) if 20 <= len(rid) <= 100 else None
        if not pending:
            return JSONResponse(
                {
                    "error": "invalid_request",
                    "message": "Authorization request expired or already used.",
                },
                status_code=400,
                headers=SECURITY_HEADERS,
            )
        if request.method == "GET":
            fields = {
                k: html.escape(str(v), quote=True)
                for k, v in {
                    "rid": rid,
                    "csrf": pending["csrf"],
                    "client": pending.get("client_name") or "Unnamed client",
                    "client_id": pending["client_id"],
                    "redirect": pending["params"]["redirect_uri"],
                }.items()
            }
            body = (
                '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>Codex-Control-MCP 授权</title>'
                "<h1>授权访问这台电脑</h1><p>此连接可以执行命令、读取、修改和删除文件，权限与本桥接进程相同，没有工作区沙箱。</p>"
                "<p>只批准你刚刚在 ChatGPT 中创建的连接。客户端自报名称不是身份认证。</p>"
                f"<p>客户端：{fields['client']}<br>标识：{fields['client_id']}<br>回调：{fields['redirect']}</p>"
                "<p>在本机运行 <code>Codex-Control-MCP.exe connect --copy</code>，将一次性配对码粘贴到下方。配对码有效期五分钟，用后失效，不是 OpenAI API Key。</p>"
                f'<form method="post" action="/consent?request={fields["rid"]}"><input type="hidden" name="csrf" value="{fields["csrf"]}">'
                '<label>一次性配对码 <input name="pairing_code" type="password" autocomplete="off" maxlength="128"></label>'
                '<button name="decision" value="approve" type="submit">授权完整访问</button> '
                '<button name="decision" value="deny" type="submit">拒绝</button></form></html>'
            )
            # Fetch serializes Origin as null for a navigation POST under
            # no-referrer. Preserve same-origin form submission while still
            # suppressing referrers on cross-origin navigation. Redirects and
            # other OAuth responses keep the stricter no-referrer policy.
            # Chromium also applies form-action to redirects after POST. Allow
            # the exact callback already validated at client registration and
            # authorization, never an arbitrary cross-origin form destination.
            callback = pending["params"]["redirect_uri"]
            if not valid_redirect(callback):
                return JSONResponse(
                    {"error": "invalid_request"},
                    status_code=400,
                    headers=SECURITY_HEADERS,
                )
            policy = SECURITY_HEADERS["Content-Security-Policy"].replace(
                "form-action 'self'", "form-action 'self' " + callback
            )
            response = HTMLResponse(
                body,
                headers={
                    **SECURITY_HEADERS,
                    "Referrer-Policy": "same-origin",
                    "Content-Security-Policy": policy,
                },
            )
            response.set_cookie(
                "ccm_consent",
                pending["csrf"],
                max_age=600,
                path="/consent",
                secure=True,
                httponly=True,
                samesite="lax",
            )
            return response
        form = await request.form()
        csrf = str(form.get("csrf", ""))
        if (
            request.headers.get("origin") != self.origin
            or not ascii_equal(csrf, pending["csrf"])
            or not ascii_equal(request.cookies.get("ccm_consent", ""), pending["csrf"])
        ):
            return JSONResponse(
                {
                    "error": "access_denied",
                    "message": "Consent origin or CSRF validation failed.",
                },
                status_code=403,
                headers=SECURITY_HEADERS,
            )
        with self.store.transaction():
            current = self.store.get("pending", rid)
            if not current or not self.store.get("client", current["client_id"]):
                return JSONResponse(
                    {"error": "invalid_request"},
                    status_code=400,
                    headers=SECURITY_HEADERS,
                )
            if form.get("decision") == "deny":
                self.store.get("pending", rid, consume=True)
                return RedirectResponse(
                    self.redirect(pending, error="access_denied"),
                    status_code=303,
                    headers=SECURITY_HEADERS,
                )
            secret = str(form.get("pairing_code", "")).strip()
            if (
                form.get("decision") != "approve"
                or not 20 <= len(secret) <= 128
                or not self.store.get("pairing", secret, consume=True)
            ):
                return JSONResponse(
                    {
                        "error": "access_denied",
                        "message": "A valid owner-issued one-time pairing code and explicit approval are required.",
                    },
                    status_code=403,
                    headers=SECURITY_HEADERS,
                )
            accepted = self.store.get("pending", rid, consume=True)
            if not accepted:
                return JSONResponse(
                    {"error": "invalid_request"},
                    status_code=400,
                    headers=SECURITY_HEADERS,
                )
            self.store.remove_kinds("enrollment")
            p = AuthorizationParams.model_validate(accepted["params"])
            code = secrets.token_urlsafe(32)
            grant = AuthorizationCode(
                code=code,
                client_id=accepted["client_id"],
                scopes=p.scopes or SCOPES,
                expires_at=time.time() + 120,
                code_challenge=p.code_challenge,
                redirect_uri=p.redirect_uri,
                redirect_uri_provided_explicitly=p.redirect_uri_provided_explicitly,
                resource=self.resource,
                subject="windows_owner",
            )
            self.store.put(
                "code", code, grant.model_dump(mode="json"), grant.expires_at
            )
        response = RedirectResponse(
            self.redirect(accepted, code=code),
            status_code=303,
            headers=SECURITY_HEADERS,
        )
        response.delete_cookie(
            "ccm_consent", path="/consent", secure=True, httponly=True, samesite="lax"
        )
        return response

    async def token_endpoint(self, request):
        form = await request.form()
        if form.get("resource") != self.resource:
            return JSONResponse(
                {
                    "error": "invalid_target",
                    "error_description": "Exact MCP resource binding is required.",
                },
                status_code=400,
                headers=SECURITY_HEADERS,
            )
        if form.get("grant_type") == "authorization_code" and not re.fullmatch(
            r"[A-Za-z0-9._~-]{43,128}", str(form.get("code_verifier", ""))
        ):
            return JSONResponse(
                {"error": "invalid_grant"}, status_code=400, headers=SECURITY_HEADERS
            )
        return await self.token_handler.handle(request)

    async def load_authorization_code(self, client, authorization_code):
        d = self.store.get("code", authorization_code)
        return (
            AuthorizationCode.model_validate(d)
            if d and d["client_id"] == client.client_id
            else None
        )

    async def revoke_endpoint(self, request):
        # SDK 1.30.0's revocation request model requires a client_secret even for
        # public clients. Reuse its authenticator, but apply RFC 7009 to both.
        try:
            client = await OwnerClientAuthenticator(self).authenticate_request(request)
        except AuthenticationError:
            return JSONResponse(
                {"error": "invalid_client"}, status_code=401, headers=SECURITY_HEADERS
            )
        form = await request.form()
        token = str(form.get("token", ""))
        if not token or len(token) > 512:
            return JSONResponse(
                {"error": "invalid_request"}, status_code=400, headers=SECURITY_HEADERS
            )
        with self.store.transaction():
            d = self.store.get("access", token) or (
                self.store.get("refresh", token)
                or self.store.get("used_refresh", token)
            )
            if d and d["client_id"] == client.client_id:
                family = d.get("family") or d.get("claims", {}).get("family")
                if family:
                    self.store.revoke_family(family)
        # Unknown or already revoked tokens are intentionally indistinguishable.
        return JSONResponse({}, headers=SECURITY_HEADERS)

    def issue_tokens(self, client_id, scopes, family=None, deadline=None):
        with self.store.transaction():
            now = int(time.time())
            family = family or uuid.uuid4().hex
            deadline = deadline or now + 30 * 86400
            access = secrets.token_urlsafe(40)
            refresh = secrets.token_urlsafe(40)
            at = AccessToken(
                token=access,
                client_id=client_id,
                scopes=scopes,
                expires_at=min(now + 3600, deadline),
                resource=self.resource,
                subject="windows_owner",
                claims={"iss": self.issuer, "family": family},
            )
            rt = RefreshToken(
                token=refresh,
                client_id=client_id,
                scopes=scopes,
                expires_at=deadline,
                resource=self.resource,
                subject="windows_owner",
            )
            self.store.put(
                "access", access, at.model_dump(mode="json"), at.expires_at, family
            )
            if "offline_access" in scopes:
                self.store.put(
                    "refresh",
                    refresh,
                    {**rt.model_dump(mode="json"), "family": family},
                    deadline,
                    family,
                )
            return OAuthToken(
                access_token=access,
                token_type="Bearer",
                expires_in=at.expires_at - now,
                refresh_token=refresh if "offline_access" in scopes else None,
                scope=" ".join(scopes),
            )

    async def exchange_authorization_code(self, client, authorization_code):
        with self.store.transaction():
            d = self.store.get("code", authorization_code.code)
            if (
                not d
                or d["client_id"] != client.client_id
                or d["resource"] != self.resource
                or not self.store.get("client", client.client_id)
            ):
                raise TokenError(
                    "invalid_grant", "Code expired, used or bound to another client."
                )
            self.store.get("code", authorization_code.code, consume=True)
            return self.issue_tokens(client.client_id, d["scopes"])

    async def load_refresh_token(self, client, refresh_token):
        with self.store.transaction():
            if not self.store.get("client", client.client_id):
                return None
            used = self.store.get("used_refresh", refresh_token)
            if used and used["client_id"] == client.client_id:
                self.store.revoke_family(used["family"])
                return None
            d = self.store.get("refresh", refresh_token)
            return (
                RefreshToken.model_validate(d)
                if d and d["client_id"] == client.client_id
                else None
            )

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        with self.store.transaction():
            if not self.store.get("client", client.client_id):
                raise TokenError(
                    "invalid_grant", "Client registration is no longer active."
                )
            used = self.store.get("used_refresh", refresh_token.token)
            if used and used["client_id"] == client.client_id:
                # Commit replay-triggered revocation before returning invalid_grant.
                self.store.revoke_family(used["family"])
            else:
                d = self.store.get("refresh", refresh_token.token)
                if (
                    not d
                    or d["client_id"] != client.client_id
                    or d["resource"] != self.resource
                    or not set(scopes).issubset(d["scopes"])
                ):
                    raise TokenError(
                        "invalid_grant",
                        "Refresh token expired, used or has incompatible scope.",
                    )
                self.store.get("refresh", refresh_token.token, consume=True)
                self.store.revoke_family(d["family"])
                self.store.put(
                    "used_refresh",
                    refresh_token.token,
                    {"client_id": client.client_id, "family": d["family"]},
                    d["expires_at"],
                    d["family"],
                )
                return self.issue_tokens(
                    client.client_id, scopes, d["family"], d["expires_at"]
                )
        raise TokenError("invalid_grant", "Refresh token has already been used.")

    async def load_access_token(self, token):
        d = self.store.get("access", token)
        if (
            not d
            or d.get("resource") != self.resource
            or "control" not in d.get("scopes", [])
            or d.get("claims", {}).get("iss") != self.issuer
        ):
            return None
        return AccessToken.model_validate(d)

    async def verify_token(self, token):
        return await self.load_access_token(token)

    async def revoke_token(self, token):
        with self.store.transaction():
            d = (
                self.store.get("access", token.token)
                or self.store.get("refresh", token.token)
                or self.store.get("used_refresh", token.token)
            )
            if d and d["client_id"] == token.client_id:
                family = d.get("family") or d.get("claims", {}).get("family")
                if family:
                    self.store.revoke_family(family)

    async def __call__(self, scope, receive, send):
        async def secure_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                # SDK authorization errors also carry RFC 9207 issuer identification.
                for i, (key, value) in enumerate(headers):
                    if key.lower() == b"location" and scope.get("path") == "/authorize":
                        u = urlsplit(value.decode("latin1"))
                        target = urlunsplit((u.scheme, u.netloc, u.path, "", ""))
                        if valid_redirect(target):
                            q = dict(parse_qsl(u.query, keep_blank_values=True))
                            q["iss"] = self.issuer
                            headers[i] = (
                                key,
                                urlunsplit(
                                    (u.scheme, u.netloc, u.path, urlencode(q), "")
                                ).encode("latin1"),
                            )
                present = {k.lower() for k, _ in headers}
                headers += [
                    (k.lower().encode(), v.encode())
                    for k, v in SECURITY_HEADERS.items()
                    if k.lower().encode() not in present
                ]
                message = {**message, "headers": headers}
            await send(message)

        try:
            receive = await validate_oauth_request(scope, receive)
            await self.router(scope, receive, secure_send)
        except (ValueError, UnicodeError, RecursionError):
            response = JSONResponse(
                {"error": "invalid_request"}, status_code=400, headers=SECURITY_HEADERS
            )
            await response(scope, receive, send)

    def close(self):
        self.store.close()
