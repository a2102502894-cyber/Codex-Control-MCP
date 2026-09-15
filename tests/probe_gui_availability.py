"""Read-only official GUI capability probe. No screenshot or user-window mutation."""

import asyncio, json, os, pathlib, time
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = pathlib.Path(__file__).resolve().parents[1]


async def main():
    base = (
        pathlib.Path.home() / ".codex/plugins/cache/openai-bundled/unified-computer-use"
    )
    manifest = max(base.glob("*/.mcp.json"), key=lambda p: p.stat().st_mtime_ns)
    c = json.loads(manifest.read_text("utf-8"))["mcpServers"]["cua_repl"]
    env = os.environ.copy()
    env.update(c.get("env", {}))
    report = {
        "manifest": str(manifest),
        "steps": [],
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }

    def compact(result):
        return {
            "isError": result.isError,
            "content": [
                {"type": x.type, "text": x.text[-14000:]}
                for x in result.content
                if x.type == "text"
            ],
        }

    try:
        async with asyncio.timeout(65):
            with open(os.devnull, "w") as err:
                async with stdio_client(
                    StdioServerParameters(
                        command=c["command"],
                        args=c["args"],
                        cwd=str(manifest.parent),
                        env=env,
                    ),
                    errlog=err,
                ) as (r, w):
                    async with ClientSession(r, w) as s:
                        await s.initialize()
                        for name, code in [
                            (
                                "runtime",
                                "nodeRepl.write(JSON.stringify({cua:typeof cua,agent:typeof agent}));",
                            ),
                            ("browsers", "await cua.listBrowsers();"),
                            (
                                "native_readonly",
                                'try { const {sky}=await import("@oai/sky"); nodeRepl.write(JSON.stringify((await sky.list_windows()).map(w=>({id:w.id,app:w.app})))); } catch(e) { nodeRepl.write(JSON.stringify({error:String(e)})); }',
                            ),
                        ]:
                            result = await s.call_tool(
                                "js",
                                {
                                    "code": code,
                                    "timeout_ms": 12000,
                                    "title": "Codex-Control-MCP " + name,
                                },
                            )
                            d = compact(result)
                            if name == "runtime":
                                d["content"] = d["content"][-1:]
                            report["steps"].append({"name": name, "result": d})
                            print(
                                json.dumps(report["steps"][-1], ensure_ascii=True),
                                flush=True,
                            )
    except BaseException as e:
        report["error"] = type(e).__name__ + ": " + str(e)[:1500]
    (ROOT / "evidence/gui-availability.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("GUI_AVAILABILITY_PROBE_FINISHED")


asyncio.run(main())
