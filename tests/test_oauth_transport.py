"""Actual loopback MCP execution authenticated with an isolated OAuth grant.
The protocol/consent flow is separately tested in test_oauth.py. This test uses
an isolated provider-issued fixture grant, never the user's real account.
Set CCM_TEST_EXE to verify the packaged executable instead of Python source.
"""

import asyncio
import os
import socket
import subprocess
import sys
import time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from codex_control_mcp.auth import owner_token
from codex_control_mcp.config import Config, DEFAULT_CONFIG
from codex_control_mcp.oauth import OwnerOAuth


def test_real_oauth_http_official_execution_and_shutdown(tmp_path):
    home = tmp_path / "oauth-home"
    home.mkdir()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    text = (
        DEFAULT_CONFIG.replace("8774", str(port))
        + '\n[oauth]\nenabled=true\nissuer="https://isolated-owner.test/"\n'
    )
    (home / "config.toml").write_text(text, "utf-8")
    cfg = Config.load(home)
    cfg.initialize_storage()
    owner_token(cfg, create=True)
    provider = OwnerOAuth(cfg)
    try:
        grant = provider.issue_tokens(
            "isolated-transport-fixture", ["control", "offline_access"]
        )
    finally:
        provider.close()
    exe = os.environ.get("CCM_TEST_EXE")
    prefix = [exe] if exe else [sys.executable, "-m", "codex_control_mcp"]
    log = (tmp_path / "oauth-http.log").open("wb")
    proc = subprocess.Popen(
        [*prefix, "--home", str(home), "serve", "--transport", "streamable-http"],
        cwd=tmp_path,
        stdout=log,
        stderr=log,
    )
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                assert proc.poll() is None, (
                    "Isolated OAuth service exited during startup"
                )
                time.sleep(0.1)
        else:
            raise AssertionError("Isolated OAuth service did not bind")
        with httpx.Client(trust_env=False, timeout=8) as c:
            response = c.get(url)
            assert (
                response.status_code == 401
                and "oauth-protected-resource/mcp"
                in response.headers["www-authenticate"]
            )
            meta = c.get(
                f"http://127.0.0.1:{port}/.well-known/oauth-authorization-server"
            )
            assert (
                meta.status_code == 200
                and meta.json()["issuer"] == "https://isolated-owner.test/"
            )

        async def exercise():
            async with httpx.AsyncClient(
                trust_env=False,
                timeout=40,
                headers={"Authorization": "Bearer " + grant.access_token},
            ) as client:
                async with streamable_http_client(url, http_client=client) as (r, w, _):
                    async with ClientSession(r, w) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        assert len(tools.tools) == 33
                        assert all(
                            t.meta["securitySchemes"][0]["type"] == "oauth2"
                            and t.meta["securitySchemes"][0]["scopes"] == ["control", "offline_access"]
                            for t in tools.tools
                        )
                        result = await session.call_tool(
                            "exec_command",
                            {
                                "command": "Write-Output 'OAUTH_MCP_LOCAL_OFFICIAL_OK'",
                                "cwd": str(tmp_path),
                            },
                        )
                        assert not result.isError, result.structuredContent
                        assert (
                            result.structuredContent["result"]["stdout"].strip()
                            == "OAUTH_MCP_LOCAL_OFFICIAL_OK"
                        )
                        assert (
                            result.structuredContent["result"]["execution_backend"]
                            == "codex_app_server.command_exec"
                        )

        asyncio.run(exercise())
        stopped = subprocess.run(
            [*prefix, "--home", str(home), "stop"], capture_output=True, timeout=55
        )
        assert stopped.returncode == 0, stopped.stdout.decode("utf-8", "replace")
        assert proc.wait(10) == 0
        assert not (home / "state/service.json").exists()
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(5)
        log.close()
        assert (
            grant.access_token.encode()
            not in (tmp_path / "oauth-http.log").read_bytes()
        )
        assert (
            grant.refresh_token.encode()
            not in (tmp_path / "oauth-http.log").read_bytes()
        )
