"""End-to-end public HTTPS transport acceptance with the real owner credential.
Credentials stay in memory. This is not a ChatGPT account-linking test.
"""

import asyncio, json, pathlib, time
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from codex_control_mcp.auth import owner_token
from codex_control_mcp.config import Config, build_environment

ROOT = pathlib.Path(__file__).resolve().parents[1]


async def main():
    cfg = Config.load()
    record = json.loads((cfg.home / "state/tunnel.json").read_text("utf-8"))
    url = "https://" + record["hostname"] + "/mcp"
    env, _ = build_environment(cfg)
    report = {
        "timestamp": time.time(),
        "url": url,
        "auth": "bearer_required",
        "chatgpt_account_connected": False,
        "test_location": "owner laptop via public DNS, HTTPS and Cloudflare tunnel",
        "steps": [],
    }
    try:
        async with httpx.AsyncClient(
            proxy=env.get("HTTPS_PROXY"),
            trust_env=False,
            timeout=25,
            follow_redirects=False,
        ) as http:
            for name, headers in [
                ("unauthenticated", {}),
                (
                    "wrong_credential",
                    {"Authorization": "Bearer NOT_A_VALID_CREDENTIAL"},
                ),
            ]:
                r = await http.get(url, headers=headers)
                report["steps"].append(
                    {
                        "name": name,
                        "http_status": r.status_code,
                        "pass": r.status_code == 401,
                    }
                )
        async with httpx.AsyncClient(
            proxy=env.get("HTTPS_PROXY"),
            trust_env=False,
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
                    tools = await session.list_tools()
                    report["server"] = init.serverInfo.model_dump(mode="json")
                    report["tool_count"] = len(tools.tools)
                    r = await session.call_tool(
                        "exec_command",
                        {
                            "command": "Write-Output 'CODEX_CONTROL_HTTPS_OK'",
                            "shell": "powershell",
                            "cwd": str(ROOT / "test-workspace"),
                            "timeout_ms": 10000,
                        },
                    )
                    out = r.structuredContent or json.loads(
                        next(c.text for c in r.content if c.type == "text")
                    )
                    report["steps"].append(
                        {
                            "name": "public_https_official_exec",
                            "pass": bool(
                                out.get("ok")
                                and out.get("result", {}).get("stdout", "").strip()
                                == "CODEX_CONTROL_HTTPS_OK"
                            ),
                            "result": out,
                        }
                    )
    except Exception as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)[:1500]}
    report["pass"] = len(report["steps"]) == 3 and all(
        s["pass"] for s in report["steps"]
    )
    (ROOT / "evidence/https-acceptance-v011.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), "utf-8"
    )
    print(json.dumps(report, ensure_ascii=True, indent=2))
    raise SystemExit(0 if report["pass"] else 1)


asyncio.run(main())
