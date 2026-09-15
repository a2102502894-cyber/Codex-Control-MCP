"""Package actual evidence for the user-approved Tabbit release scope.

The index is an explicit allowlist. Credentials, browser profiles and deployment
config backups are excluded. Historical or waived checks are never called PASS.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET
import zipfile
from codex_control_mcp import __version__

ROOT = Path(__file__).resolve().parents[1]
EV = ROOT / 'evidence'
TAG = 'v' + __version__.replace('.', '')

def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def load(name):
    return json.loads((EV / name).read_text('utf-8-sig'))

def source_files():
    allowed = {'.py','.cjs','.ps1','.cmd','.md','.txt','.toml','.json'}
    files = []
    for folder in ('src','tests','scripts','docs','THIRD_PARTY_LICENSES'):
        for path in (ROOT / folder).rglob('*'):
            if path.is_symlink():
                raise RuntimeError('Source contains a symbolic link')
            if path.is_file() and '__pycache__' not in path.parts:
                if folder == 'THIRD_PARTY_LICENSES' or path.suffix.lower() in allowed:
                    files.append((path,path.relative_to(ROOT).as_posix()))
    files += [(ROOT/name,name) for name in ('.gitignore','pyproject.toml','requirements-lock.txt',
                'README.md','CODEX_HANDOFF_LATEST.md','THIRD_PARTY_NOTICES.md')]
    return sorted(files,key=lambda row:row[1])

def zip_files(destination, files):
    with zipfile.ZipFile(destination,'w',zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
        for path,name in sorted(files,key=lambda row:row[1]):
            if path.is_symlink() or '..' in Path(name).parts or Path(name).is_absolute():
                raise RuntimeError('Unsafe archive entry')
            archive.write(path,name)
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip() is not None:
            raise RuntimeError('Archive integrity verification failed')

def regressions():
    result = {}
    for name in ('standard-'+TAG,'admin-'+TAG,'packaged-oauth-'+TAG):
        suites = list(ET.parse(EV/(name+'.xml')).getroot().iter('testsuite'))
        counts = {k:sum(int(s.get(k,0)) for s in suites) for k in ('tests','failures','errors','skipped')}
        if not counts['tests'] or counts['errors'] or counts['failures']:
            raise RuntimeError('Regression failed: '+name)
        counts['passed'] = counts['tests']-counts['skipped']
        result[name] = counts
    return result

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    out=parser.parse_args().out.resolve()
    package=out/('Codex-Control-MCP_'+__version__+'_Windows_delivery.zip')
    if package.exists():
        raise RuntimeError('A frozen delivery already exists; never overwrite it')
    index=load('delivery-index-'+TAG+'.json')
    assert index['version']==__version__
    for name,expected in index['files'].items():
        if Path(name).is_absolute() or '..' in Path(name).parts or sha(EV/name)!=expected:
            raise RuntimeError('Evidence mismatch: '+name)
    build=load('windows-build.json')
    core=load('packaged-'+TAG+'.json')
    installed=load('installed-'+__version__+'.json')
    host=load('background-host-'+TAG+'.json')
    lifecycle=load('service-lifecycle-'+TAG+'.json')
    source_check=load('source-runtime-verification-'+TAG+'.json')
    sync=load('source-sync-'+TAG+'.json')
    live=load('final-live-'+TAG+'.json')
    account=load('chatgpt-account-acceptance-'+TAG+'.json')
    account_tools=load('chatgpt-tools-'+TAG+'.json')
    for value,key in ((build,'ok'),(core,'ok'),(installed,'ok'),(host,'pass'),
                      (lifecycle,'pass'),(source_check,'pass'),(sync,'pass'),(live,'pass')):
        assert value.get(key) is True, 'Required release evidence failed'
    install=Path(os.environ['LOCALAPPDATA'])/'Programs/Codex-Control-MCP'/__version__
    assert (build['sha256']==core['sha256']==installed['sha256']==source_check['sha256']
            ==live['executable_sha256']==sha(install/'Codex-Control-MCP.exe'))
    assert build['background_host_sha256']==host['host_sha256']==sha(install/'Codex-Control-MCP-Host.exe')
    assert core['configured_model_request_count']==0 and core['version']['version']==__version__
    assert account['pass'] is True and account['version']==__version__
    assert account['current_model_call_verified'] and account['all_tool_calls_succeeded']
    assert account['verified_tool_sequence']==['codex_health','browser_start','browser_snapshot','browser_close']
    assert account['browser_page_closed'] and account['result']['screenshot_received']
    assert account_tools['pass'] and account_tools['after_count']==37 and account_tools['browser_tools_present']
    assert index['current_account_acceptance']==account
    for name in index['browser_reports']:
        report=load(name)
        actions=next(c for c in report['checks'] if c['name']=='eight_actions')
        assert (report['pass'] and report['version']==__version__ and report['executable_sha256']==build['sha256']
                and report['owned_fixture_stopped'] and actions['pass']
                and set(actions['operations'])=={'start','snapshot','fill','click','press','scroll','navigate','close'}
                and report['runtime']['browser_id']=='tabbit' and report['runtime']['official_browser_backend'] is False)
    assert len(index['public_batches'])>=3
    for name in index['public_batches']:
        report=load(name)
        assert report['pass'] and report['version']==__version__, 'Full public batch failed'
    source=source_files()
    manifest={name:sha(path) for path,name in source}
    assert manifest==sync['file_hashes'], 'Source changed after synchronization'
    assert all(sha(Path(sync['source_root'])/name)==expected for name,expected in manifest.items())
    record={'version':__version__,'prepared_at':datetime.now(timezone.utc).isoformat(),
        'approved_scope':index['approved_scope'],'release_checks_pass':True,'regressions':regressions(),
        'core_sha256':build['sha256'],'background_host_sha256':build['background_host_sha256'],
        'public_full_batches':len(index['public_batches']),'browser_reports':index['browser_reports'],
        'source_files':len(manifest),'source_python_modules':len(source_check['modules']),
        'source_worker_matches_exe':source_check['tabbit_worker']['equal_bytes'],
        'current_account_acceptance':index['current_account_acceptance'],'historical_scope':index['historical_scope'],
        'waived_by_user':index['waived_by_user'],'clash_modified':False,'system_network_modified':False,
        'code_signed':False,'official_runtime_bundled':False,'tabbit_browser_bundled':False}
    out.mkdir(parents=True,exist_ok=True)
    for name in ('Codex-Control-MCP.exe','Codex-Control-MCP-Host.exe'):
        shutil.copyfile(install/name,out/name)
        assert sha(install/name)==sha(out/name)
    for name in index['files']:
        target=out/'evidence'/name
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(EV/name,target)
    shutil.copyfile(EV/('delivery-index-'+TAG+'.json'),out/'evidence'/('delivery-index-'+TAG+'.json'))
    (out/'evidence'/('RELEASE_STATUS_'+__version__+'.json')).write_text(json.dumps(record,ensure_ascii=False,indent=2),'utf-8')
    for name in ('Start-Admin.cmd','Check.cmd','Stop.cmd','Connect-ChatGPT.cmd'):
        shutil.copyfile(ROOT/'scripts'/name,out/name)
    for name in ('README.md','CODEX_HANDOFF_LATEST.md','THIRD_PARTY_NOTICES.md','requirements-lock.txt'):
        shutil.copyfile(ROOT/name,out/name)
    shutil.copyfile(ROOT/'docs'/('交付验收_'+__version__+'.md'),out/('交付验收_'+__version__+'.md'))
    (out/'SOURCE_MANIFEST.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),'utf-8')
    zip_files(out/('Codex-Control-MCP_'+__version__+'_source.zip'),source+[(out/'SOURCE_MANIFEST.json','SOURCE_MANIFEST.json')])
    payload=[(p,p.relative_to(out).as_posix()) for p in out.rglob('*') if p.is_file() and p not in (package,out/'SHA256SUMS.txt')]
    (out/'SHA256SUMS.txt').write_text(''.join(f'{sha(p)}  {name}\n' for p,name in sorted(payload,key=lambda row:row[1])),'utf-8')
    zip_files(package,payload+[(out/'SHA256SUMS.txt','SHA256SUMS.txt')])
    with (out/'SHA256SUMS.txt').open('a',encoding='utf-8') as stream:
        stream.write(f'{sha(package)}  {package.name}\n')
    print(json.dumps({'package':str(package),'package_sha256':sha(package),'source_files':len(manifest),'release_checks_pass':True},ensure_ascii=True))
    return 0

if __name__=='__main__':
    raise SystemExit(main())
