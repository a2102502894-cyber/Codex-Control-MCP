"""Read-only liveness + a harmless command through authenticated MCP. No retries.

Credentials remain in memory; stdout contains only a bounded, payload-free report.
An unhealthy probe never restarts a service or resubmits an unknown command.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import time

import httpx
from codex_control_mcp.auth import owner_token
from codex_control_mcp.config import Config


def rpc_result(response, request_id):
    response.raise_for_status()
    if "text/event-stream" in response.headers.get("content-type", ""):
        frames = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]
        payload = next(frame for frame in frames if frame.get("id") == request_id)
    else:
        payload = response.json()
    if payload.get("error"):
        raise RuntimeError("mcp_rpc_error")
    return payload["result"]


async def probe(home=None, public=False, timeout_seconds=10):
    cfg = Config.load(home)
    url = (cfg.oauth["issuer"].rstrip("/") if public else f"http://127.0.0.1:{cfg.http.get('port', 8774)}") + "/mcp"
    headers = {"Authorization": "Bearer " + owner_token(cfg), "Accept": "application/json, text/event-stream"}
    started = time.monotonic()
    counter = 0
    async with asyncio.timeout(timeout_seconds):
        async with httpx.AsyncClient(trust_env=False, timeout=timeout_seconds, headers=headers) as client:
            async def rpc(method, params):
                nonlocal counter
                counter += 1
                response = await client.post(url, json={"jsonrpc": "2.0", "id": counter, "method": method, "params": params})
                if response.headers.get("Mcp-Session-Id"):
                    client.headers["Mcp-Session-Id"] = response.headers["Mcp-Session-Id"]
                return rpc_result(response, counter)

            async def call(name, arguments):
                result = await rpc("tools/call", {"name": name, "arguments": arguments})
                data = result.get("structuredContent")
                if data is None:
                    data = json.loads(next(c["text"] for c in result["content"] if c.get("type") == "text"))
                if result.get("isError") or not data.get("ok"):
                    raise RuntimeError("mcp_tool_error")
                return data["result"]

            init = await rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "ccm-application-probe", "version": "1"}})
            if init["serverInfo"]["name"] != "Codex-Control-MCP":
                raise RuntimeError("mcp_identity_mismatch")
            client.headers["MCP-Protocol-Version"] = init["protocolVersion"]
            notified = await client.post(url, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
            notified.raise_for_status()
            tools = await rpc("tools/list", {})
            health = await call("codex_health", {})
            checks = health.get("checks", {})
            if any(checks.get(key) != "PASS" for key in ("codex", "app_server", "schema")):
                raise RuntimeError("official_backend_unhealthy")
            receipt = await call("exec_command", {"command": "Write-Output 'CCM_APPLICATION_PROBE_OK'", "timeout_ms": 3000, "yield_time_ms": 1000, "output_limit_bytes": 1024})
            text = receipt.get("stdout", "")
            while receipt.get("state") in {"starting", "running"} or receipt.get("has_more"):
                await asyncio.sleep(0.15)
                receipt = await call("session_read", {"session_id": receipt["session_id"], "cursor": receipt["next_cursor"], "max_bytes": 1024})
                text += receipt.get("stdout", "")
            if receipt.get("exit_code") != 0 or "CCM_APPLICATION_PROBE_OK" not in text:
                raise RuntimeError("official_command_probe_failed")
            sessions = await call("session_list", {})
            return {"ok": True, "scope": "public_loopback" if public else "local_mcp_and_official_execution", "version": init["serverInfo"]["version"], "tool_count": len(tools["tools"]), "command_probe": "PASS", "active_sessions": sum(s.get("state") in {"starting", "running"} for s in sessions["sessions"]), "elapsed_ms": round((time.monotonic() - started) * 1000), "client_receipt_confirmed": True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path)
    parser.add_argument("--public", action="store_true")
    args = parser.parse_args()
    try:
        report = asyncio.run(probe(args.home, args.public))
    except Exception as exc:
        # Exceptions may contain URLs, authorization, or returned output.
        report = {"ok": False, "error_type": type(exc).__name__, "automatic_restart_performed": False, "execution_retry_performed": False}
    print(json.dumps(report, separators=(",", ":")))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
