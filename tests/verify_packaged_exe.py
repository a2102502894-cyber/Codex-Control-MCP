"""Verify the actual packaged bridge using an official execution-only test provider."""

from __future__ import annotations
import asyncio
import hashlib
import http.server
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
EXE = Path(
    os.environ.get(
        "CCM_VERIFY_EXE",
        str(ROOT.parent / "交付/Codex-Control-MCP/Codex-Control-MCP.exe"),
    )
)
RUN = ROOT / "test-workspace" / ("packaged-" + uuid.uuid4().hex[:10])
RUN.mkdir()
HOME = RUN / "home"
RUNTIME = HOME / "runtime-home"
RUNTIME.mkdir(parents=True)
attempts = []


class Guard(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        attempts.append({"method": "POST", "path": self.path.split("?")[0]})
        self.send_response(503)
        self.end_headers()

    def do_GET(self):
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args):
        pass


guard = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Guard)
threading.Thread(target=guard.serve_forever, daemon=True).start()
(RUNTIME / "config.toml").write_text(
    "\n".join(
        [
            'sandbox_mode="danger-full-access"',
            'approval_policy="never"',
            'model="packaged-execution-only"',
            'model_provider="execution_test"',
            "[analytics]",
            "enabled=false",
            "[feedback]",
            "enabled=false",
            "[model_providers.execution_test]",
            'name="Packaged execution sentinel"',
            f'base_url="http://127.0.0.1:{guard.server_port}/v1"',
            'wire_api="responses"',
            "requires_openai_auth=false",
            "",
        ]
    ),
    encoding="utf-8",
)
report = {
    "executable": str(EXE),
    "sha256": hashlib.sha256(EXE.read_bytes()).hexdigest(),
    "workspace": str(RUN),
    "checks": {},
    "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
}


def cli(*args):
    env = os.environ.copy()
    env.pop("OPENAI_API_KEY", None)
    env.pop("CODEX_API_KEY", None)
    p = subprocess.run(
        [str(EXE), "--home", str(HOME), *args],
        cwd=RUN,
        env=env,
        capture_output=True,
        timeout=50,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if p.returncode != 0:
        raise AssertionError(
            "EXE CLI failed: "
            + p.stdout.decode("utf-8", "replace")[-2500:]
            + p.stderr.decode("utf-8", "replace")[-1500:]
        )
    return json.loads(p.stdout)


async def mcp_check():
    params = StdioServerParameters(
        command=str(EXE), args=["--home", str(HOME), "serve"], cwd=str(RUN)
    )
    with (RUN / "stdio-stderr.log").open("w", encoding="utf-8") as err:
        async with stdio_client(params, errlog=err) as (read, write):
            async with ClientSession(read, write) as s:
                init = await s.initialize()
                tools = await s.list_tools()
                assert init.serverInfo.name == "Codex-Control-MCP"
                assert len(tools.tools) == 33

                async def call(name, args):
                    r = await s.call_tool(name, args)
                    assert not r.isError, str(r.structuredContent)
                    return r.structuredContent["result"]

                output = await call(
                    "exec_command", {"command": "Write-Output 'PACKAGED_CODEX_MCP_OK'"}
                )
                assert "PACKAGED_CODEX_MCP_OK" in output["stdout"]
                p = RUN / "proof.txt"
                text = "PACKAGED_FILE_PROOF_中文\r\n"
                await call("file_add", {"path": str(p), "content": text})
                result = await call("read_file", {"path": str(p)})
                assert result["content"] == text
                capability = await call("codex_capabilities", {})
                git = capability["runtime"]["git_path"]
                await call("exec_command", {"argv": [git, "init", str(RUN / "repo")]})
                status = await call("git_status", {"cwd": str(RUN / "repo")})
                assert status["exit_code"] == 0
                rejected = await s.call_tool("turn/start", {})
                assert rejected.isError
                report["checks"].update(
                    {
                        "mcp_initialize": True,
                        "tool_count": len(tools.tools),
                        "official_shell_marker": True,
                        "utf8_file_roundtrip": True,
                        "git_status": True,
                        "unknown_agent_tool_rejected": True,
                    }
                )


try:
    report["version"] = cli("version")
    health = cli("doctor", "--active")
    assert health["ok"]
    assert health["result"]["checks"]["shell"] == "PASS"
    assert health["result"]["child_is_admin_verified"] is True
    report["checks"]["doctor"] = True
    report["checks"]["admin_child"] = True
    report["effective_sandbox"] = health["result"]["effective_sandbox"]
    report["runtime"] = health["result"]["runtime"]
    asyncio.run(mcp_check())
    report["ok"] = True
except BaseException as exc:
    report["ok"] = False
    report["error"] = type(exc).__name__ + ": " + str(exc)[:5000]
finally:
    guard.shutdown()
    guard.server_close()
    report["configured_model_endpoint_attempts"] = attempts
    report["configured_model_request_count"] = len(attempts)
    report["full_process_network_trace"] = "not_performed"
    if attempts:
        report["ok"] = False
    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    (
        ROOT
        / "evidence"
        / os.environ.get("CCM_VERIFY_REPORT", "packaged-exe-verification.json")
    ).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True, indent=2))
raise SystemExit(0 if report["ok"] else 1)
