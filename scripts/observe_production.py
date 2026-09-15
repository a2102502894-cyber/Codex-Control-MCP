"""Read-only real-time stability observer; never starts/stops services."""
import argparse, datetime, json, pathlib, socket, time
import psutil, requests
p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--seconds',type=int,default=360);a=p.parse_args()
out=pathlib.Path(a.output);out.parent.mkdir(parents=True,exist_ok=True)
state=pathlib.Path.home()/'.codex-control-mcp/state'
s=requests.Session();s.trust_env=False

def load(n):
 try:return json.loads((state/n).read_text('utf-8-sig'))
 except Exception:return {}

def sample():
 r={'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'listeners':{}}
 for c in psutil.net_connections(kind='tcp'):
  if c.status=='LISTEN' and c.laddr.port in (8774,8771,8767):r['listeners'][str(c.laddr.port)]=c.pid
 t=load('tunnel-service.json');r['tunnel_parent_pid']=t.get('pid');r['cloudflared_pid']=t.get('child_pid')
 r['cloudflared_alive']=psutil.pid_exists(t.get('child_pid',0));r['ha_connections']=None
 if r['cloudflared_alive']:
  for c in psutil.Process(t['child_pid']).net_connections(kind='tcp'):
   if c.status!='LISTEN' or c.laddr.ip not in ('127.0.0.1','::1'):continue
   try:
    text=s.get(f'http://127.0.0.1:{c.laddr.port}/metrics',timeout=3).text
    line=next(x for x in text.splitlines() if x.startswith('cloudflared_tunnel_ha_connections '))
    r['ha_connections']=int(float(line.rsplit(' ',1)[1]));break
   except Exception:continue
 try:r['public_unauth_http']=s.post('https://codex-control.aiwsb.site/mcp',timeout=8,json={'jsonrpc':'2.0','id':1,'method':'tools/list'}).status_code
 except Exception as e:r['public_error']=type(e).__name__
 r['maintenance_record_exists']=(state/'maintenance.lock').exists()
 r['core_fail_count']=load('watchdog-core-failures.json').get('count',0)
 report=load('core-controller-report.json');r['last_controller']={k:report.get(k) for k in ('started_at','operation','result','selected','new_pid','exit_code')}
 return r

end=time.monotonic()+a.seconds
with out.open('w',encoding='utf-8') as f:
 while True:
  r=sample();f.write(json.dumps(r,ensure_ascii=False)+'\n');f.flush()
  if time.monotonic()>=end:break
  time.sleep(min(15,max(0,end-time.monotonic())))
rows=[json.loads(x) for x in out.read_text('utf-8').splitlines()]
summary={'samples':len(rows),'start':rows[0]['at'],'end':rows[-1]['at'],'core_pids':sorted({x['listeners'].get('8774') for x in rows if x['listeners'].get('8774')}),'core_down_observed':any('8774' not in x['listeners'] for x in rows),'final':rows[-1],'tunnel_pids':sorted({x['cloudflared_pid'] for x in rows}),'connections_all_four':all(x['ha_connections']==4 for x in rows),'director_pids':sorted({x['listeners'].get('8771') for x in rows if x['listeners'].get('8771')}),'8767_always_absent':all('8767' not in x['listeners'] for x in rows)}
out.with_suffix('.summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(summary,ensure_ascii=False))
