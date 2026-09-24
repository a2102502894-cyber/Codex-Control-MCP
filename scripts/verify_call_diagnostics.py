"""Real MCP/official-runtime acceptance using only new test-owned fixtures.

No platform rejection is replayed and no policy or authorization is changed.
The fixture tests local success, process failure and protocol-error reporting.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


def now():
    return datetime.now(timezone.utc).isoformat()


async def run(report, work, home):
    env = dict(os.environ)
    env['PYTHONPATH'] = str(ROOT / 'src')
    env['PYTHONUTF8'] = '1'
    params = StdioServerParameters(
        command=sys.executable,
        args=['-X', 'utf8', '-m', 'codex_control_mcp', '--home', str(home),
              'serve', '--transport', 'stdio'],
        cwd=str(work), env=env,
    )
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as client:
            init = await client.initialize()
            report['server'] = init.serverInfo.model_dump()
            calls = report['calls']

            async def call(name, args):
                value = await client.call_tool(name, args)
                out = value.structuredContent
                if out is None:
                    out = next(json.loads(x.text) for x in value.content if x.type == 'text')
                assert out['operation_id'] == out['diagnostics']['operation_id']
                assert out['diagnostics']['mcp_received'] is True
                assert out['diagnostics']['bridge_received'] is True
                assert out['diagnostics']['upstream_safety_decision'] == 'not_observable'
                assert value.isError == (not out['ok'])
                data = out.get('result') or {}
                calls.append({'tool': name, 'operation_id': out['operation_id'],
                              'ok': out['ok'], 'error': out.get('error'),
                              'state': data.get('state'), 'exit_code': data.get('exit_code'),
                              'next_cursor': data.get('next_cursor'),
                              'stdout_bytes': len(data.get('stdout', '').encode('utf-8')),
                              'origin_operation_id': data.get('origin_operation_id'),
                              'diagnostics': out['diagnostics']})
                report['runtime_version'] = out.get('evidence', {}).get('runtime_version')
                return out

            async def settle(out):
                data = out.get('result') or {}
                original_id = out['operation_id']
                sid = data.get('session_id')
                output = data.get('stdout', '')
                # session mode returns metadata, not a consumed output page.
                cursor = data.get('next_cursor', 0) if 'stdout' in data else 0
                for _ in range(100):
                    if data.get('state') not in ('starting', 'running'):
                        return out, output
                    await asyncio.sleep(0.05)
                    out = await call('session_read', {'session_id': sid, 'cursor': cursor})
                    data = out['result']
                    assert data['origin_operation_id'] == original_id
                    output += data.get('stdout', '')
                    cursor = data.get('next_cursor', cursor)
                raise AssertionError('Owned fixture did not finish within bounded polling')

            fixture = work / 'diagnostic-fixture.txt'
            text = '真实调用验收：文件内容与中文编码。\n'
            added = await call('file_add', {'path': str(fixture), 'content': text})
            assert added['ok']
            read = await call('read_file', {'path': str(fixture)})
            assert read['ok'] and read['result']['content'] == text
            assert read['result']['sha256'] == hashlib.sha256(text.encode()).hexdigest()
            report['checks']['utf8_file_roundtrip'] = True

            failure = await call('exec_command', {
                'argv': [sys.executable, '-X', 'utf8', '-c',
                         "import time,sys;print('fixture-start',flush=True);time.sleep(0.4);print('fixture-end',flush=True);sys.exit(7)"],
                'cwd': str(work), 'execution_mode': 'session', 'timeout_ms': 10000})
            assert failure['ok'] and failure['result']['state'] in ('starting', 'running')
            final, output = await settle(failure)
            assert not final['ok'] and final['result']['exit_code'] == 7
            assert final['diagnostics']['failure_origin'] == 'command_process'
            assert output.count('fixture-start') == 1 and output.count('fixture-end') == 1
            report['checks']['real_nonzero_exit_and_poll_linkage'] = True

            invalid = await call('read_file', {})
            assert not invalid['ok'] and invalid['error']['code'] == 'invalid_arguments'
            assert invalid['diagnostics']['rpc_dispatched_count'] == 0
            assert invalid['diagnostics']['failure_origin'] == 'bridge_validation'
            report['checks']['local_validation_origin'] = True

            missing = await call('list_dir', {'path': str(work / 'intentionally-nonexistent')})
            assert not missing['ok']
            assert missing['diagnostics']['failure_origin'] == 'codex_app_server'
            assert missing['error']['details']['operation_id'] == missing['operation_id']
            assert type(missing['error']['details']['rpc_code']) is int
            report['checks']['real_official_rpc_error_origin'] = True

            async def successful(label):
                out = await call('exec_command', {
                    'argv': [sys.executable, '-X', 'utf8', '-c', f'print({label!r})'],
                    'cwd': str(work), 'execution_mode': 'buffered', 'timeout_ms': 10000})
                assert out['ok'] and out['result']['stdout'].strip() == label
                return out['operation_id']
            ids = await asyncio.gather(successful('parallel-A-中文'), successful('parallel-B-中文'))
            assert ids[0] != ids[1]
            report['checks']['concurrent_success_correlated'] = True

            counter = work / 'once.txt'
            args = {'argv': [sys.executable, '-X', 'utf8', '-c',
                             "from pathlib import Path;p=Path('once.txt');p.open('a',encoding='utf-8',newline='').write('once\\n')"],
                    'cwd': str(work), 'execution_mode': 'buffered', 'timeout_ms': 10000,
                    'idempotency_key': 'owned-acceptance-once'}
            first = await call('exec_command', args)
            second = await call('exec_command', args)
            assert first['ok'] and second['ok'] and second['idempotent_replay']
            assert first['operation_id'] != second['operation_id']
            assert second['diagnostics']['replayed_operation_id'] == first['operation_id']
            assert second['diagnostics']['rpc_dispatched_count'] == 0
            once = await call('read_file', {'path': str(counter)})
            assert once['result']['content'] == 'once\n'
            report['checks']['idempotency_no_duplicate_write'] = True

            health = await call('codex_health', {'active': False, 'refresh': False})
            observation = health['result']['health_observation']
            assert observation['mode'] == 'passive'
            assert observation['checks']['shell']['source'] == 'cached_rpc_success'
            assert observation['checks']['shell']['observed_at']
            assert observation['guarantees_future_call_authorization'] is False
            report['checks']['health_evidence_explicit'] = True
            report['health_observation'] = observation
            for owned in (counter, fixture):
                removed = await call('file_delete', {'path': str(owned)})
                assert removed['ok']
            report['checks']['owned_files_removed'] = True

    # The owned MCP server has exited before verifying its terminal audit.
    events = [json.loads(line) for line in (home / 'logs/audit.jsonl').read_text('utf-8').splitlines()]
    for record in report['calls']:
        linked = [e for e in events if e.get('operation_id') == record['operation_id']]
        names = {e['event'] for e in linked}
        assert {'mcp_received', 'tool_received', 'tool_finish'} <= names
        assert record['diagnostics']['rpc_dispatched_count'] == sum(e['event'] == 'rpc_send' for e in linked)
        for e in linked:
            if e['event'] in ('rpc_result', 'rpc_error'):
                assert any(s['event'] == 'rpc_send' and s.get('request_id') == e['request_id']
                           and s.get('generation') == e.get('generation') for s in linked)
    assert any(e['event'] == 'appserver_stop' for e in events)
    report['checks']['audit_roundtrip_and_owned_shutdown'] = True
    report['event_count'] = len(events)
    report['pass'] = True


def main():
    owned = ROOT / 'test-workspace' / ('call-diagnostics-live-' + uuid.uuid4().hex[:12])
    work, home = owned / 'project', owned / 'bridge-home'
    work.mkdir(parents=True)
    home.mkdir()
    (home / 'config.toml').write_text('cwd = ' + json.dumps(str(work), ensure_ascii=False) + '\n', 'utf-8')
    report = {'started_at': now(), 'scope': 'real MCP stdio and installed official Codex runtime; test-owned fixtures only',
              'production_service_changed': False, 'platform_rejection_replayed': False,
              'workspace': str(owned), 'calls': [], 'checks': {}, 'pass': False}
    try:
        asyncio.run(run(report, work, home))
    except BaseException as exc:
        report['failure_type'] = type(exc).__name__
        raise
    finally:
        report['finished_at'] = now()
        target = ROOT / 'evidence/call-diagnostics-live.json'
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
        print(json.dumps({'report': str(target), 'pass': report['pass'], 'checks': report['checks'],
                          'call_count': len(report['calls'])}, ensure_ascii=False))


if __name__ == '__main__':
    main()
