"""Eight real Browser tools through a packaged or installed MCP HTTP service."""
import argparse
import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import http.server
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import uuid
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from codex_control_mcp import __version__
from codex_control_mcp.auth import owner_token
from codex_control_mcp.client import call_running_service
from codex_control_mcp.config import Config, build_environment
from codex_control_mcp.common import atomic_json
from codex_control_mcp.lifecycle import create_owned_process_job
from test_tabbit_browser import HTML

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from public_transport import SafeConnectTransport

async def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe',type=Path)
    parser.add_argument('--public',action='store_true')
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    run=args.out.resolve(); run.mkdir(parents=True,exist_ok=False)
    counts={'posts':0}
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path=='/record': counts['posts']+=1
            body=('<label for="f">框架输入</label><input id="f">' if self.path=='/frame' else HTML).encode('utf-8')
            self.send_response(200); self.send_header('Content-Type','text/html;charset=utf-8')
            self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
        def log_message(self,*_): pass
    fixture=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=fixture.serve_forever,daemon=True).start()
    page_url=f'http://127.0.0.1:{fixture.server_port}/'
    proc=job=None
    owned_page=None
    events=[]
    report={'started_at':datetime.now(timezone.utc).isoformat(),'version':__version__,
        'acceptance_transport':'streamable_http','public':args.public,'checks':[],
        'clash_modified':False,'system_network_modified':False,'daily_tabbit_attached':False}
    def check(name,condition,**detail):
        report['checks'].append({'name':name,'pass':bool(condition),**detail})
        if not condition: raise AssertionError(name)
    try:
        if args.exe:
            exe=args.exe.resolve()
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
            home=run/'home'; home.mkdir()
            profile=Path.home()/'.agentdock/browser/profiles'/('ccm-http-'+uuid.uuid4().hex[:10])
            (home/'config.toml').write_text('\n'.join([
                'permission_mode="trusted_owner_full_access"','computer_use_enabled=true','browser_use_enabled=true',
                'cwd='+json.dumps(str(run)),'[browser]','backend="tabbit"','headless=true',
                'profile_directory='+json.dumps(str(profile)), '[http]',f'port={port}',
            ])+'\n','utf-8')
            cfg=Config.load(home); cfg.initialize_storage()
            token=owner_token(cfg,create=True)
            with (run/'service.log').open('wb') as log:
                proc=subprocess.Popen([str(exe),'--home',str(home),'serve','--transport','streamable-http'],
                    cwd=run,stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
            job=create_owned_process_job(proc._handle)
            report.update(executable=str(exe),executable_sha256=hashlib.sha256(exe.read_bytes()).hexdigest(),pid=proc.pid)
            deadline=time.monotonic()+35
            while time.monotonic()<deadline:
                try:
                    with socket.create_connection(('127.0.0.1',port),timeout=.3): break
                except OSError:
                    if proc.poll() is not None: raise RuntimeError('Packaged service exited during startup')
                    await asyncio.sleep(.2)
            else: raise TimeoutError('Packaged service startup')
        else:
            cfg=Config.load(Path.home()/'.codex-control-mcp')
            token=owner_token(cfg)
            state=json.loads((cfg.home/'state/service.json').read_text('utf-8'))
            report.update(executable=state['executable'],executable_sha256=hashlib.sha256(Path(state['executable']).read_bytes()).hexdigest())
        env,_=build_environment(cfg)
        endpoint=(cfg.oauth['issuer'] if args.public else f'http://127.0.0.1:{cfg.http.get("port",8767)}').rstrip('/')+'/mcp'
        report['endpoint']=endpoint
        transport=SafeConnectTransport(proxy=env.get('HTTPS_PROXY') if args.public else None,events=events)
        async with httpx.AsyncClient(transport=transport,trust_env=False,timeout=75,
                headers={'Authorization':'Bearer '+token}) as http_client:
            async with streamable_http_client(endpoint,http_client=http_client) as (read,write,_):
                async with ClientSession(read,write) as client:
                    init=await client.initialize()
                    check('version',init.serverInfo.version==__version__)
                    tools=await client.list_tools()
                    check('41_tools',len(tools.tools)==41,tool_count=len(tools.tools))
                    async def call(name,values):
                        result=await client.call_tool(name,values)
                        if result.isError or not result.structuredContent['ok']:
                            error=result.structuredContent.get('error') or {}
                            raise RuntimeError(name+': '+str(error.get('code')))
                        for block in result.content:
                            if block.type=='image':
                                data=base64.b64decode(block.data); check('jpeg_screenshot',data.startswith(b'\xff\xd8'))
                                (run/'browser-proof.jpg').write_bytes(data)
                        return result.structuredContent['result']
                    def element(s,name):
                        return next(e['element_index'] for e in s['elements'] if e['name']==name)
                    async def act(name,s,**values):
                        return await call(name,{'page_id':s['page_id'],'snapshot_id':s['snapshot_id'],**values})
                    s=await call('browser_start',{'browser_id':'tabbit','url':page_url})
                    owned_page=s['page_id']
                    report['runtime']=s['runtime']
                    check('actual_tabbit',s['browser_id']=='tabbit' and s['runtime']['official_browser_backend'] is False)
                    s=await act('browser_fill',s,element_index=element(s,'姓名'),text='CCM_TABBIT_HTTP_'+__version__+'_中文')
                    old=s
                    s=await act('browser_click',s,element_index=element(s,'提交'))
                    check('chinese_click_result','结果 CCM_TABBIT_HTTP_'+__version__+'_中文' in s['text'])
                    invalid=await client.call_tool('browser_click',{'page_id':old['page_id'],'snapshot_id':old['snapshot_id'],'element_index':element(old,'提交')})
                    check('stale_snapshot_rejected',invalid.isError and invalid.structuredContent['error']['code']=='stale_snapshot')
                    check('write_not_replayed',counts['posts']==1)
                    s=await act('browser_fill',s,element_index=element(s,'姓名'),text='键盘通过')
                    s=await act('browser_press',s,key='Enter')
                    check('keyboard_submit','结果 键盘通过' in s['text'])
                    s=await act('browser_fill',s,element_index=element(s,'框架输入'),text='iframe中文')
                    check('iframe_fill',any(e['value']=='iframe中文' for e in s['elements']))
                    s=await act('browser_scroll',s,x=1000,y=780,direction='down',pages=1)
                    check('scroll',s['scroll_position']['y']>0)
                    s=await call('browser_navigate',{'page_id':s['page_id'],'url':page_url+'next'})
                    check('navigate',s['url'].endswith('/next'))
                    s=await call('browser_snapshot',{'page_id':s['page_id'],'screenshot':True})
                    if args.public:
                        s=await call('browser_navigate',{'page_id':s['page_id'],'url':'https://example.com/'})
                        check('internet_https_page',s['title']=='Example Domain' and 'Example Domain' in s['text'],url=s['url'])
                    closed=await call('browser_close',{'page_id':s['page_id']})
                    owned_page=None
                    check('eight_actions',closed['closed'] and closed['full_action_chain_verified'],operations=closed['verified_operations'])
                    capabilities=await call('codex_capabilities',{})
                    check('browser_health_verified',capabilities['browser']['full_action_chain_verified'])
        report['pass']=all(c['pass'] for c in report['checks'])
    except BaseException as exc:
        report['pass']=False
        report['error']={'type':type(exc).__name__,'message':str(exc)[:600]}
        leaves=[]
        def collect(error):
            if isinstance(error,BaseExceptionGroup):
                for child in error.exceptions: collect(child)
            else: leaves.append({'type':type(error).__name__,'message':str(error)[:400]})
        collect(exc)
        report['error']['causes']=leaves
    finally:
        if owned_page and not proc:
            try:
                cleanup=await call_running_service(cfg,'browser_close',{'page_id':owned_page})
                report['failed_run_owned_page_closed']=bool(cleanup and cleanup.get('ok'))
            except Exception as cleanup_error:
                report['failed_run_cleanup_error']=type(cleanup_error).__name__
        if proc:
            # Only this fixture's process tree belongs to this job.
            if job: job.Close()
            if proc.poll() is None: proc.wait(8)
            report['owned_service_stopped']=proc.poll() is not None
        fixture.shutdown(); fixture.server_close()
        report['owned_fixture_stopped']=True
        report['connection_recoveries']=sum(bool(a.get('retried')) for row in events for a in row['attempts'])
        report['finished_at']=datetime.now(timezone.utc).isoformat()
        atomic_json(run/'result.json',report); atomic_json(run/'transport.json',{'requests':events})
        print(json.dumps(report,ensure_ascii=True,indent=2))
    return 0 if report['pass'] else 1

if __name__=='__main__': raise SystemExit(asyncio.run(main()))
