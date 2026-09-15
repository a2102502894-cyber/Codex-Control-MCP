"""Probe only the official installed GUI MCP entry point; no user UI mutations."""

import asyncio, json, os, pathlib, time
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = pathlib.Path(__file__).resolve().parents[1]


async def main():
    base = (
        pathlib.Path.home() / ".codex/plugins/cache/openai-bundled/unified-computer-use"
    )
    manifests = sorted(
        base.glob("*/.mcp.json"), key=lambda p: p.stat().st_mtime_ns, reverse=True
    )
    report = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "scope": "official plugin direct initialize/tools/list and one read-only JS availability probe",
        "model_turn_started": False,
    }
    if not manifests:
        report["error"] = "official_manifest_missing"
    else:
        manifest = manifests[0]
        config = json.loads(manifest.read_text("utf-8"))["mcpServers"]["cua_repl"]
        env = os.environ.copy()
        env.update(config.get("env", {}))
        report["manifest"] = str(manifest)
        report["configured_surfaces"] = env.get("CUA_REPL_ENABLED_SURFACES")
        report["command_exists"] = pathlib.Path(config["command"]).exists()
        # Honor the installed official manifest as-is; do not invent pipe endpoints or permissions.
        params = StdioServerParameters(
            command=config["command"],
            args=config["args"],
            env=env,
            cwd=str(manifest.parent),
        )
        try:
            async with asyncio.timeout(45):
                with open(
                    ROOT / "evidence/gui-plugin-stderr.log", "w", encoding="utf-8"
                ) as err:
                    async with stdio_client(params, errlog=err) as (r, w):
                        async with ClientSession(r, w) as session:
                            init = await session.initialize()
                            report["server_info"] = init.serverInfo.model_dump()
                            tools = await session.list_tools()
                            report["tools"] = [
                                t.model_dump(mode="json") for t in tools.tools
                            ]
                            js = next((t for t in tools.tools if t.name == "js"), None)
                            if js:
                                report["readonly_probe"] = (
                                    await session.call_tool(
                                        "js",
                                        {
                                            "code": "nodeRepl.write(JSON.stringify({sky: typeof globalThis.sky, browser: typeof globalThis.browser, agent: typeof globalThis.agent}));",
                                            "title": "Codex-Control-MCP official GUI availability probe",
                                        },
                                    )
                                ).model_dump(mode="json")
        except BaseException as e:
            report["error_type"] = type(e).__name__
            report["error"] = str(e)[:2000]
    (ROOT / "evidence/gui-plugin-probe.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # Tool descriptions can be lengthy; keep console output focused.
    print(
        json.dumps(
            {
                **report,
                "tools": [
                    {"name": t["name"], "inputSchema": t["inputSchema"]}
                    for t in report.get("tools", [])
                ],
            },
            ensure_ascii=True,
            indent=2,
        )
    )


asyncio.run(main())
