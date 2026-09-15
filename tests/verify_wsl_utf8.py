"""Read-only WSL encoding acceptance through the actual official App Server."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import uuid

from codex_control_mcp import __version__
from codex_control_mcp.bridge import Bridge
from codex_control_mcp.config import Config

ROOT = Path(__file__).resolve().parents[1]
run = ROOT / 'test-workspace' / ('wsl-utf8-' + uuid.uuid4().hex[:8])
run.mkdir(parents=True)
missing = 'CCM-Not-Installed-' + uuid.uuid4().hex[:8]
baseline_env = os.environ.copy()
baseline_env.pop('WSL_UTF8', None)
baseline = subprocess.run(['wsl.exe', '-d', missing, '--exec', 'true'],
                          env=baseline_env, capture_output=True, timeout=20,
                          creationflags=subprocess.CREATE_NO_WINDOW)
raw = baseline.stdout + baseline.stderr
report = {'at': datetime.now(timezone.utc).isoformat(), 'bridge_version': __version__,
          'baseline': {'exit_code': baseline.returncode, 'contains_nul': b'\0' in raw,
                       'byte_count': len(raw)}, 'distributions_modified': False}
bridge = Bridge(Config(home=run / 'home', cwd=str(run)))
try:
    normal = bridge.execute('exec_command', {
        'shell': 'wsl', 'wsl_distribution': 'Codex-Control-MCP-Debian',
        'wsl_cwd': '/tmp', 'command': "printf 'CCM_WSL_中文😀\\n'; printf 'CCM_STDERR_中文\\n' >&2",
        'timeout_ms': 30000})
    failure = bridge.execute('exec_command', {
        'shell': 'wsl', 'wsl_distribution': missing, 'command': 'true', 'timeout_ms': 20000})
    normal_result, failure_result = normal.get('result') or {}, failure.get('result') or {}
    failure_text = failure_result.get('stdout', '') + failure_result.get('stderr', '')
    report['normal'] = normal
    report['missing_distribution'] = failure
    report['pass'] = bool(normal['ok'] and normal_result.get('stdout') == 'CCM_WSL_中文😀\n'
                          and 'CCM_STDERR_中文\n' in normal_result.get('stderr', '')
                          and not failure['ok'] and failure_result.get('exit_code') != 0
                          and 'WSL_E_DISTRO_NOT_FOUND' in failure_text
                          and '\0' not in failure_text and '\ufffd' not in failure_text
                          and normal_result.get('execution_backend') == 'codex_app_server.command_exec')
finally:
    bridge.close()
(ROOT / 'evidence/wsl-utf8-acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
print(json.dumps(report, ensure_ascii=True, indent=2))
raise SystemExit(0 if report['pass'] else 1)
