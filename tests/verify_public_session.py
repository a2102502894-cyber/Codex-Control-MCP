"""Bounded public MCP session diagnostic; no command execution or network changes."""
import asyncio
import json
from pathlib import Path
import re
import time
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from codex_control_mcp.auth import owner_token
from codex_control_mcp.config import Config, build_environment

cfg = Config.load()
env, _ = build_environment(cfg)
report = {'started_at': time.time(), 'pass': False, 'responses': [], 'clash_modified': False, 'system_proxy_modified': False}

async def main():
    async def observed(response):
        report['responses'].append({'method':response.request.method,'path':response.request.url.path,
            'status':response.status_code,'content_type':response.headers.get('content-type'),
            'session_header_present':bool(response.headers.get('mcp-session-id'))})
    try:
        async with asyncio.timeout(40):
            async with httpx.AsyncClient(trust_env=False, proxy=env.get('HTTPS_PROXY'), timeout=15,
                    limits=httpx.Limits(max_keepalive_connections=0), event_hooks={'response':[observed]},
                    headers={'Authorization':'Bearer '+owner_token(cfg)}) as http:
                async with streamable_http_client(cfg.oauth['issuer']+'mcp',http_client=http) as (read,write,_):
                    async with ClientSession(read,write) as client:
                        report['version']=(await client.initialize()).serverInfo.version
                        report['tool_count']=len((await client.list_tools()).tools)
                        result=await client.call_tool('codex_health',{})
                        report['health_ok']=result.structuredContent['ok']
                        report['pass']=report['version']=='0.1.9' and report['tool_count']==29 and report['health_ok']
    except BaseException as exc:
        def errors(error):
            if isinstance(error,BaseExceptionGroup):
                return [row for child in error.exceptions for row in errors(child)]
            return [{'type':type(error).__name__,'message':re.sub(r'https?://\S+','[URL]',str(error))[:300]}]
        report['errors']=errors(exc)
    report['finished_at']=time.time()
    (Path(__file__).resolve().parents[1]/'evidence/public-session-v019.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps(report,ensure_ascii=True))

asyncio.run(main())
