"""Verify the real inherited proxy through an official command/exec child.
Only an HTTPS HEAD request to Microsoft's public home page is transmitted.
"""

import asyncio, json, pathlib, sys, time
from codex_control_mcp.config import Config
from codex_control_mcp.client import call_running_service
from codex_control_mcp import __version__

ROOT = pathlib.Path(__file__).resolve().parents[1]
CODE = r"""
import http.client,json,os,ssl,time
from urllib.parse import urlsplit
raw=os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy')
if not raw:raise RuntimeError('HTTPS_PROXY not inherited')
p=urlsplit(raw)
if p.scheme!='http' or p.username or p.password:raise RuntimeError('Probe requires a credential-free HTTP CONNECT proxy')
c=http.client.HTTPSConnection(p.hostname,p.port,timeout=20,context=ssl.create_default_context())
c.set_tunnel('www.microsoft.com',443)
start=time.monotonic()
c.connect()
peer=c.sock.getpeername();tls=c.sock.version()
c.request('HEAD','/',headers={'Host':'www.microsoft.com','User-Agent':'Codex-Control-MCP network acceptance'})
r=c.getresponse()
print(json.dumps({'pid':os.getpid(),'target':'https://www.microsoft.com/','method':'HEAD','http_status':r.status,'tls_version':tls,'tls_certificate_verified':True,'socket_peer_host':peer[0],'socket_peer_port':peer[1],'proxy_host_from_environment':p.hostname,'proxy_port_from_environment':p.port,'elapsed_ms':round((time.monotonic()-start)*1000),'no_proxy_localhost_present':'127.0.0.1' in os.environ.get('NO_PROXY','')}))
c.close()
"""


async def main():
    cfg = Config.load()
    before = time.monotonic()
    result = await call_running_service(
        cfg,
        "exec_command",
        {
            "argv": [sys.executable, "-c", CODE],
            "cwd": str(ROOT / "test-workspace"),
            "timeout_ms": 30000,
            "output_limit_bytes": 10000,
        },
    )
    report = {
        "timestamp": time.time(),
        "scope": "actual bridge service -> official Codex command/exec -> test Python child -> inherited HTTP CONNECT proxy -> Microsoft HTTPS HEAD",
        "result": result,
        "total_client_ms": round((time.monotonic() - before) * 1000),
        "remote_websocket_verified": False,
        "gui_network_verified": False,
    }
    if result and result.get("ok"):
        try:
            report["network"] = json.loads(result["result"]["stdout"])
        except (KeyError, ValueError):
            pass
    n = report.get("network", {})
    report["pass"] = bool(
        n.get("tls_certificate_verified")
        and n.get("socket_peer_host") == n.get("proxy_host_from_environment")
        and n.get("socket_peer_port") == n.get("proxy_port_from_environment")
        and 100 <= n.get("http_status", 0) < 600
    )
    (
        ROOT / "evidence" / ("real-proxy-v" + __version__.replace(".", "") + ".json")
    ).write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps(report, ensure_ascii=True, indent=2))
    raise SystemExit(0 if report["pass"] else 1)


asyncio.run(main())
