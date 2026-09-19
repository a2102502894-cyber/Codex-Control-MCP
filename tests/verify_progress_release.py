"""Opt-in Windows acceptance: loopback SSE, real command, owned CUA cleanup.

Run from the repository root with its Python environment. Does not click/type.
Writes sanitized receipts under evidence/, never screenshots or window titles.
"""
import asyncio
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import uuid

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from codex_control_mcp.config import Config
from codex_control_mcp.lifecycle import create_owned_process_job, request_stop

ROOT = Path(__file__).resolve().parents[1]


def processes():
    ps = 'Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name | ConvertTo-Json -Compress'
    p = subprocess.run(['powershell', '-NoProfile', '-Command', ps], capture_output=True,
                       text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
    p.check_returncode()
    return json.loads(p.stdout)


def descendants(pid, rows):
    ids = {pid}
    while True:
        more = {r['ProcessId'] for r in rows if r['ParentProcessId'] in ids}
        if more <= ids:
            break
        ids |= more
    return [r for r in rows if r['ProcessId'] in ids and r['ProcessId'] != pid]


async def accept(url, token, pid, directory):
    report = {}
    async with httpx.AsyncClient(trust_env=False, timeout=120,
            headers={'Authorization': 'Bearer ' + token}) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, _):
            async with ClientSession(read, write) as client:
                init = await client.initialize()
                listed = await client.list_tools()
                assert any(t.name == 'computer_close' for t in listed.tools)
                report['version'] = init.serverInfo.version
                async def call(name, args):
                    r = await client.call_tool(name, args)
                    assert not r.isError, r.structuredContent
                    return r.structuredContent['result']
                await call('codex_health', {'active': False})
                begin = time.monotonic()
                marker = directory / 'executed-once.txt'
                code = "from pathlib import Path; import time; p=Path(" + repr(str(marker)) + "); p.write_text(p.read_text()+'x' if p.exists() else 'x'); print('START',flush=True); time.sleep(4); print('END',flush=True)"
                args = {'argv': [sys.executable, '-u', '-c', code], 'timeout_ms': 10000,
                        'idempotency_key': 'acceptance-once-' + uuid.uuid4().hex}
                started = await call('exec_command', args)
                report['auto_first_response_seconds'] = round(time.monotonic() - begin, 3)
                assert not started['completed'] and report['auto_first_response_seconds'] < 3
                assert 'START' in started['stdout']
                replay = await call('exec_command', args)
                assert replay['session_id'] == started['session_id']
                cursor = started['next_cursor']; output = started['stdout']
                while True:
                    page = await call('session_read', {'session_id': started['session_id'], 'cursor': cursor})
                    output += page['stdout']; cursor = page['next_cursor']
                    if page['state'] not in ('starting', 'running') and not page['has_more']:
                        assert page['exit_code'] == 0
                        break
                    await asyncio.sleep(0.25)
                assert output.count('START') == 1 and output.count('END') == 1
                assert marker.read_text() == 'x'
                report['single_dispatch_and_cursor'] = True
                progress = []; begin = time.monotonic()
                async def on_progress(value, total, message):
                    progress.append({'seconds': round(time.monotonic()-begin, 3), 'value': value})
                result = await client.call_tool('exec_command', {'argv': [sys.executable, '-c',
                    'import time; time.sleep(6); print("BUFFERED_END")'], 'timeout_ms': 10000,
                    'execution_mode': 'buffered'}, progress_callback=on_progress)
                assert not result.isError and len(progress) >= 2
                assert progress[0]['seconds'] < 2 and progress[-1]['seconds'] < 6
                report['sse_progress'] = progress
                for cycle in range(2):
                    await call('computer_snapshot', {})
                    owned = descendants(pid, processes())
                    cua = [r for r in owned if r['Name'] in ('node_repl.exe', 'codex-computer-use.exe')]
                    assert cua, owned
                    closed = await call('computer_close', {})
                    assert closed['closed']
                    after = processes()
                    remaining = {r['ProcessId'] for r in after} & {r['ProcessId'] for r in cua}
                    assert not remaining, remaining
                    report[f'computer_cycle_{cycle+1}'] = {'owned_cua_processes': len(cua), 'remaining': 0}
                assert (await call('computer_close', {}))['already_closed']
    return report


def main():
    assert os.name == 'nt'
    production = Config.load()
    assert production.auto_approve_application_access, 'Existing owner preapproval required'
    directory = ROOT / 'evidence' / ('progress-release-' + uuid.uuid4().hex[:8])
    directory.mkdir(parents=True)
    home = directory / 'home'; home.mkdir()
    (home/'config.toml').write_text('computer_use_enabled = true\nauto_approve_application_access = true\n', encoding='utf-8')
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]
    env = os.environ.copy(); token = secrets.token_urlsafe(40)
    env['CODEX_CONTROL_MCP_TOKEN'] = token; env['PYTHONPATH'] = str(ROOT/'src')
    log = (directory/'service.log').open('wb')
    p = subprocess.Popen([sys.executable, '-m', 'codex_control_mcp', '--home', str(home),
        'serve', '--transport', 'streamable-http', '--port', str(port)], cwd=ROOT,
        env=env, stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
    job = create_owned_process_job(p._handle)
    report = {'test_scope': 'loopback_real_runtime_no_click_no_type'}
    try:
        deadline = time.monotonic()+20
        while not (home/'state/service.json').exists():
            assert p.poll() is None, 'Candidate exited'
            assert time.monotonic() < deadline, 'Candidate startup timeout'
            time.sleep(0.1)
        report.update(asyncio.run(accept(f'http://127.0.0.1:{port}/mcp', token, p.pid, directory)))
        report['passed'] = True
    finally:
        state = home/'state/service.json'
        if state.exists() and p.poll() is None:
            request_stop(json.loads(state.read_text(encoding='utf-8'))['instance_id'])
            try:
                p.wait(25)
            except subprocess.TimeoutExpired:
                pass
        job.Close(); p.wait(10); log.close()
        report['candidate_exited'] = p.poll() is not None
        (directory/'receipt.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps({'evidence': str(directory/'receipt.json'), **report}), flush=True)


if __name__ == '__main__':
    main()
