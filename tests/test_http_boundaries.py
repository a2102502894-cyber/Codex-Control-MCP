"""Body framing and SDK input boundaries without a network or real account."""

import asyncio
import base64
import json
import secrets
import time
from urllib.parse import quote

import pytest
from codex_control_mcp.http_guard import OwnerHTTP
from codex_control_mcp.auth import owner_token
from codex_control_mcp.errors import BridgeError
from test_oauth import oauth_case, client, ISSUER, CALLBACK  # noqa: F401


class Sink:
    def __init__(self):
        self.calls = 0
        self.body = None

    async def handle_request(self, scope, receive, send):
        self.calls += 1
        self.body = (await receive()).get("body")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def invoke(headers=(), chunks=(b"",), *, limit=32, events=None):
    sink = Sink()
    app = OwnerHTTP(sink, "x" * 48, limit=limit)
    output = []
    messages = (
        list(events)
        if events is not None
        else [
            {"type": "http.request", "body": chunk, "more_body": i < len(chunks) - 1}
            for i, chunk in enumerate(chunks)
        ]
    )

    async def receive():
        return messages.pop(0)

    async def send(msg):
        output.append(msg)

    asyncio.run(
        app(
            {
                "type": "http",
                "path": "/mcp",
                "method": "POST",
                "headers": [(b"authorization", b"Bearer " + b"x" * 48), *headers],
            },
            receive,
            send,
        )
    )
    return output, sink


@pytest.mark.parametrize(
    "length", [b"-1", b"+1", b"1.0", b" 1", b"1 ", b"", b"1,1", b"a", b"9" * 100]
)
def test_invalid_content_length_never_dispatches(length):
    out, sink = invoke([(b"content-length", length)], [b"x"])
    assert out[0]["status"] == 400
    assert sink.calls == 0


@pytest.mark.parametrize(
    "headers",
    [
        [(b"content-length", b"1"), (b"transfer-encoding", b"chunked")],
        [(b"transfer-encoding", b"gzip")],
        [(b"content-type", b"application/json"), (b"content-type", b"text/plain")],
        [(b"content-length", b"1"), (b"content-length", b"1")],
        [(b"mcp-session-id", b"first"), (b"mcp-session-id", b"second")],
    ],
)
def test_ambiguous_headers_never_dispatch(headers):
    out, sink = invoke(headers, [b"x"])
    assert out[0]["status"] == 400
    assert sink.calls == 0


def test_chunked_body_is_bounded_and_replayed_once():
    out, sink = invoke([(b"transfer-encoding", b"chunked")], [b"a", b"b", b"c"])
    assert out[0]["status"] == 200 and sink.body == b"abc"
    out, sink = invoke([], [b"a" * 20, b"b" * 20])
    assert out[0]["status"] == 413 and sink.calls == 0


def test_declared_length_mismatch_never_dispatches():
    out, sink = invoke([(b"content-length", b"2")], [b"x"])
    assert out[0]["status"] == 400 and sink.calls == 0


def test_client_disconnect_never_dispatches():
    out, sink = invoke(events=[{"type": "http.disconnect"}])
    assert not out and sink.calls == 0


@pytest.mark.parametrize(
    "body",
    [
        b'{"client_name":"\\ud800"}',
        b'{"x":NaN}',
        b"[]",
        b'{"x":{"a":1,"a":2}}',
        b'{"x":' + b"[" * 40 + b"0" + b"]" * 40 + b"}",
    ],
)
def test_registration_rejects_invalid_json_before_storing_client(oauth_case, body):
    cfg, provider, app, _ = oauth_case
    from codex_control_mcp.oauth import create_pairing_secret

    create_pairing_secret(cfg.home)

    async def run():
        async with client(app) as http:
            result = await http.post(
                "/register", content=body, headers={"Content-Type": "application/json"}
            )
            assert result.status_code == 400
            assert result.json()["error"] == "invalid_request"
            assert provider.store.count("client") == 0

    asyncio.run(run())


@pytest.mark.parametrize(
    "query", ["client_id=%FF", "client_id=%GG", "client_id=%", "x=1&x=2"]
)
def test_invalid_query_is_structured(oauth_case, query):
    _, _, app, _ = oauth_case

    async def run():
        async with client(app) as http:
            result = await http.get("/authorize?" + query)
            assert (
                result.status_code == 400
                and result.json()["error"] == "invalid_request"
            )

    asyncio.run(run())


@pytest.mark.parametrize("scheme", ["Basic", "basic", "BASIC"])
def test_sdk_basic_auth_works_without_duplicate_form_client_id(oauth_case, scheme):
    _, provider, app, _ = oauth_case
    cid, secret = "basic-client", secrets.token_urlsafe(32)
    provider.store.put(
        "client",
        cid,
        {
            "client_id": cid,
            "client_secret": secret,
            "redirect_uris": [CALLBACK],
            "grant_types": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_method": "client_secret_basic",
            "scope": "control offline_access",
        },
        time.time() + 60,
    )
    token = provider.issue_tokens(cid, ["control", "offline_access"])
    encoded = base64.b64encode(
        (quote(cid, safe="") + ":" + quote(secret, safe="")).encode()
    ).decode()

    async def run():
        async with client(app) as http:
            response = await http.post(
                "/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": token.refresh_token,
                    "resource": provider.resource,
                },
                headers={"Authorization": scheme + " " + encoded},
            )
            assert response.status_code == 200, response.text
            revoked = await http.post(
                "/revoke",
                data={"token": response.json()["refresh_token"]},
                headers={"Authorization": scheme + " " + encoded},
            )
            assert revoked.status_code == 200
            assert provider.store.count("access") == 0

    asyncio.run(run())


@pytest.mark.parametrize(
    "value", ["x" * 31, "x" * 513, "中文" * 32, "x" * 40 + "\n", "x" * 40 + " "]
)
def test_invalid_owner_token_is_configuration_error(oauth_case, monkeypatch, value):
    cfg, _, _, _ = oauth_case
    monkeypatch.setenv("CODEX_CONTROL_MCP_TOKEN", value)
    with pytest.raises(BridgeError) as error:
        owner_token(cfg)
    assert error.value.code == "invalid_config"
