"""Owned HTTP/official-backend probes. Production mode never edits registries."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid

import httpx
from codex_control_mcp.auth import owner_token
from codex_control_mcp.config import Config, DEFAULT_CONFIG
from core_application_probe import rpc_result

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / 'evidence/stall-repair-20261007'


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--production', action='store_true'); args = parser.parse_args()
    run = EVIDENCE / ('round4-owned-' + uuid.uuid4().hex[:8]); run.mkdir()
    process = log = None
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    report = {'scope': 'production_read_and_rejection' if args.production else 'isolated_http_and_official_backend'}
    if args.production:
        cfg = Config.load()
    else:
        home = run / 'home'; home.mkdir()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        (home / 'config.toml').write_text(DEFAULT_CONFIG.replace('8774', str(port)), 'utf-8')
        cfg = Config.load(home); cfg.initialize_storage()
    token = owner_token(cfg, create=not args.production)
    url = f"http://127.0.0.1:{cfg.http.get('port', 8774)}/mcp"
    try:
        if not args.production:
            log = (run / 'service.log').open('wb')
            process = subprocess.Popen([sys.executable, '-m', 'codex_control_mcp', '--home', str(cfg.home), 'serve', '--transport', 'streamable-http'],
                                       cwd=ROOT, env=env, stdout=log, stderr=log, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            report['temporary_service_pid'] = process.pid
            deadline = time.monotonic() + 20
            while True:
                if process.poll() is not None: raise RuntimeError('temporary service exited')
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=.2): break
                except OSError:
                    if time.monotonic() > deadline: raise TimeoutError('temporary service start')
                    time.sleep(.1)
        with httpx.Client(trust_env=False, timeout=30, headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json, text/event-stream'}) as client:
            counter = 0
            def rpc(method, params):
                nonlocal counter
                counter += 1
                response = client.post(url, json={'jsonrpc': '2.0', 'id': counter, 'method': method, 'params': params})
                if response.headers.get('Mcp-Session-Id'): client.headers['Mcp-Session-Id'] = response.headers['Mcp-Session-Id']
                return rpc_result(response, counter)
            def call(name, arguments, ok=True):
                out = rpc('tools/call', {'name': name, 'arguments': arguments})['structuredContent']
                assert out['ok'] is ok, (name, (out.get('error') or {}).get('code'))
                return out
            initialized = rpc('initialize', {'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'round4-owned-verifier', 'version': '1'}})
            client.headers['MCP-Protocol-Version'] = initialized['protocolVersion']
            client.post(url, json={'jsonrpc': '2.0', 'method': 'notifications/initialized'}).raise_for_status()
            report['tool_count'] = len(rpc('tools/list', {})['tools'])
            report['runtime_version'] = call('codex_health', {})['evidence']['runtime_version']
            body = ('你好🙂' * 700) + '\nnext\n'; target = run / 'unicode.txt'; target.write_bytes(body.encode('utf-8'))
            first = call('read_file', {'path': str(target), 'max_bytes': 1024})['result']
            text, receipt, pages = first['content'], first, 1
            while receipt['next_action']:
                assert pages < 20
                receipt = call(receipt['next_action']['tool'], receipt['next_action']['arguments'])['result']
                text += receipt['content']; pages += 1
            assert text == body
            report['long_unicode_line'] = {'pages': pages, 'bytes': len(body.encode()), 'identical': True}
            target.write_text('changed', 'utf-8')
            changed = call('read_file', first['next_action']['arguments'], ok=False)
            assert changed['error']['code'] == 'concurrent_modification'
            report['changed_file_rejected'] = True
            assert call('host_files', {'host': 'local', 'action': 'read', 'path': str(target), 'recursive': True}, ok=False)['error']['code'] == 'invalid_arguments'
            assert call('host_files', {'host': 'local', 'action': 'write', 'path': str(target), 'content': 'overwrite', 'force': True}, ok=False)['error']['code'] == 'invalid_arguments'
            assert target.read_text('utf-8') == 'changed'
            report['unsupported_options_rejected_before_write'] = True
            if not args.production:
                fixture = run / 'fixture.py'; dispatch = run / 'dispatch.txt'
                fixture.write_text('''import asyncio
from pathlib import Path
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
server = Server("round4-fixture", version="1")
@server.list_tools()
async def list_tools():
    return [types.Tool(name="echo", inputSchema={"type":"object","properties":{"password":{"type":"integer"}},"required":["password"]})]
@server.call_tool()
async def call_tool(name, arguments):
    Path(__file__).with_name("dispatch.txt").write_text("once")
    return [types.TextContent(type="text", text="FIXTURE_OK")]
async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
asyncio.run(main())
''', 'utf-8')
                call('mcp_manage', {'action': 'register', 'name': 'fixture', 'transport': 'stdio', 'command': sys.executable, 'args': [str(fixture)]})
                call('mcp_manage', {'action': 'refresh', 'name': 'fixture'})
                bad = call('mcp_tool_call', {'name': 'fixture:echo', 'arguments': {'password': 'synthetic-private-value'}}, ok=False)
                assert 'synthetic-private-value' not in json.dumps(bad) and not dispatch.exists()
                call('mcp_tool_call', {'name': 'fixture:echo', 'arguments': {'password': 1}})
                assert dispatch.read_text() == 'once'
                call('mcp_manage', {'action': 'register', 'name': 'aaa-offline', 'transport': 'stdio', 'command': str(run / 'nonexistent.exe'), 'timeout_ms': 1000})
                search = call('mcp_tool_search', {'query': 'echo'})['result']
                assert search['partial'] and any(t['qualified_name'] == 'fixture:echo' for t in search['tools'])
                call('mcp_manage', {'action': 'update', 'name': 'fixture', 'args': [str(fixture), '--changed']})
                current = call('mcp_manage', {'action': 'get', 'name': 'fixture'})['result']
                assert current['tools'] == [] and current['server']['status'] == 'never_refreshed'
                report['dynamic_stdio'] = {'sensitive_error_hidden': True, 'invalid_call_not_dispatched': True, 'valid_call_passed': True,
                                           'offline_node_did_not_hide_healthy_tools': True, 'cache_invalidated': True}
            report['ok'] = True
    finally:
        if process is not None:
            if process.poll() is None:
                stopped = subprocess.run([sys.executable, '-m', 'codex_control_mcp', '--home', str(cfg.home), 'stop'], cwd=ROOT, env=env,
                                         capture_output=True, timeout=50, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                report['temporary_stop_exit_code'] = stopped.returncode
                try: process.wait(15)
                except subprocess.TimeoutExpired:
                    process.terminate(); process.wait(10)
            report['temporary_service_stopped'] = process.poll() is not None
        if log is not None: log.close()
        (EVIDENCE / ('round4-production.json' if args.production else 'round4-candidate.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
