"""Bounded HTTP/OAuth parsing before the SDK consumes authorization state."""

from __future__ import annotations
import base64
import hmac
import json
import re
from urllib.parse import parse_qsl, unquote, urlencode
from starlette.requests import Request


def ascii_equal(supplied: str, expected: str) -> bool:
    if not isinstance(supplied, str) or not isinstance(expected, str):
        return False
    try:
        return hmac.compare_digest(supplied.encode("ascii"), expected.encode("ascii"))
    except UnicodeError:
        return False


def bearer_credential(header: bytes) -> bytes | None:
    if not isinstance(header, bytes) or len(header) > 1024:
        return None
    match = re.fullmatch(rb"(?i:Bearer) +([A-Za-z0-9._~+/-]+=*)", header)
    return match[1] if match and 1 <= len(match[1]) <= 512 else None


def unique_fields(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate OAuth field")
        result[name] = value
    return result


def form_fields(raw):
    text = raw.decode("utf-8")
    if re.search(r"%(?![0-9a-fA-F]{2})", text):
        raise ValueError("Invalid percent encoding")
    return unique_fields(
        parse_qsl(text, keep_blank_values=True, max_num_fields=100, errors="strict")
    )


def valid_json_text(value, depth=0):
    if depth > 32:
        raise ValueError("OAuth object nesting limit exceeded")
    if isinstance(value, str):
        value.encode("utf-8")
    elif isinstance(value, dict):
        for key, item in value.items():
            valid_json_text(key, depth + 1)
            valid_json_text(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            valid_json_text(item, depth + 1)


def invalid_constant(value):
    raise ValueError("Non-finite JSON number")


async def validate_oauth_request(scope, receive):
    """Replay bounded body; normalize Basic for SDK 1.30's required client_id.

    Authentication and PKCE verification still happen in the SDK.
    """
    query = scope.get("query_string", b"")
    if len(query) > 8192:
        raise ValueError("OAuth query limit exceeded")
    query_fields = form_fields(query)
    if scope.get("method") != "POST":
        return receive
    request = Request(scope, receive)
    body = await request.body()
    if len(body) > 65536:
        raise ValueError("OAuth body limit exceeded")
    kind = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    path = scope.get("path")
    if path == "/register":
        if kind != "application/json":
            raise ValueError("Registration requires a JSON object")
        value = json.loads(
            body, object_pairs_hook=unique_fields, parse_constant=invalid_constant
        )
        if not isinstance(value, dict):
            raise ValueError("Registration requires a JSON object")
        valid_json_text(value)
    elif path in {"/authorize", "/consent", "/token", "/revoke"}:
        if kind != "application/x-www-form-urlencoded":
            raise ValueError("OAuth request requires URL-encoded form data")
        fields = form_fields(body)
        if set(fields) & set(query_fields):
            raise ValueError("OAuth fields appear in both query and body")
        if path in {"/token", "/revoke"}:
            auth = request.headers.get("authorization", "")
            if auth:
                match = re.fullmatch(r"(?i:Basic) +([A-Za-z0-9+/]+={0,2})", auth)
                if not match or "client_secret" in fields:
                    raise ValueError("Ambiguous client authentication")
                credentials = base64.b64decode(match[1], validate=True).decode("utf-8")
                basic_id, separator, _ = credentials.partition(":")
                basic_id = unquote(basic_id, errors="strict")
                if (
                    not separator
                    or not basic_id
                    or ("client_id" in fields and fields["client_id"] != basic_id)
                ):
                    raise ValueError("Invalid Basic client identity")
                fields["client_id"] = basic_id
                body = urlencode(fields).encode("utf-8")
                scope["headers"] = [
                    (
                        key,
                        ("Basic " + match[1]).encode("ascii")
                        if key.lower() == b"authorization"
                        else str(len(body)).encode("ascii")
                        if key.lower() == b"content-length"
                        else value,
                    )
                    for key, value in scope.get("headers", [])
                ]
    delivered = False

    async def replay():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return replay
