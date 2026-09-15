"""Narrow cleanup of identified disposable acceptance artifacts only."""
import datetime,json,pathlib,psutil,shutil,winreg
ROOT=pathlib.Path('D:/AgentDock工作区/Codex-Control-MCP');state=pathlib.Path.home()/'.codex-control-mcp/state'
targets=[ROOT/'test-workspace/ccm-prodconfig-smoke-gm5oxw7q',ROOT/'test-workspace/ccm-source-smoke-fky8inwn',ROOT/'test-workspace/proof-76ef7d3b845e42b68f97b749b07cfa14.txt',state/'lkg-0.2.0.candidate-20260910-194235']
assert not (targets[-1]/'manifest.json').exists()
report={'recorded_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'removed':[],'retained_in_use':[]}
for path in targets:
 holders=[]
 for p in psutil.process_iter(['pid','name']):
  try:
   if p.cwd().casefold().startswith(str(path).casefold()) or any(str(x).strip('"').casefold().startswith(str(path).casefold()) for x in p.cmdline()):holders.append(p.info)
  except (psutil.Error,OSError):pass
 if holders:report['retained_in_use'].append({'path':str(path),'holders':holders});continue
 if path.exists():
  if path.is_dir():shutil.rmtree(path)
  else:path.unlink()
 assert not path.exists();report['removed'].append(str(path))
patterns=['core-takeover-controller.*','tunnel-switch-controller.*','restart-https-ipv4.*','service-watchdog.pre-conservative.ps1']
report['active_migration_residuals']=[str(p) for r in (ROOT,state) for pattern in patterns for p in r.glob(pattern)]
legacy=state/'director-desk-gateway'
report['legacy_gateway']={'exists':legacy.exists(),'empty':legacy.exists() and not any(legacy.iterdir()),'holders':[]}
for p in psutil.process_iter(['pid','name']):
 try:
  if p.cwd().casefold()==str(legacy).casefold():report['legacy_gateway']['holders'].append(p.info)
 except (psutil.Error,OSError):pass
try:
 with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,r'SYSTEM\CurrentControlSet\Control\Session Manager') as k:ops=winreg.QueryValueEx(k,'PendingFileRenameOperations')[0]
 report['legacy_gateway']['pending_reboot_delete']=any('director-desk-gateway' in str(x) for x in ops)
except OSError:report['legacy_gateway']['pending_reboot_delete']=False
report['gui_fixture_processes']=[p.info for p in psutil.process_iter(['pid','name']) if p.info['name']=='CodexControlGuiFixture.exe']
report['historical_regression_directories_preserved']=True
report['current_reference_clone_in_project']=any(p.is_dir() and ('reference' in p.name.casefold() or p.name=='.refs') for p in ROOT.iterdir())
(ROOT/'evidence/cleanup-final.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False,indent=2))
