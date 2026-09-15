"""Real production MCP acceptance client. No fake clock/backend or POST retry.
Owns only tagged local fixtures; credentials remain in memory.
"""
from __future__ import annotations
import argparse, base64, datetime, http.server, json, os, pathlib, subprocess, sys, threading, uuid
import httpx
from codex_control_mcp.auth import owner_token
from codex_control_mcp.config import Config
from codex_control_mcp.common import atomic_json

ROOT = pathlib.Path(__file__).resolve().parents[1]

class Client:
    def __init__(self, run, public=False):
        self.run = pathlib.Path(run); self.run.mkdir(parents=True, exist_ok=True)
        self.cfg = Config.load()
        self.base = self.cfg.oauth['issuer'].rstrip('/') if public else 'http://127.0.0.1:8774'
        self.s = httpx.Client(trust_env=False)
        self.s.headers.update({'Authorization':'Bearer '+owner_token(self.cfg), 'Accept':'application/json, text/event-stream'})
        self.i = 0
        init = self.rpc('initialize', {'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'production-acceptance','version':'1'}})
        self.s.headers['MCP-Protocol-Version'] = init['protocolVersion']
        r=self.s.post(self.base+'/mcp',json={'jsonrpc':'2.0','method':'notifications/initialized'},timeout=30);r.raise_for_status()
        self.schemas = {t['name']: t['inputSchema'] for t in self.rpc('tools/list',{})['tools']}
        assert init['serverInfo']=={'name':'Codex-Control-MCP','version':'0.2.0'}
        assert len(self.schemas)==47 and not any(n.startswith(('grok_','director_')) for n in self.schemas)
    def rpc(self, method, params):
        self.i+=1
        r=self.s.post(self.base+'/mcp',json={'jsonrpc':'2.0','id':self.i,'method':method,'params':params},timeout=240)
        r.raise_for_status();r.encoding='utf-8'
        if r.headers.get('Mcp-Session-Id'):self.s.headers['Mcp-Session-Id']=r.headers['Mcp-Session-Id']
        if 'text/event-stream' in r.headers.get('Content-Type',''):
            d=next(json.loads(x[5:]) for x in r.text.splitlines() if x.startswith('data:') and json.loads(x[5:]).get('id')==self.i)
        else:d=r.json()
        if 'error' in d:raise RuntimeError(str(d['error']))
        return d['result']
    def call(self,name,args):
        import jsonschema
        jsonschema.validate(args,self.schemas[name])
        stamp=datetime.datetime.now(datetime.timezone.utc).isoformat()
        d=self.rpc('tools/call',{'name':name,'arguments':args})
        result=d.get('structuredContent')
        if result is None:result=next(json.loads(x['text']) for x in d.get('content',[]) if x.get('type')=='text')
        images=[]
        for x in d.get('content',[]):
            if x.get('type')=='image':
                p=self.run/(name+'-'+uuid.uuid4().hex[:8]+('.png' if x.get('mimeType')=='image/png' else '.jpg'))
                p.write_bytes(base64.b64decode(x['data']));images.append(str(p))
        record={'at':stamp,'endpoint':self.base,'name':name,'arguments':args,'response':result,'images':images}
        with (self.run/'calls.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(record,ensure_ascii=False)+'\n')
        atomic_json(self.run/'last-call.json',record)
        if d.get('isError') or not result.get('ok'):raise RuntimeError(json.dumps(result.get('error'),ensure_ascii=False))
        return result['result']


def browser_e2e(c):
    sys.path.insert(0,str(ROOT/'tests'))
    from test_tabbit_browser import HTML
    counts={'writes':0}
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path=='/record':counts['writes']+=1
            body=('<label for="f">框架输入</label><input id="f">' if self.path=='/frame' else HTML).encode('utf-8')
            self.send_response(200);self.send_header('Content-Type','text/html;charset=utf-8');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def log_message(self,*a):pass
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    url=f'http://127.0.0.1:{server.server_port}/';page=None
    report={'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'endpoint':c.base,'fixture_url':url,'pass':False}
    def element(s,name):return next(x['element_index'] for x in s['elements'] if x['name']==name)
    def act(name,s,**args):return c.call(name,dict(page_id=s['page_id'],snapshot_id=s['snapshot_id'],**args))
    try:
        s=c.call('browser_start',{'url':url});page=s['page_id']
        s=act('browser_fill',s,element_index=element(s,'姓名'),text='CCM_FINAL_中文')
        s=act('browser_click',s,element_index=element(s,'提交'))
        assert '结果 CCM_FINAL_中文' in s['text']
        s=act('browser_fill',s,element_index=element(s,'姓名'),text='CCM_FINAL_ENTER')
        s=act('browser_press',s,key='Enter');assert '结果 CCM_FINAL_ENTER' in s['text']
        s=act('browser_scroll',s,element_index=element(s,'滚动区域'),direction='down',pages=0.5)
        assert next(x for x in s['elements'] if x['name']=='滚动区域')['scroll_y']>0
        s=c.call('browser_navigate',{'page_id':page,'url':url+'next'});assert s['url'].endswith('/next')
        s=c.call('browser_snapshot',{'page_id':page,'screenshot':True})
        closed=c.call('browser_close',{'page_id':page});page=None
        assert closed['closed'] and closed['full_action_chain_verified']
        assert set(closed['verified_operations'])=={'start','snapshot','fill','click','press','scroll','navigate','close'}
        caps=c.call('codex_capabilities',{})
        assert caps['browser']['full_action_chain_verified'] and counts['writes']==2
        report.update(pass_=True,operations=closed['verified_operations'],browser=caps['browser'],observed_writes=counts['writes'])
        report['pass']=True;report.pop('pass_',None)
    except BaseException as exc:
        report['error']=type(exc).__name__+': '+str(exc);raise
    finally:
        if page:
            try:report['page_cleanup']=c.call('browser_close',{'page_id':page})
            except Exception as exc:report['cleanup_error']=str(exc)
        server.shutdown();server.server_close()
        report['fixture_server_closed']=True
        report['finished_at']=datetime.datetime.now(datetime.timezone.utc).isoformat()
        atomic_json(c.run/'browser-e2e.json',report)
    return report


def gui_start(c):
    exe=ROOT/'test-workspace/CodexControlGuiFixture.exe'
    compiler=pathlib.Path(os.environ['WINDIR'])/'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    subprocess.run([str(compiler),'/nologo','/target:winexe','/reference:System.Windows.Forms.dll','/reference:System.Drawing.dll','/out:'+str(exe),str(ROOT/'tests/GuiFixture.cs')],check=True,creationflags=subprocess.CREATE_NO_WINDOW)
    title='CCM FINAL ISOLATED '+uuid.uuid4().hex[:8]
    proof=(c.run/'gui-proof.txt').resolve()
    task_name='CCM-Isolated-GUI-'+uuid.uuid4().hex[:8]
    subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(ROOT/'scripts/Start-Isolated-GUI.ps1'),'-Exe',str(exe),'-Title',title,'-Proof',str(proof),'-TaskName',task_name],check=True,creationflags=subprocess.CREATE_NO_WINDOW)
    import time,win32gui,win32process
    deadline=time.monotonic()+10
    while True:
        hwnd=win32gui.FindWindow(None,title)
        if hwnd:break
        if time.monotonic()>deadline:raise RuntimeError('owned fixture did not show: '+task_name)
        time.sleep(.25)
    record={'pid':win32process.GetWindowThreadProcessId(hwnd)[1],'title':title,'exe':str(exe),'proof_path':str(proof),'task_name':task_name,'run_level':'Limited'}
    atomic_json(c.run/'gui-owned.json',record)
    windows=c.call('computer_snapshot',{})['windows']
    own=[w for w in windows if w['title']==title]
    assert len(own)==1,'Fixture not uniquely observed'
    record['window_id']=own[0]['id'];atomic_json(c.run/'gui-owned.json',record)
    snap=c.call('computer_snapshot',{'window_id':record['window_id']})
    return {'owned':record,'snapshot':snap}


def gui_finish(c):
    import win32api, win32event, win32gui, win32process
    r=json.loads((c.run/'gui-owned.json').read_text('utf-8'))
    assert win32gui.GetWindowText(r['window_id'])==r['title']
    assert win32process.GetWindowThreadProcessId(r['window_id'])[1]==r['pid']
    p=win32api.OpenProcess(0x100001,False,r['pid'])
    proof=pathlib.Path(r['proof_path']);text=proof.read_text('utf-8') if proof.exists() else None
    caps=c.call('codex_capabilities',{})
    win32api.TerminateProcess(p,0)
    stopped=win32event.WaitForSingleObject(p,10000)==0
    p.Close()
    if r.get('task_name'):
        assert r['task_name'].startswith('CCM-Isolated-GUI-')
        subprocess.run(['powershell.exe','-NoProfile','-Command',"Unregister-ScheduledTask -TaskName '"+r['task_name']+"' -Confirm:$false"],check=True,creationflags=subprocess.CREATE_NO_WINDOW)
    report={'pass':text=='CCM_FINAL_GUI_中文' and caps['computer_use']['full_action_chain_verified'],
            'proof_content':text,'computer_use':caps['computer_use'],'fixture_stopped':stopped,
            'finished_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
    if proof.exists():proof.unlink()
    report['proof_file_removed']=not proof.exists()
    atomic_json(c.run/'computer-e2e.json',report)
    return report


def main():
    p=argparse.ArgumentParser();p.add_argument('--run',required=True);p.add_argument('--public',action='store_true');p.add_argument('action',choices=['call','browser-e2e','gui-start','gui-finish']);p.add_argument('name',nargs='?');p.add_argument('arguments',nargs='?',default='{}');a=p.parse_args()
    c=Client(a.run,a.public)
    if a.action=='call':out=c.call(a.name,json.loads(a.arguments))
    elif a.action=='browser-e2e':out=browser_e2e(c)
    elif a.action=='gui-start':out=gui_start(c)
    else:out=gui_finish(c)
    print(json.dumps(out,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
