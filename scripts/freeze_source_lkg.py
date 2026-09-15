"""Freeze verified source plus recovery scripts; never package or include credentials."""
from __future__ import annotations
import argparse, datetime, hashlib, json, os, pathlib, shutil, subprocess, sys, tempfile
ROOT=pathlib.Path(__file__).resolve().parents[1]
RECOVERY_FILES=['core_recovery_controller.py','Core-Source-Host.ps1','Request-Core-Restart.ps1','Install-Core-RecoveryTasks.ps1','Service-Watchdog.ps1']

def tree_manifest(root):
    rows=[]
    for p in sorted(root.rglob('*')):
        if p.is_file() and p.name!='manifest.json' and '__pycache__' not in p.parts:
            rows.append({'path':p.relative_to(root).as_posix(),'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
    digest=hashlib.sha256(json.dumps(rows,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')).hexdigest()
    return rows,digest

PROBE=r'''
import json,sys,pathlib,tomllib
root=pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0,str(root/'src'))
import codex_control_mcp as pkg
from codex_control_mcp.tools import TOOL_SPECS
from codex_control_mcp.config import Config,DEFAULT_CONFIG
from codex_control_mcp.bridge import Bridge
from codex_control_mcp.computer import OfficialComputer
from codex_control_mcp.proxy_probe import route_is_verified
origin=pathlib.Path(pkg.__file__).resolve()
assert origin.is_relative_to(root/'src'),origin
c=Config(home=root/'probe-unused-home',cwd=str(root))
out={'version':pkg.__version__,'core_tools':len(TOOL_SPECS),
     'static_grok_tools':sum(n.startswith('grok_') for n in TOOL_SPECS),
     'static_director_tools':sum(n.startswith('director_') for n in TOOL_SPECS),
     'default_port':tomllib.loads(DEFAULT_CONFIG)['http']['port'],'module_path':str(origin),'isolated_import':True}
assert out['version']=='0.2.0' and out['core_tools']==47
assert out['static_grok_tools']==out['static_director_tools']==0
assert out['default_port']==8774
print(json.dumps(out,ensure_ascii=False))
'''

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--home',type=pathlib.Path,default=pathlib.Path.home()/'.codex-control-mcp');parser.add_argument('--tests-json',type=pathlib.Path,required=True);parser.add_argument('--activate',action='store_true');a=parser.parse_args()
    tests=json.loads(a.tests_json.read_text('utf-8'))
    assert tests.get('all_passed') is True,'Final test evidence must be passing'
    if a.activate:
        assert tests.get('full_stack_accepted') is True,'Do not replace LKG while required production acceptance remains incomplete'
    state=a.home/'state';stamp=datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    staging=state/('lkg-0.2.0.candidate-'+stamp);staging.mkdir()
    shutil.copytree(ROOT/'src/codex_control_mcp',staging/'src/codex_control_mcp',ignore=shutil.ignore_patterns('__pycache__','*.pyc','*.bak'))
    (staging/'scripts').mkdir()
    for name in RECOVERY_FILES:shutil.copy2(ROOT/'scripts'/name,staging/'scripts'/name)
    env=os.environ.copy();env['PYTHONPATH']=str(staging/'src');env['PYTHONIOENCODING']='utf-8';env['PYTHONDONTWRITEBYTECODE']='1'
    checked=subprocess.run([sys.executable,'-I','-X','utf8','-c',PROBE,str(staging)],cwd=staging,env=env,capture_output=True,text=True,encoding='utf-8',timeout=40)
    if checked.returncode:raise RuntimeError(checked.stderr)
    validation=json.loads(checked.stdout);rows,digest=tree_manifest(staging)
    manifest={'version':'0.2.0','created_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'source':str(ROOT/'src/codex_control_mcp'),
              'sha256':digest,'sha256_algorithm':'SHA256(canonical UTF8 JSON ordered file path/bytes/SHA256 rows; excludes manifest and pycache)',
              'file_count':len(rows),'files':rows,'validation':validation,'tests':tests,'packaging_required':False}
    target=state/'lkg-0.2.0';previous=None
    if a.activate:
        if target.exists():
            previous=state/('lkg-0.2.0.previous-'+stamp);target.rename(previous)
        try:staging.rename(target)
        except BaseException:
            if previous and not target.exists():previous.rename(target)
            raise
        staging=target
        final=subprocess.run([sys.executable,'-I','-X','utf8','-c',PROBE,str(target)],cwd=target,env=env,capture_output=True,text=True,encoding='utf-8',timeout=40)
        if final.returncode:raise RuntimeError(final.stderr)
        manifest['validation']=json.loads(final.stdout)
    manifest['activated']=a.activate;manifest['previous_snapshot']=str(previous) if previous else None
    (staging/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    assert tree_manifest(staging)[1]==digest
    print(json.dumps({'path':str(staging),'sha256':digest,'file_count':len(rows),'validation':manifest['validation'],'activated':a.activate,'previous':manifest['previous_snapshot']},ensure_ascii=False,indent=2))
if __name__=='__main__':main()
