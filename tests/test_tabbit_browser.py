"""Real installed Tabbit tests, using only fresh project-owned profiles."""
import base64
import http.server
import json
from pathlib import Path
import threading
import time
import uuid
import pytest
from codex_control_mcp.bridge import Bridge
from codex_control_mcp.config import Config

TABBIT = Path(r'C:\Program Files\Tabbit\Application\Tabbit Browser.exe')
pytestmark = pytest.mark.skipif(not TABBIT.is_file(), reason='Installed Tabbit is required')

HTML = '''<!doctype html><meta charset="utf-8"><title>CCM Tabbit fixture</title>
<style>body{font:20px sans-serif}input,button{font:inherit;margin:8px}#scroller{height:180px;overflow:auto;border:1px solid}#inside{height:1400px}</style>
<h1>独立 Tabbit 验收</h1><form onsubmit="event.preventDefault();document.querySelector('output').textContent='结果 '+document.querySelector('#name').value;fetch('/record')">
<label for="name">姓名</label><input id="name"><button>提交</button></form><output></output>
<button onclick="setTimeout(()=>document.querySelector('#name').outerHTML='<input id=name>',700)">替换输入框</button>
<button onclick="window.open('/popup')">打开测试页</button>
<iframe title="子页面" src="/frame"></iframe>
<div id="scroller" tabindex="0" aria-label="滚动区域"><div id="inside">内部滚动内容</div></div>
<div style="height:2200px"></div><p>页面底部</p>'''

@pytest.fixture
def browser(tmp_path):
    count = {'posts': 0}
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/record':
                count['posts'] += 1
            body = ('<label for="f">框架输入</label><input id="f">' if self.path=='/frame'
                else '<title>CCM popup</title><h1>弹出页</h1>' if self.path=='/popup' else HTML).encode('utf-8')
            self.send_response(200); self.send_header('Content-Type','text/html;charset=utf-8')
            self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
        def log_message(self, *_):
            pass
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    cfg=Config(home=tmp_path/'home',cwd=str(tmp_path),browser_use_enabled=True,browser={
        'backend':'tabbit','executable_path':str(TABBIT),
        'profile_directory':str(Path.home()/'.agentdock/browser/profiles'/('ccm-test-'+uuid.uuid4().hex[:10])),
        'max_pages':4, 'timeout_ms':5000})
    bridge=Bridge(cfg)
    try:
        yield bridge, f'http://127.0.0.1:{server.server_port}', count
    finally:
        proc=bridge.browser.proc if bridge.browser else None
        bridge.close(); server.shutdown(); server.server_close()
        if proc: assert proc.poll() is not None

def call(bridge,name,**args):
    out=bridge.execute(name,args)
    assert out['ok'], json.dumps(out, ensure_ascii=True)
    return out['result']

def index(snapshot,name):
    rows=[e for e in snapshot['elements'] if e['name']==name]
    assert len(rows)==1, (name,snapshot['elements'])
    return rows[0]['element_index']

def action(bridge,name,snapshot,**args):
    return call(bridge,name,page_id=snapshot['page_id'],snapshot_id=snapshot['snapshot_id'],**args)

def test_all_eight_actions_and_no_replayed_side_effect(browser):
    bridge,url,count=browser
    snapshot=call(bridge,'browser_start',url=url)
    assert snapshot['browser_id']=='tabbit'
    assert snapshot['runtime']['official_browser_backend'] is False
    assert snapshot['runtime']['browser_version']=='150.0.7871.129' or snapshot['runtime']['browser_version']
    snapshot=action(bridge,'browser_fill',snapshot,element_index=index(snapshot,'姓名'),text='CCM_TABBIT_中文')
    previous=snapshot
    snapshot=action(bridge,'browser_click',snapshot,element_index=index(snapshot,'提交'))
    assert '结果 CCM_TABBIT_中文' in snapshot['text']
    rejected=bridge.execute('browser_click',{'page_id':previous['page_id'],'snapshot_id':previous['snapshot_id'],'element_index':index(previous,'提交')})
    assert rejected['error']['code']=='stale_snapshot'
    assert count['posts']==1
    snapshot=action(bridge,'browser_fill',snapshot,element_index=index(snapshot,'姓名'),text='键盘验证')
    snapshot=action(bridge,'browser_press',snapshot,key='Enter')
    assert '结果 键盘验证' in snapshot['text'] and count['posts']==2
    snapshot=action(bridge,'browser_fill',snapshot,element_index=index(snapshot,'框架输入'),text='iframe中文')
    assert 'iframe中文' in snapshot['text'] or any(e['value']=='iframe中文' for e in snapshot['elements'])
    snapshot=action(bridge,'browser_scroll',snapshot,element_index=index(snapshot,'滚动区域'),direction='down',pages=0.5)
    assert next(e for e in snapshot['elements'] if e['name']=='滚动区域')['scroll_y']>0
    snapshot=action(bridge,'browser_scroll',snapshot,x=1000,y=780,direction='down',pages=1)
    assert snapshot['scroll_position']['y']>0
    snapshot=call(bridge,'browser_navigate',page_id=snapshot['page_id'],url=url+'/next')
    image=bridge.execute('browser_snapshot',{'page_id':snapshot['page_id'],'screenshot':True})
    assert image['ok'] and base64.b64decode(image['_image_blocks'][0]['data']).startswith(b'\xff\xd8')
    closed=call(bridge,'browser_close',page_id=snapshot['page_id'])
    assert closed['closed'] and closed['full_action_chain_verified']
    assert set(closed['verified_operations'])=={'start','snapshot','click','fill','press','scroll','navigate','close'}
    assert bridge.rpc is None  # Browser did not start a Codex model or execution server.

def test_detached_observation_and_page_ownership(browser):
    bridge,url,_=browser
    one=call(bridge,'browser_start',url=url)
    one=action(bridge,'browser_click',one,element_index=index(one,'替换输入框'))
    old_index=index(one,'姓名')
    time.sleep(0.9)
    stale=bridge.execute('browser_fill',{'page_id':one['page_id'],'snapshot_id':one['snapshot_id'],'element_index':old_index,'text':'must not type'})
    assert stale['error']['code']=='stale_snapshot'
    two=call(bridge,'browser_start',url=url)
    wrong=bridge.execute('browser_fill',{'page_id':two['page_id'],'snapshot_id':one['snapshot_id'],'element_index':0,'text':'must not type'})
    assert wrong['error']['code']=='stale_snapshot'
    assert bridge.execute('browser_close',{'page_id':'not-owned'})['error']['code']=='page_not_owned'
    assert bridge.execute('browser_navigate',{'page_id':two['page_id'],'url':'javascript:alert(1)'})['error']['code']=='invalid_arguments'
    call(bridge,'browser_close',page_id=one['page_id'])
    call(bridge,'browser_close',page_id=two['page_id'])

def test_popup_and_coordinate_actions(browser):
    bridge,url,_=browser
    snapshot=call(bridge,'browser_start',url=url)
    button=next(e for e in snapshot['elements'] if e['name']=='打开测试页')
    box=button['box']
    snapshot=action(bridge,'browser_click',snapshot,x=box['x']+box['width']/2,y=box['y']+box['height']/2)
    snapshot=call(bridge,'browser_snapshot',page_id=snapshot['page_id'])
    popup=next(p for p in snapshot['owned_pages'] if p['page_id']!=snapshot['page_id'])
    pop=call(bridge,'browser_snapshot',page_id=popup['page_id'])
    assert '弹出页' in pop['text']
    call(bridge,'browser_close',page_id=popup['page_id'])
    assert call(bridge,'browser_snapshot',page_id=snapshot['page_id'])['title']=='CCM Tabbit fixture'
    call(bridge,'browser_close',page_id=snapshot['page_id'])

def test_nonowned_profile_is_rejected(tmp_path):
    profile=tmp_path/'existing-profile'; profile.mkdir(); (profile/'Preferences').write_text('{}')
    cfg=Config(home=tmp_path/'home',cwd=str(tmp_path),browser_use_enabled=True,browser={
        'backend':'tabbit','profile_directory':str(profile)})
    bridge=Bridge(cfg)
    try:
        out=bridge.execute('browser_start',{})
        assert out['error']['code']=='invalid_config'
        assert not (profile/'.codex-control-profile.json').exists()
    finally:
        bridge.close()
