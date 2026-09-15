"""Actual MCP -> official App Server -> WSL environment and local proxy checks."""
import argparse
import asyncio
import hashlib
import http.server
import json
import os
from pathlib import Path
import sys
import threading
import time
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from codex_control_mcp import __version__

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--exe', type=Path)
options = parser.parse_args()
run = ROOT / 'test-workspace' / ('wsl-transport-' + uuid.uuid4().hex[:8])
run.mkdir(parents=True)
requests = []

class LocalProxy(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        requests.append({'method': self.command, 'target': self.path})
        self.send_response(204)
        self.end_headers()
    def log_message(self, *_):
        pass

proxy = http.server.ThreadingHTTPServer(('127.0.0.1', 0), LocalProxy)
threading.Thread(target=proxy.serve_forever, daemon=True).start()
marker = 'CCM_WSL_ENV_中文😀'
report = {'bridge_version': __version__, 'started_at': time.time(), 'pass': False,
          'scope': 'Actual official WSL1 execution and controlled loopback proxy, not public internet routing',
          'global_network_settings_modified': False, 'other_distributions_modified': False,
          'steps': [], 'executable_sha256': hashlib.sha256(options.exe.read_bytes()).hexdigest() if options.exe else None}

async def main():
    command = str(options.exe) if options.exe else sys.executable
    arguments = ([] if options.exe else ['-m', 'codex_control_mcp']) + ['--home', str(run / 'home'), 'serve']
    environment = os.environ.copy()
    environment['PYTHONPATH'] = str(ROOT / 'src')
    params = StdioServerParameters(command=command, args=arguments, cwd=str(run), env=environment)
    with (run / 'stdio-stderr.log').open('w', encoding='utf-8') as errors:
        async with stdio_client(params, errlog=errors) as (read, write):
            async with ClientSession(read, write) as client:
                init = await client.initialize()
                assert init.serverInfo.version == __version__
                async def call(name, args, expect_success=True):
                    response = await client.call_tool(name, args)
                    data = response.structuredContent
                    assert isinstance(data, dict)
                    report['steps'].append({'tool': name, 'response': data})
                    assert bool(data['ok']) == expect_success
                    return data.get('result') or {}
                base = {'shell': 'wsl', 'wsl_distribution': 'Codex-Control-MCP-Debian',
                        'wsl_cwd': '/tmp', 'timeout_ms': 15000}
                text = await call('exec_command', {**base, 'env': {'CCM_EXPLICIT_ENV': marker},
                    'command': 'printf "%s\\n" "$CCM_EXPLICIT_ENV"; printf "CCM_STDERR_中文\\n" >&2'})
                assert text['stdout'] == marker + '\n' and 'CCM_STDERR_中文\n' in text['stderr']
                failure = await call('exec_command', {**base,
                    'wsl_distribution': 'CCM-Not-Installed-' + uuid.uuid4().hex[:8], 'command': 'true'}, False)
                diagnostic = failure['stdout'] + failure['stderr']
                assert 'WSL_E_DISTRO_NOT_FOUND' in diagnostic and '\0' not in diagnostic and '\ufffd' not in diagnostic
                session = await call('session_start', {**base, 'env': {'CCM_EXPLICIT_ENV': marker},
                    'command': 'printf "%s\\n" "$CCM_EXPLICIT_ENV"'})
                until = time.monotonic() + 20
                while True:
                    output = await call('session_read', {'session_id': session['session_id']})
                    if output['state'] == 'exited':
                        break
                    assert time.monotonic() < until
                    await asyncio.sleep(0.1)
                assert output['exit_code'] == 0 and output['stdout'] == marker + '\n'
                proxy_command = '''set -eu
authority=${HTTP_PROXY#http://}
host=${authority%:*}
port=${authority##*:}
exec 3<>/dev/tcp/"$host"/"$port"
printf 'GET http://ccm.invalid/wsl-proxy-proof HTTP/1.1\r\nHost: ccm.invalid\r\nConnection: close\r\n\r\n' >&3
IFS= read -r status <&3
case "$status" in *' 204 '*) printf 'CCM_WSL_PROXY_OK\\n';; *) exit 7;; esac
'''
                routed = await call('exec_command', {**base, 'command': proxy_command,
                    'env': {'HTTP_PROXY': f'http://127.0.0.1:{proxy.server_port}'}})
                assert routed['stdout'] == 'CCM_WSL_PROXY_OK\n'
                assert requests == [{'method': 'GET', 'target': 'http://ccm.invalid/wsl-proxy-proof'}]
                report['pass'] = True

try:
    asyncio.run(main())
except Exception as exc:
    report['error_type'] = type(exc).__name__
finally:
    proxy.shutdown()
    proxy.server_close()
    report['controlled_proxy_requests'] = requests
    report['finished_at'] = time.time()
    destination = ROOT / 'evidence' / ('wsl-transport-exe-v019.json' if options.exe else 'wsl-transport-source-v019.json')
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
    print(json.dumps({'pass': report['pass'], 'evidence': str(destination), 'steps': len(report['steps'])}))
raise SystemExit(0 if report['pass'] else 1)
