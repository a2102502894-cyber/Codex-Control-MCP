"""Freeze immutable, verified source; activation switches a guarded selection only."""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import tomllib

try:
    from .lkg_state import activate, atomic_bytes, probe, snapshot_record, tree_manifest
except ImportError:
    from lkg_state import activate, atomic_bytes, probe, snapshot_record, tree_manifest

ROOT = Path(__file__).resolve().parents[1]
RECOVERY_FILES = ['core_recovery_controller.py', 'Core-Source-Host.ps1', 'Request-Core-Restart.ps1',
                  'Install-Core-RecoveryTasks.ps1', 'Service-Watchdog.ps1', 'lkg_state.py', 'freeze_source_lkg.py']


def freeze(root, home, tests, *, probe_fn=probe):
    if tests.get('all_passed') is not True:
        raise ValueError('Final test evidence must be passing')
    root, state = Path(root).resolve(), Path(home) / 'state'
    version = tomllib.loads((root / 'pyproject.toml').read_text('utf-8'))['project']['version']
    state.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='lkg-candidate-', dir=state))
    try:
        shutil.copytree(root / 'src/codex_control_mcp', staging / 'src/codex_control_mcp',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.bak'))
        (staging / 'scripts').mkdir()
        for name in RECOVERY_FILES:
            shutil.copy2(root / 'scripts' / name, staging / 'scripts' / name)
        validation = probe_fn(staging, version, None)
        rows, digest = tree_manifest(staging)
        manifest = {'schema': 2, 'version': version, 'expected_tool_count': validation['core_tools'],
                    'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    'source': str(root / 'src/codex_control_mcp'), 'sha256': digest,
                    'sha256_algorithm': 'SHA256(canonical UTF8 JSON ordered file path/bytes/SHA256 rows; excludes manifest and pycache)',
                    'file_count': len(rows), 'files': rows, 'validation': validation, 'tests': tests,
                    'packaging_required': False}
        manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8')
        atomic_bytes(staging / 'manifest.json', manifest_bytes)
        # Evidence is immutable too: later acceptance creates a distinct snapshot.
        identity = hashlib.sha256(manifest_bytes).hexdigest()[:16]
        target = state / f'lkg-{version}-{digest[:16]}-{identity}'
        if target.exists():
            existing = snapshot_record(target, state)
            if existing['sha256'] != digest or existing['version'] != version:
                raise ValueError('Immutable LKG snapshot already exists with different contents')
            # Retain its original acceptance evidence; never upgrade it in place.
            return target
        staging.rename(target)
        snapshot_record(target, state)
        return target
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--home', type=Path, default=Path.home() / '.codex-control-mcp')
    parser.add_argument('--tests-json', type=Path, required=True)
    parser.add_argument('--activate', action='store_true')
    parser.add_argument('--snapshot', type=Path, help='Activate an existing immutable snapshot with its original evidence')
    args = parser.parse_args()
    tests = json.loads(args.tests_json.read_text('utf-8'))
    if tests.get('all_passed') is not True:
        raise ValueError('Final test evidence must be passing')
    if args.activate and tests.get('full_stack_accepted') is not True:
        raise ValueError('Do not activate without full-stack acceptance evidence')
    if args.snapshot and not args.activate:
        parser.error('--snapshot requires --activate')
    snapshot = args.snapshot or freeze(ROOT, args.home, tests)
    selection = activate(args.home, snapshot) if args.activate else None
    print(json.dumps({'path': str(snapshot), 'selection': selection, 'activated': args.activate,
                      'full_stack_accepted': tests.get('full_stack_accepted') is True}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
