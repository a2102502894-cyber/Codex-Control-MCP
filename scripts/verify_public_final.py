"""Public read-back verification; never reauthorizes or logs tokens."""
import datetime,importlib.metadata,json,pathlib
import httpx
from production_acceptance import Client
root=pathlib.Path(__file__).resolve().parents[1];run=root/'evidence/public-final-20260910';c=Client(run,True)
s=httpx.Client(trust_env=False,timeout=30)
meta=s.get(c.base+'/.well-known/oauth-authorization-server')
challenge=s.post(c.base+'/mcp',json={'jsonrpc':'2.0','id':1,'method':'tools/list'})
bad=s.post(c.base+'/mcp',headers={'Authorization':'Bearer not-a-valid-token'},json={'jsonrpc':'2.0','id':1,'method':'tools/list'})
assert meta.status_code==200 and challenge.status_code==bad.status_code==401
assert meta.json()['issuer'].rstrip('/')==c.base
r=c.call('mcp_tool_call',{'name':'director:director_read','arguments':{}})
nested=r['result'];body=nested.get('structuredContent') or json.loads(nested['content'][0]['text']);assert not nested.get('isError') and body.get('ok') is True
m=c.call('mcp_manage',{'action':'list'})['servers']
assert {x['name']:(x['status'],x['tool_count']) for x in m}=={'director':('ready',17),'grok':('ready',3)}
direct=json.loads(importlib.metadata.distribution('codex-control-mcp').read_text('direct_url.json'))
assert direct['dir_info']['editable'] is True
state=pathlib.Path.home()/'.codex-control-mcp/state'
svc=json.loads((state/'service.json').read_text('utf-8-sig'));tunnel=json.loads((state/'tunnel-service.json').read_text('utf-8-sig'))
report={'verified_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'endpoint':c.base+'/mcp','server':'Codex-Control-MCP','version':'0.2.0','tool_count':len(c.schemas),'static_grok':0,'static_director':0,'oauth_metadata_http':meta.status_code,'unauthenticated_mcp_http':challenge.status_code,'wrong_bearer_http':bad.status_code,'owner_bearer_initialize_verified':True,'existing_oauth_tokens_reissued':False,'oauth_browser_grant_flow_rerun':False,'director_read_verified':True,'dynamic_mcp':{x['name']:{'status':x['status'],'tool_count':x['tool_count']} for x in m},'editable_install':True,'core_pid':svc['pid'],'tunnel_parent_pid':tunnel['pid'],'cloudflared_pid':tunnel['child_pid'],'packaging_required':False}
(run/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False,indent=2))
