"""End-to-end MCP protocol and authentication tests; no public listening port."""

import asyncio, json, os, pathlib, secrets, socket, subprocess, sys, time
import httpx, pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

ROOT = pathlib.Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows test runtime required")


def test_stdio_real_mcp(tmp_path):
    async def run():
        env = os.environ.copy()
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "codex_control_mcp", "--home", str(tmp_path / "home"), "serve"],
            cwd=str(tmp_path),
            env=env,
        )
        with (tmp_path / "stderr.txt").open("w", encoding="utf-8") as err:
            async with stdio_client(params, errlog=err) as (read, write):
                async with ClientSession(read, write) as session:
                    init = await session.initialize()
                    assert init.serverInfo.name == "Codex-Control-MCP"
                    listed = await session.list_tools()
                    names = [t.name for t in listed.tools]
                    assert "exec_command" in names and not any(
                        n.startswith("browser_") for n in names
                    )
                    result = await session.call_tool(
                        "exec_command", {"command": "Write-Output 'MCP_STDIO_REAL_OK'"}
                    )
                    assert not result.isError and result.structuredContent["ok"]
                    assert (
                        "MCP_STDIO_REAL_OK"
                        in result.structuredContent["result"]["stdout"]
                    )
                    bad = await session.call_tool("read_file", {})
                    assert (
                        bad.isError
                        and bad.structuredContent["error"]["code"]
                        == "invalid_arguments"
                    )
                    nz = await session.call_tool("exec_command", {"command": "exit 9"})
                    assert (
                        nz.isError and nz.structuredContent["result"]["exit_code"] == 9
                    )
                    return {
                        "server": init.serverInfo.model_dump(),
                        "tool_count": len(names),
                        "marker": "MCP_STDIO_REAL_OK",
                        "structured_errors": True,
                    }

    report = asyncio.run(run())
    (ROOT / "evidence/stdio-transport.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )


def test_dpapi_roundtrip(tmp_path):
    from codex_control_mcp.config import Config
    from codex_control_mcp.auth import owner_token

    cfg = Config(home=tmp_path, cwd=str(tmp_path))
    cfg.initialize_storage()
    a = owner_token(cfg, create=True)
    assert len(a) >= 32
    assert owner_token(cfg) == a
    assert a.encode() not in (tmp_path / "state/http-token.dpapi").read_bytes()


def test_invalid_serve_startup_does_not_pollute_stdio(tmp_path):
    home = tmp_path / "bad"
    home.mkdir()
    (home / "config.toml").write_text('permission_mode = "typo"', encoding="utf-8")
    p = subprocess.run(
        [sys.executable, "-m", "codex_control_mcp", "--home", str(home), "serve"],
        capture_output=True,
        timeout=10,
    )
    assert p.returncode != 0 and not p.stdout and b"invalid_config" in p.stderr


def test_http_real_mcp_auth_origin_body_limits(tmp_path):
    token = secrets.token_urlsafe(40)
    env = os.environ.copy()
    env["CODEX_CONTROL_MCP_TOKEN"] = token
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}/mcp"
    home = tmp_path / "http-home"
    log = (tmp_path / "http.log").open("wb")
    p = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "codex_control_mcp",
            "--home",
            str(home),
            "serve",
            "--transport",
            "streamable-http",
            "--port",
            str(port),
        ],
        cwd=tmp_path,
        env=env,
        stdout=log,
        stderr=log,
    )
    report = {"url_scope": "loopback_only", "tests": {}}
    try:
        with httpx.Client(trust_env=False, timeout=3) as c:
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                try:
                    if c.get(url).status_code == 401:
                        break
                except httpx.TransportError:
                    pass
                if p.poll() is not None:
                    pytest.fail("HTTP service exited during startup")
                time.sleep(0.1)
            else:
                pytest.fail("HTTP service did not start")
            assert (
                c.get(url, headers={"Authorization": "Bearer wrong"}).status_code == 401
            )
            report["tests"]["wrong_token"] = 401
            headers = {
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            }
            initialize = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "http-test", "version": "1"},
                },
            }
            assert (
                c.post(
                    url,
                    headers={**headers, "Origin": "https://untrusted.invalid"},
                    json=initialize,
                ).status_code
                == 403
            )
            report["tests"]["untrusted_origin"] = 403
            assert c.post(
                url, headers={**headers, "Host": "untrusted.invalid"}, json=initialize
            ).status_code in (400, 403, 421)
            report["tests"]["untrusted_host"] = "rejected"
            big = c.post(url, headers=headers, content=b"x" * (2097152 + 1))
            assert big.status_code == 413
            report["tests"]["oversized_body"] = 413

        async def run():
            async with httpx.AsyncClient(
                headers={"Authorization": "Bearer " + token},
                trust_env=False,
                timeout=30,
            ) as http:
                async with streamable_http_client(url, http_client=http) as (
                    read,
                    write,
                    _,
                ):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        assert any(t.name == "exec_command" for t in tools.tools)
                        r = await session.call_tool(
                            "exec_command",
                            {"command": "Write-Output 'MCP_HTTP_REAL_OK'"},
                        )
                        assert (
                            not r.isError
                            and "MCP_HTTP_REAL_OK"
                            in r.structuredContent["result"]["stdout"]
                        )
                        report["tests"]["official_execution"] = "MCP_HTTP_REAL_OK"

        asyncio.run(run())
        cli = subprocess.run(
            [
                sys.executable,
                "-m",
                "codex_control_mcp",
                "--home",
                str(home),
                "doctor",
                "--active",
            ],
            env=env,
            capture_output=True,
            timeout=30,
        )
        assert cli.returncode == 0, cli.stderr.decode("utf-8", "replace")
        live = json.loads(cli.stdout)
        assert (
            live["via_running_service"] and live["result"]["checks"]["shell"] == "PASS"
        )
        report["tests"]["cli_reuses_service"] = True
    finally:
        p.terminate()
        try:
            p.wait(6)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(5)
        log.close()
        report["owned_http_server_stopped"] = p.poll() is not None
        report["chatgpt_web_verified"] = False
        assert token.encode() not in (tmp_path / "http.log").read_bytes()
        (ROOT / "evidence/http-transport.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
