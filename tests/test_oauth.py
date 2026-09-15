"""Real SDK OAuth protocol and DPAPI tests in isolated in-memory HTTP transport.
These tests do not authorize the user's ChatGPT account or use real credentials.
"""

import asyncio
import base64
import hashlib
import html
import re
import secrets
from urllib.parse import urlsplit, parse_qs

import httpx
import pytest
from codex_control_mcp.config import Config
from codex_control_mcp.errors import BridgeError
from codex_control_mcp.http_guard import OwnerHTTP
from codex_control_mcp.oauth import (
    OwnerOAuth,
    create_pairing_secret,
    valid_redirect,
    valid_issuer,
)

ISSUER = "https://owner.test/"
CALLBACK = "https://chatgpt.com/connector_platform_oauth_redirect"


class ProtocolSink:
    """Transport authentication sink only, not an official execution backend."""

    async def handle_request(self, scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send(
            {"type": "http.response.body", "body": b'{"authenticated_transport":true}'}
        )


@pytest.fixture
def oauth_case(tmp_path):
    cfg = Config(
        home=tmp_path, cwd=str(tmp_path), oauth={"enabled": True, "issuer": ISSUER}
    )
    cfg.initialize_storage()
    provider = OwnerOAuth(cfg)
    owner = secrets.token_urlsafe(48)
    app = OwnerHTTP(ProtocolSink(), owner, oauth=provider, hosts=["owner.test"])
    yield cfg, provider, app, owner
    provider.close()


def client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ISSUER, follow_redirects=False
    )


async def register(http, cfg, callback=CALLBACK):
    pairing = create_pairing_secret(cfg.home)
    result = await http.post(
        "/register",
        json={
            "redirect_uris": [callback],
            "client_name": "Isolated protocol test",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": "control offline_access",
        },
    )
    assert result.status_code == 201, result.text
    return result.json(), pairing


async def begin(http, cfg, provider, **overrides):
    registered, pairing = await register(http, cfg)
    verifier = secrets.token_urlsafe(48)
    params = {
        "client_id": registered["client_id"],
        "response_type": "code",
        "redirect_uri": CALLBACK,
        "code_challenge": base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        )
        .decode()
        .rstrip("="),
        "code_challenge_method": "S256",
        "scope": "control offline_access",
        "state": secrets.token_urlsafe(16),
        "resource": provider.resource,
    }
    params.update(overrides)
    response = await http.get("/authorize", params=params)
    return registered, pairing, verifier, params, response


async def consent(http, initial, pairing, **overrides):
    assert initial.status_code in (302, 303, 307), initial.text
    page = await http.get(initial.headers["location"])
    assert page.status_code == 200, page.text
    csrf = html.unescape(re.search(r'name="csrf" value="([^"]+)"', page.text).group(1))
    data = {"csrf": csrf, "pairing_code": pairing, "decision": "approve"}
    data.update(overrides)
    response = await http.post(
        initial.headers["location"], data=data, headers={"Origin": ISSUER.rstrip("/")}
    )
    return response, page


async def grant(http, cfg, provider):
    registered, pairing, verifier, params, initial = await begin(http, cfg, provider)
    accepted, page = await consent(http, initial, pairing)
    assert accepted.status_code == 303, accepted.text
    query = parse_qs(urlsplit(accepted.headers["location"]).query)
    assert query["iss"] == [ISSUER] and query["state"] == [params["state"]]
    request = {
        "grant_type": "authorization_code",
        "client_id": registered["client_id"],
        "code": query["code"][0],
        "redirect_uri": CALLBACK,
        "code_verifier": verifier,
        "resource": provider.resource,
    }
    response = await http.post("/token", data=request)
    assert response.status_code == 200, response.text
    return registered, response.json(), request, accepted, pairing


def test_browser_form_retains_origin_without_cross_origin_referrer(oauth_case):
    """A real HTML form POST must retain its Origin for CSRF validation.

    Fetch Standard, 'append a request Origin header': no-referrer changes a
    navigation POST's Origin to null. That broke the actual ChatGPT consent UI.
    The consent page must suppress cross-origin referrers without doing that.
    """
    cfg, provider, app, _ = oauth_case

    async def run():
        async with client(app) as c:
            _, pairing, _, _, initial = await begin(c, cfg, provider)
            location = initial.headers["location"]
            page = await c.get(location)
            assert page.headers["referrer-policy"] == "same-origin"
            form_policy = next(
                part.strip()
                for part in page.headers["content-security-policy"].split(";")
                if part.strip().startswith("form-action ")
            )
            # Chromium also checks the final form redirect. Only the exact
            # registered callback may leave the owner origin.
            assert form_policy == "form-action 'self' " + CALLBACK
            csrf = html.unescape(
                re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
            )
            form = {"csrf": csrf, "pairing_code": pairing, "decision": "approve"}
            for origin in ("null", "https://attacker.test"):
                denied = await c.post(location, data=form, headers={"Origin": origin})
                assert denied.status_code == 403
            accepted = await c.post(
                location, data=form, headers={"Origin": ISSUER.rstrip("/")}
            )
            assert accepted.status_code == 303
            assert accepted.headers["referrer-policy"] == "no-referrer"

    asyncio.run(run())


def test_metadata_challenge_and_closed_enrollment(oauth_case):
    cfg, provider, app, owner = oauth_case

    async def run():
        async with client(app) as c:
            r = await c.get("/mcp")
            assert (
                r.status_code == 401
                and "oauth-protected-resource/mcp" in r.headers["www-authenticate"]
            )
            for path in [
                "/.well-known/oauth-protected-resource",
                "/.well-known/oauth-protected-resource/mcp",
            ]:
                r = await c.get(path)
                assert (
                    r.status_code == 200 and r.json()["resource"] == provider.resource
                )
            meta = (await c.get("/.well-known/oauth-authorization-server")).json()
            assert (
                meta["issuer"] == ISSUER
                and meta["authorization_response_iss_parameter_supported"] is True
            )
            assert meta["code_challenge_methods_supported"] == ["S256"]
            r = await c.post(
                "/register",
                json={
                    "redirect_uris": [CALLBACK],
                    "grant_types": ["authorization_code", "refresh_token"],
                },
            )
            assert r.status_code == 400 and "closed" in r.text
            r = await c.get("/mcp", headers={"Authorization": "Bearer " + owner})
            assert r.status_code == 200

    asyncio.run(run())


@pytest.mark.parametrize(
    "value",
    [
        "http://chatgpt.com/connector_platform_oauth_redirect",
        "https://chatgpt.com.attacker.test/connector_platform_oauth_redirect",
        "https://attacker.test@chatgpt.com/connector_platform_oauth_redirect",
        "https://chatgpt.com/connector_platform_oauth_redirect?next=https://attacker.test",
        "https://chatgpt.com/connector/oauth/../../attacker",
        "https://chatgpt.com:444/connector_platform_oauth_redirect",
    ],
)
def test_redirect_restrictions(value):
    assert not valid_redirect(value)


@pytest.mark.parametrize(
    "value",
    [
        "http://owner.test",
        "https://user:pw@owner.test",
        "https://owner.test/path",
        "https://owner.test/?token=x",
    ],
)
def test_issuer_restrictions(value):
    with pytest.raises(BridgeError):
        valid_issuer(value)


def test_sdk_flow_single_use_dpapi_persistence_and_issuer(oauth_case):
    cfg, provider, app, owner = oauth_case

    async def run():
        async with client(app) as c:
            registered, token, request, accepted, pairing = await grant(
                c, cfg, provider
            )
            r = await c.get(
                "/mcp", headers={"Authorization": "Bearer " + token["access_token"]}
            )
            assert r.status_code == 200
            assert not provider.store.get("enrollment", "active")
            assert await provider.get_client(registered["client_id"]) is not None
            duplicate = await c.post("/token", data=request)
            assert (
                duplicate.status_code == 400
                and duplicate.json()["error"] == "invalid_grant"
            )
            other = OwnerOAuth(cfg)
            try:
                assert await other.load_access_token(token["access_token"]) is not None
            finally:
                other.close()
            raw = (cfg.home / "state/oauth.sqlite3").read_bytes()
            for secret in [
                token["access_token"],
                token["refresh_token"],
                pairing,
                request["code"],
            ]:
                assert secret.encode() not in raw

    asyncio.run(run())


def test_wrong_pkce_resource_and_refresh_replay(oauth_case):
    cfg, provider, app, owner = oauth_case

    async def run():
        async with client(app) as c:
            registered, pairing, verifier, params, initial = await begin(
                c, cfg, provider
            )
            accepted, _ = await consent(c, initial, pairing)
            code = parse_qs(urlsplit(accepted.headers["location"]).query)["code"][0]
            request = {
                "grant_type": "authorization_code",
                "client_id": registered["client_id"],
                "code": code,
                "redirect_uri": CALLBACK,
                "code_verifier": verifier,
                "resource": provider.resource,
            }
            assert (
                await c.post(
                    "/token", data={**request, "resource": "https://wrong.test/mcp"}
                )
            ).status_code == 400
            assert (
                await c.post("/token", data={**request, "code_verifier": "A" * 64})
            ).status_code == 400
            correct = await c.post("/token", data=request)
            assert correct.status_code == 200, correct.text
            old = correct.json()
            refresh = {
                "grant_type": "refresh_token",
                "client_id": registered["client_id"],
                "refresh_token": old["refresh_token"],
                "resource": provider.resource,
            }
            assert (
                await c.post("/token", data={**refresh, "scope": "control admin"})
            ).status_code == 400
            rotated = await c.post("/token", data=refresh)
            assert rotated.status_code == 200, rotated.text
            new = rotated.json()
            assert new["refresh_token"] != old["refresh_token"]
            assert (
                await c.get(
                    "/mcp", headers={"Authorization": "Bearer " + old["access_token"]}
                )
            ).status_code == 401
            assert (
                await c.get(
                    "/mcp", headers={"Authorization": "Bearer " + new["access_token"]}
                )
            ).status_code == 200
            assert (await c.post("/token", data=refresh)).status_code == 400
            assert (
                await c.get(
                    "/mcp", headers={"Authorization": "Bearer " + new["access_token"]}
                )
            ).status_code == 401

    asyncio.run(run())


def test_consent_requires_owner_secret_cookie_origin_and_csrf(oauth_case):
    cfg, provider, app, owner = oauth_case

    async def run():
        async with client(app) as c:
            _, pairing, _, _, initial = await begin(c, cfg, provider)
            denied, page = await consent(c, initial, "invalid-pairing-code-123456789")
            assert denied.status_code == 403
            assert (
                page.headers["x-frame-options"] == "DENY"
                and "frame-ancestors" in page.headers["content-security-policy"]
            )
            denied, _ = await consent(c, initial, pairing, csrf="bad")
            assert denied.status_code == 403
            r = await c.post(
                initial.headers["location"],
                data={"decision": "approve"},
                headers={"Origin": "https://attacker.test"},
            )
            assert r.status_code == 403
            denied, _ = await consent(c, initial, pairing, decision="deny")
            q = parse_qs(urlsplit(denied.headers["location"]).query)
            assert q["error"] == ["access_denied"] and q["iss"] == [ISSUER]
            assert (
                provider.store.count("access") == 0
                and provider.store.count("code") == 0
            )

    asyncio.run(run())


def test_authorization_error_contains_issuer_and_no_consent(oauth_case):
    cfg, provider, app, owner = oauth_case

    async def run():
        async with client(app) as c:
            _, _, _, _, response = await begin(
                c, cfg, provider, resource="https://other.test/mcp"
            )
            if response.status_code in (302, 303, 307):
                q = parse_qs(urlsplit(response.headers["location"]).query)
                assert q["error"] and q["iss"] == [ISSUER]
            else:
                assert response.status_code == 400
            assert provider.store.count("pending") == 0

    asyncio.run(run())


def test_revocation_removes_whole_grant_family(oauth_case):
    cfg, provider, app, owner = oauth_case

    async def run():
        async with client(app) as c:
            registered, token, *_ = await grant(c, cfg, provider)
            response = await c.post(
                "/revoke",
                data={
                    "client_id": registered["client_id"],
                    "token": token["refresh_token"],
                },
            )
            assert response.status_code == 200, response.text
            assert (
                await c.get(
                    "/mcp", headers={"Authorization": "Bearer " + token["access_token"]}
                )
            ).status_code == 401
            assert provider.store.count("refresh") == 0

    asyncio.run(run())


def test_malformed_registration_is_structured(oauth_case):
    _, _, app, _ = oauth_case

    async def run():
        async with client(app) as c:
            response = await c.post(
                "/register",
                content=b"{not-json",
                headers={"Content-Type": "application/json"},
            )
            assert (
                response.status_code == 400
                and response.json()["error"] == "invalid_request"
            )

    asyncio.run(run())
