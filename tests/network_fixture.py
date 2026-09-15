"""Gate-controlled fixture for whole owned process-tree network observation."""
from pathlib import Path
import json
import os
import sys
import time

run = Path(sys.argv[1])
exe = Path(sys.argv[2])
deadline = time.monotonic() + 15
while not (run / 'go').exists():
    if time.monotonic() > deadline:
        raise SystemExit('Trace startup gate did not arrive')
    time.sleep(0.05)

import hashlib
import http.server
import socket
import subprocess
import threading
from urllib.parse import urlsplit

attempts = []
class Sentinel(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        attempts.append({'method': self.command})
        self.send_response(503)
        self.end_headers()
    do_POST = do_GET
    def log_message(self, *_):
        pass

server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Sentinel)
threading.Thread(target=server.serve_forever, daemon=True).start()
proxy_attempts = []
proxy = None
child_env = os.environ.copy()
if os.environ.get('CCM_TRACE_CLASSIFY_PROXY') == '1':
    # Diagnostic second run: record only requested destination authority and
    # reject forwarding. Never intercept TLS, log headers or change system proxy.
    class ClassifyProxy(http.server.BaseHTTPRequestHandler):
        def do_CONNECT(self):
            dest = urlsplit('https://' + self.path)
            proxy_attempts.append({'method': self.command, 'host': dest.hostname, 'port': dest.port or 443})
            self.send_error(502, 'Test proxy does not forward requests')
        def do_GET(self):
            dest = urlsplit(self.path)
            proxy_attempts.append({'method': self.command, 'host': dest.hostname, 'port': dest.port or 80})
            self.send_error(502, 'Test proxy does not forward requests')
        do_POST = do_GET
        def log_message(self, *_):
            pass
    proxy = http.server.ThreadingHTTPServer(('127.0.0.1', 0), ClassifyProxy)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'):
        child_env[key] = child_env[key.lower()] = f'http://127.0.0.1:{proxy.server_port}'
    child_env['NO_PROXY'] = child_env['no_proxy'] = 'localhost,127.0.0.1,::1'
home = run / 'home'
runtime = home / 'runtime-home'
runtime.mkdir(parents=True)
(runtime / 'config.toml').write_text('\n'.join([
    'sandbox_mode="danger-full-access"', 'approval_policy="never"',
    'model="trace-execution-only"', 'model_provider="trace_sentinel"',
    '[analytics]', 'enabled=false', '[feedback]', 'enabled=false',
    '[model_providers.trace_sentinel]', 'name="Execution trace sentinel"',
    f'base_url="http://127.0.0.1:{server.server_port}/v1"',
    'wire_api="responses"', 'requires_openai_auth=false',
]), 'utf-8')
# A positive control proves ETW sees traffic; the echo bytes contain no secrets.
with socket.socket() as listener:
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    port = listener.getsockname()[1]
    def echo():
        peer, _ = listener.accept()
        with peer:
            peer.sendall(peer.recv(32))
    thread = threading.Thread(target=echo)
    thread.start()
    with socket.create_connection(('127.0.0.1', port)) as peer:
        peer.sendall(b'CCM_ETW_CONTROL')
        assert peer.recv(32) == b'CCM_ETW_CONTROL'
    thread.join(5)

params = {'command': "Write-Output 'CCM_ETW_OFFICIAL_中文'", 'cwd': str(run), 'timeout_ms': 20000}
p = subprocess.run([str(exe), '--home', str(home), 'call', 'exec_command', '--args-json', json.dumps(params, ensure_ascii=True)], capture_output=True, timeout=90, creationflags=subprocess.CREATE_NO_WINDOW, env=child_env)
server.shutdown(); server.server_close()
if proxy:
    proxy.shutdown(); proxy.server_close()
response = json.loads(p.stdout)
value = response.get('result') or {}
report = {
    'pass': p.returncode == 0 and response.get('ok') is True and value.get('exit_code') == 0 and value.get('execution_backend') == 'codex_app_server.command_exec' and 'CCM_ETW_OFFICIAL_中文' in value.get('stdout', '') and not attempts,
    'exe': str(exe), 'exe_sha256': hashlib.sha256(exe.read_bytes()).hexdigest(),
    'pid': os.getpid(), 'positive_control_port': port, 'model_sentinel_port': server.server_port,
    'configured_model_requests': len(attempts), 'official_result': response,
    'proxy_mode': 'diagnostic_authority_only_no_forwarding' if proxy else 'inherited_unmodified',
    'proxy_listener_port': proxy.server_port if proxy else None,
    'proxy_destination_attempts': proxy_attempts,
}
(run / 'fixture.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
raise SystemExit(0 if report['pass'] else 1)
