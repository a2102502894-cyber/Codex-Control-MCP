"""Actual direct CLI / stdio GUI checks, without an interactive MCP callback."""
from __future__ import annotations
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from codex_control_mcp import __version__
from codex_control_mcp.bridge import Bridge
from codex_control_mcp.config import Config, DEFAULT_CONFIG

ROOT = Path(__file__).resolve().parents[1]
ap = argparse.ArgumentParser()
ap.add_argument('--mode', choices=('direct', 'stdio'), required=True)
ap.add_argument('--out', type=Path, required=True)
ap.add_argument('--exe', type=Path)
args = ap.parse_args()
out = args.out.resolve()
out.mkdir(parents=True, exist_ok=False)
home = out / 'home'
home.mkdir()
(home / 'config.toml').write_text('computer_use_enabled = true\nauto_approve_application_access = true\n' + DEFAULT_CONFIG, encoding='utf-8')
cfg = Config.load(home)
env = os.environ.copy()
env['PYTHONPATH'] = str(ROOT / 'src')
env['PYTHONIOENCODING'] = 'utf-8'
command = [str(args.exe.resolve())] if args.exe else [sys.executable, '-B', '-m', 'codex_control_mcp']
report = {'version': __version__, 'mode': args.mode, 'packaged': bool(args.exe),
          'client_elicitation_callback': False, 'only_read_only_gui_calls': True,
          'started_at': time.time(), 'snapshots': []}


def target(data):
    if not data.get('ok'):
        raise RuntimeError('Enumeration failed: ' + json.dumps(data.get('error')))
    choices = [w for w in data['result'].get('windows', []) if 'tabbit' in json.dumps(w.get('app', '')).casefold()]
    if not choices:
        raise RuntimeError('No returned Tabbit window')
    return choices[0]['id']


def record(data, image_count):
    if not data.get('ok'):
        raise RuntimeError('GUI failed: ' + json.dumps(data.get('error')))
    payload = data['result']
    item = {'ok': True, 'image_count': image_count,
            'policy': payload['runtime']['application_access_policy'],
            'authorization': payload.get('application_authorization')}
    assert image_count and item['policy'] == 'owner_preapproved'
    assert item['authorization']['decision'] == 'accept'
    assert item['authorization']['source'] == 'explicit_owner_preapproval'
    report['snapshots'].append(item)


async def stdio():
    with (out / 'stdio.log').open('w', encoding='utf-8') as err:
        async with stdio_client(StdioServerParameters(command=command[0],
            args=command[1:] + ['--home', str(home), 'serve', '--transport', 'stdio'],
            cwd=str(ROOT), env=env), errlog=err) as streams:
            async with ClientSession(*streams) as client:
                init = await client.initialize()
                assert init.serverInfo.version == __version__
                listed = await client.call_tool('computer_snapshot', {})
                selected = target(listed.structuredContent)
                for _ in range(2):
                    result = await client.call_tool('computer_snapshot', {'window_id': selected})
                    record(result.structuredContent, sum(x.type == 'image' for x in result.content))


def direct():
    bridge = None if args.exe else Bridge(cfg)
    def call(tool, params):
        if bridge:
            return bridge.execute(tool, params)
        result = subprocess.run(command + ['--home', str(home), 'call', tool, '--args-json', json.dumps(params)],
            cwd=ROOT, env=env, capture_output=True, timeout=230, creationflags=subprocess.CREATE_NO_WINDOW)
        data = json.loads(result.stdout)
        return data
    try:
        selected = target(call('computer_snapshot', {}))
        for _ in range(2):
            data = call('computer_snapshot', {'window_id': selected})
            record(data, len(data.get('_image_blocks', [])))
    finally:
        if bridge:
            bridge.close()


try:
    if args.mode == 'stdio':
        asyncio.run(stdio())
    else:
        direct()
    report['pass'] = True
except Exception as exc:
    report['pass'] = False
    report['error'] = str(exc)
finally:
    report['finished_at'] = time.time()
    (out / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True), flush=True)
raise SystemExit(0 if report['pass'] else 1)
