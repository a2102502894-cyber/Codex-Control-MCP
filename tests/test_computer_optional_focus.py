"""Adapter-only JS contract tests; not GUI production acceptance."""
import json
import shutil
import subprocess
import threading
from codex_control_mcp.computer import OfficialComputer


def test_optional_focus_requires_current_window_click_and_fresh_snapshot():
    obj = OfficialComputer.__new__(OfficialComputer)
    obj.action_lock = threading.RLock()
    obj.verified_operations = set()
    obj.verified = False
    codes = []
    def capture(code, forwarder):
        codes.append(code)
        req = json.loads(code.split('const req=', 1)[1].split(';\n', 1)[0])
        return {'snapshot_id': req['snapshot_id']}
    obj._call = capture
    def call(name, **args):return obj.call(name, args)['snapshot_id']
    s = call('computer_snapshot', window_id=1)
    call('computer_type', snapshot_id=s, text='blocked without focus')
    s = call('computer_snapshot', window_id=1)
    clicked = call('computer_click', snapshot_id=s, x=10, y=10)
    call('computer_type', snapshot_id=clicked, text='allowed after explicit click')
    call('computer_type', snapshot_id=clicked, text='blocked replay')
    s = call('computer_snapshot', window_id=1)
    clicked = call('computer_click', snapshot_id=s, x=10, y=10)
    s2 = call('computer_snapshot', window_id=2)
    call('computer_type', snapshot_id=clicked, text='blocked cross-window old snapshot')
    call('computer_type', snapshot_id=s2, text='blocked unrelated snapshot')
    program = '''
const output=[],typed=[];
globalThis.nodeRepl={write:x=>output.push(JSON.parse(x.slice('CCM_RESULT='.length)))};
globalThis.ccmObservations=new Map();
globalThis.ccmSky={
 list_windows:async()=>[{id:1,app:'unit'},{id:2,app:'unit'}],
 get_window_state:async({window})=>({window,accessibility:{tree:'unit test only'},screenshots:[{id:'unit'}]}),
 click:async()=>{},type_text:async({window,text})=>typed.push({window,text})
};
(async()=>{const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;for(const code of CODES){await new AsyncFunction(code)();}console.log(JSON.stringify({output,typed}));})().catch(e=>{console.error(e);process.exit(1)});
'''.replace('CODES', json.dumps(codes))
    result = subprocess.run([shutil.which('node'), '-'], input=program, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data['typed'] == [{'window': {'id': 1, 'app': 'unit'}, 'text': 'allowed after explicit click'}]
    errors = [r['error_code'] for r in data['output'] if 'error_code' in r]
    assert errors == ['invalid_arguments', 'stale_snapshot', 'stale_snapshot', 'invalid_arguments']
