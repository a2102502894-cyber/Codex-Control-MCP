"""Two complete cycles of the packaged GUI-subsystem service host, no UI input."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import uuid

import httpx
import pefile
import win32gui
import win32process
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from codex_control_mcp.auth import owner_token
from codex_control_mcp.common import CREATE_NO_WINDOW, ps_argv
from codex_control_mcp.config import Config, DEFAULT_CONFIG

ROOT = Path(__file__).resolve().parents[1]
HOST = Path(os.environ['CCM_TEST_HOST'])
EXE = HOST.with_name('Codex-Control-MCP.exe')
RUN = ROOT/'test-workspace'/('host-' + uuid.uuid4().hex[:8])
HOME = RUN/'home space 涓枃'
HOME.mkdir(parents=True)
with socket.socket() as sock:
    sock.bind(('127.0.0.1', 0))
    PORT = sock.getsockname()[1]
(HOME/'config.toml').write_text(DEFAULT_CONFIG.replace('8774', str(PORT)), encoding='utf-8')
cfg = Config.load(HOME)
cfg.initialize_storage()
TOKEN = owner_token(cfg, create=True)
REPORT = {'cycles': [], 'scope': 'packaged background service; own process windows only',
          'host_sha256': hashlib.sha256(HOST.read_bytes()).hexdigest(),
          'core_sha256': hashlib.sha256(EXE.read_bytes()).hexdigest()}


async def exercise():
    async with httpx.AsyncClient(trust_env=False, timeout=40,
                                headers={'Authorization': 'Bearer ' + TOKEN}) as http:
        async with streamable_http_client(f'http://127.0.0.1:{PORT}/mcp', http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool('exec_command', {'command': "Write-Output 'BACKGROUND_HOST_OK'"})
                assert not result.isError, result.structuredContent
                assert result.structuredContent['result']['stdout'].strip() == 'BACKGROUND_HOST_OK'


def descendant_pids(root):
    code = ("$ids=[Collections.Generic.List[int]]::new();$ids.Add(" + str(root) + ");"
            "for($i=0;$i -lt $ids.Count;$i++){Get-CimInstance Win32_Process -Filter ('ParentProcessId='+$ids[$i])|"
            "ForEach-Object{$ids.Add([int]$_.ProcessId)}};ConvertTo-Json -Compress -InputObject @($ids)")
    result = subprocess.run(ps_argv(code), capture_output=True, timeout=15, creationflags=CREATE_NO_WINDOW)
    assert result.returncode == 0
    return set(json.loads(result.stdout))


def visible_owned_windows(pids):
    found = []
    def inspect(hwnd, _):
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid in pids and win32gui.IsWindowVisible(hwnd):
            found.append(pid)
    win32gui.EnumWindows(inspect, None)
    return found


try:
    with pefile.PE(str(HOST)) as pe:
        assert pe.OPTIONAL_HEADER.Subsystem == 2, 'Host must use the Windows GUI subsystem'
    previous = None
    for cycle in range(2):
        proc = subprocess.Popen([str(HOST), '--home', str(HOME) + '\\', 'serve', '--transport', 'streamable-http'], cwd=RUN)
        try:
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                assert proc.poll() is None, 'Background host exited during startup'
                try:
                    with socket.create_connection(('127.0.0.1', PORT), timeout=.2):
                        break
                except OSError:
                    time.sleep(.1)
            else:
                raise AssertionError('Background host did not bind')
            asyncio.run(exercise())
            record = json.loads((HOME/'state/service.json').read_text('utf-8'))
            assert record['instance_id'] != previous, 'Second cycle reused a stale service instance'
            previous = record['instance_id']
            owned = descendant_pids(proc.pid)
            assert record['pid'] in owned, 'Service process is absent from the owned host descendants'
            windows = visible_owned_windows(owned)
            assert not windows, 'Visible window PID(s) in owned process tree: ' + repr(windows)
            stopped = subprocess.run([str(EXE), '--home', str(HOME), 'stop'], capture_output=True,
                                     timeout=55, creationflags=CREATE_NO_WINDOW)
            assert stopped.returncode == 0, 'Stop failed: ' + stopped.stdout.decode('utf-8', 'replace')[-1500:]
            assert proc.wait(15) == 0, 'Background host returned a nonzero shutdown status'
            assert not (HOME/'state/service.json').exists(), 'Service state file remained after host exited'
            REPORT['cycles'].append({'cycle': cycle + 1, 'marker_verified': True,
                                     'visible_owned_windows': 0, 'graceful_exit': True,
                                     'instance_id': previous, 'owned_process_count': len(owned)})
        finally:
            if proc.poll() is None:
                subprocess.run([str(EXE), '--home', str(HOME), 'stop'], capture_output=True,
                               timeout=55, creationflags=CREATE_NO_WINDOW)
                try:
                    proc.wait(15)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    proc.wait(5)
    REPORT['pass'] = True
except Exception as error:
    REPORT['pass'] = False
    REPORT['error'] = type(error).__name__ + ': ' + str(error)[:2000]
finally:
    (ROOT/'evidence/background-host.json').write_text(json.dumps(REPORT, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(REPORT, ensure_ascii=True, indent=2))
raise SystemExit(0 if REPORT['pass'] else 1)

