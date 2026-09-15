"""Public TLS end-to-end acceptance. Owner token remains memory-only.
This tests the independently served MCP and OAuth discovery, not ChatGPT account
registration. The local fixture directory belongs exclusively to this probe.
"""

from __future__ import annotations
import asyncio
import argparse
import json
from pathlib import Path
import sys
import time
import uuid

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from codex_control_mcp import __version__
from codex_control_mcp.auth import owner_token
from codex_control_mcp.config import Config, build_environment
from codex_control_mcp.common import atomic_json
from codex_control_mcp.oauth import OAuthStore

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from public_transport import SafeConnectTransport


async def main(output_root=None):
    run_root = Path(output_root).resolve() if output_root else ROOT / "evidence" / ("public-" + uuid.uuid4().hex[:10])
    run_root.mkdir(parents=True, exist_ok=True)
    transport_events = []
    cfg = Config.load()
    record = json.loads((cfg.home / "state/tunnel.json").read_text("utf-8"))
    origin = "https://" + record["hostname"]
    url = origin + "/mcp"
    env, _ = build_environment(cfg)
    probe = run_root / "test-workspace" / ("public-" + uuid.uuid4().hex[:10])
    probe.mkdir(parents=True)
    proof = probe / "utf8-proof.txt"
    report = {
        "version": __version__,
        "started_at": time.time(),
        "url": url,
        "checks": [],
        "scope": "owner laptop through configured proxy, public DNS, verified TLS and independent Cloudflare tunnel",
        "chatgpt_account_connected": False,
        "real_chatgpt_oauth_consent_performed": False,
        "fixture_directory": str(probe),
        "credentials_logged": False,
        "http_connection_reuse": True,
        "connection_establishment_retries": 3,
        "transport": "safe_connect",
        "route": "configured_proxy" if env.get("HTTPS_PROXY") else "direct",
        "response_or_execution_retries": False,
    }

    def check(name, passed, **details):
        report["checks"].append({"name": name, "pass": bool(passed), **details})
        if not passed:
            raise AssertionError("Acceptance failed: " + name)

    try:
        # This negative registration probe must never consume a real owner's
        # enrollment window or create a client during interactive connection.
        store = OAuthStore(cfg.home)
        try:
            if store.count("enrollment"):
                raise RuntimeError("Close the interactive enrollment window before running public acceptance")
        finally:
            store.db.close()
        async with httpx.AsyncClient(
            trust_env=False,
            transport=SafeConnectTransport(proxy=env.get("HTTPS_PROXY"),
                connect_retries=3, events=transport_events),
            timeout=25,
            follow_redirects=False,
        ) as http:
            for name, headers in [
                ("missing_auth", {}),
                (
                    "wrong_auth",
                    {"Authorization": "Bearer deliberately-invalid-test-credential"},
                ),
            ]:
                r = await http.get(url, headers=headers)
                check(name, r.status_code == 401, http_status=r.status_code)
                check(
                    name + "_oauth_challenge",
                    "oauth-protected-resource/mcp"
                    in r.headers.get("www-authenticate", ""),
                )
            r = await http.get(origin + "/.well-known/oauth-authorization-server")
            check(
                "oauth_authorization_metadata",
                r.status_code == 200,
                http_status=r.status_code,
            )
            meta = r.json()
            check(
                "issuer_pkce_and_registration",
                meta.get("issuer") == cfg.oauth["issuer"]
                and meta.get("code_challenge_methods_supported") == ["S256"]
                and meta.get("registration_endpoint") == origin + "/register"
                and meta.get("authorization_response_iss_parameter_supported") is True,
            )
            for path in [
                "/.well-known/oauth-protected-resource",
                "/.well-known/oauth-protected-resource/mcp",
            ]:
                r = await http.get(origin + path)
                check(
                    "resource_metadata_" + path.rsplit("/", 1)[-1],
                    r.status_code == 200 and r.json().get("resource") == url,
                    http_status=r.status_code,
                )
            r = await http.post(
                origin + "/register",
                json={
                    "client_name": "CCM-closed-enrollment-negative-probe",
                    "redirect_uris": [
                        "https://chatgpt.com/connector_platform_oauth_redirect"
                    ],
                    "grant_types": ["authorization_code", "refresh_token"],
                },
            )
            check(
                "owner_enrollment_closed",
                r.status_code == 400 and "closed" in r.text,
                http_status=r.status_code,
            )
            r = await http.get(
                origin + "/authorize",
                headers={"Origin": "https://invalid-origin.example"},
            )
            check(
                "wrong_oauth_origin_rejected",
                r.status_code == 403,
                http_status=r.status_code,
            )
            r = await http.get(origin + "/not-a-published-route")
            check(
                "unpublished_route_not_exposed",
                r.status_code == 404,
                http_status=r.status_code,
            )
        async with httpx.AsyncClient(
            trust_env=False,
            transport=SafeConnectTransport(proxy=env.get("HTTPS_PROXY"),
                connect_retries=3, events=transport_events),
            timeout=60,
            headers={"Authorization": "Bearer " + owner_token(cfg)},
        ) as http:
            async with streamable_http_client(url, http_client=http) as (
                read,
                write,
                _,
            ):
                async with ClientSession(read, write) as session:
                    init = await session.initialize()
                    check(
                        "independent_server_identity",
                        init.serverInfo.name == "Codex-Control-MCP"
                        and init.serverInfo.version == __version__,
                        server=init.serverInfo.model_dump(mode="json"),
                    )
                    tools = await session.list_tools()
                    check(
                        "configured_tools_with_oauth_metadata",
                        len(tools.tools) == 33 + (6 if cfg.computer_use_enabled else 0) + (8 if cfg.browser_use_enabled else 0)
                        and all(
                            t.meta
                            and t.meta.get("securitySchemes", [{}])[0].get("type")
                            == "oauth2"
                            for t in tools.tools
                        ),
                        tool_count=len(tools.tools),
                    )

                    async def call(name, args):
                        result = await session.call_tool(name, args)
                        payload = result.structuredContent
                        if result.isError or not payload or not payload.get("ok"):
                            raise AssertionError("Public tool failed: " + name)
                        return payload["result"]

                    result = await call(
                        "exec_command",
                        {
                            "command": "Write-Output 'CODEX_PUBLIC_HTTPS_V013_OK'",
                            "cwd": str(probe),
                            "timeout_ms": 10000,
                        },
                    )
                    check(
                        "public_official_shell",
                        result["stdout"].strip() == "CODEX_PUBLIC_HTTPS_V013_OK"
                        and result["execution_backend"]
                        == "codex_app_server.command_exec",
                        backend=result["execution_backend"],
                    )
                    text = "PUBLIC_HTTPS_UTF8_中文\r\n"
                    await call("file_add", {"path": str(proof), "content": text})
                    result = await call("read_file", {"path": str(proof)})
                    check("public_utf8_file_roundtrip", result["content"] == text)
                    await call("file_delete", {"path": str(proof)})
                    check("public_owned_file_delete", not proof.exists())
                    start = await call(
                        "session_start",
                        {
                            "argv": [
                                sys.executable,
                                "-u",
                                "-c",
                                "print('PUBLIC_READY',flush=True); v=input(); print('PUBLIC_INPUT_'+v,flush=True)",
                            ],
                            "cwd": str(probe),
                            "timeout_ms": 15000,
                        },
                    )
                    sid = start["session_id"]
                    await call(
                        "session_write",
                        {"session_id": sid, "text": "HELLO\n", "close_stdin": True},
                    )
                    cursor = 0
                    output = ""
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        part = await call(
                            "session_read",
                            {"session_id": sid, "cursor": cursor, "max_bytes": 32768},
                        )
                        assert not part["cursor_gap"]
                        cursor = part["next_cursor"]
                        output += part["stdout"]
                        if part["state"] == "exited" and not part["has_more"]:
                            break
                        await asyncio.sleep(0.15)
                    check(
                        "public_session_stdin_streaming",
                        part["state"] == "exited"
                        and part["exit_code"] == 0
                        and "PUBLIC_INPUT_HELLO" in output,
                        session_id=sid,
                        final_state=part["state"],
                        cursor=cursor,
                    )
                    report["health"] = await call("codex_health", {"active": True})
        report["pass"] = all(c["pass"] for c in report["checks"])
    except BaseException as exc:
        report["pass"] = False
        report["error"] = {"type": type(exc).__name__, "message": str(exc)[:1200]}
        # ExceptionGroup hides the useful transport failure. Record only safe
        # classifications; never persist request headers, bodies or auth URLs.
        def classify(error):
            if isinstance(error, BaseExceptionGroup):
                return [item for child in error.exceptions for item in classify(child)]
            item = {'type': type(error).__name__}
            if isinstance(error, httpx.HTTPStatusError):
                item['http_status'] = error.response.status_code
                item['request_method'] = error.request.method
            if getattr(error, 'error', None) is not None:
                item['rpc_code'] = getattr(error.error, 'code', None)
            return [item]
        report['error']['causes'] = classify(exc)
    finally:
        # This path was created by this probe; no business files are touched.
        if proof.exists():
            proof.unlink()
        report["finished_at"] = time.time()
        path = (
            run_root
            / "evidence"
            / ("https-acceptance-v" + __version__.replace(".", "") + ".json")
        )
        report["connection_recoveries"] = sum(bool(a.get("retried")) for r in transport_events for a in r["attempts"])
        report["evidence_directory"] = str(path.parent)
        atomic_json(path.parent / "transport.json", {"requests": transport_events})
        atomic_json(path, report)
        print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report.get("pass") else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, help="Optional unique run directory; default creates a new evidence/public-* directory")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.output_root)))
