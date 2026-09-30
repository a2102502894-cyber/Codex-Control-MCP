"""Portable, credential-free LKG selection and integrity checks.

The active record is the single atomic source/config selection. Legacy snapshots
are never renamed, rewritten, or removed. No service is started by this module.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

PROBE = r'''
import json,sys,pathlib,tomllib
root=pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0,str(root/'src'))
import codex_control_mcp as pkg
from codex_control_mcp.tools import TOOL_SPECS
from codex_control_mcp.config import DEFAULT_CONFIG
origin=pathlib.Path(pkg.__file__).resolve()
assert origin.is_relative_to(root/'src'),origin
out={'version':pkg.__version__,'core_tools':len(TOOL_SPECS),
     'static_grok_tools':sum(n.startswith('grok_') for n in TOOL_SPECS),
     'static_director_tools':sum(n.startswith('director_') for n in TOOL_SPECS),
     'default_port':tomllib.loads(DEFAULT_CONFIG)['http']['port'],
     'module_path':str(origin),'isolated_import':True}
assert out['static_grok_tools']==out['static_director_tools']==0
assert out['default_port']==8774
print(json.dumps(out,ensure_ascii=False))
'''


def probe(root, expected_version=None, expected_tools=None):
    root = Path(root).resolve()
    result = subprocess.run(
        [sys.executable, '-I', '-X', 'utf8', '-c', PROBE, str(root)],
        cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=40,
        env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
    )
    if result.returncode:
        raise ValueError('LKG isolated probe failed: ' + result.stderr[-2000:])
    value = json.loads(result.stdout)
    if not isinstance(value.get('version'), str) or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:[a-zA-Z0-9.+-]*)', value['version']):
        raise ValueError('Invalid LKG version')
    if type(value.get('core_tools')) is not int or value['core_tools'] <= 0:
        raise ValueError('Invalid LKG tool count')
    if expected_version is not None and value['version'] != expected_version:
        raise ValueError('LKG version mismatch')
    if expected_tools is not None and value['core_tools'] != expected_tools:
        raise ValueError('LKG tool count mismatch')
    return value


def tree_manifest(root):
    root = Path(root)
    rows = []
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('LKG symlinks are forbidden')
        if path.is_file() and path.name != 'manifest.json' and '__pycache__' not in path.parts:
            rows.append({'path': path.relative_to(root).as_posix(), 'bytes': path.stat().st_size,
                         'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    digest = hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
    return rows, digest


def validate_snapshot(root):
    root = Path(root).resolve()
    manifest = json.loads((root / 'manifest.json').read_text('utf-8'))
    if not isinstance(manifest, dict) or manifest.get('schema') not in (None, 2):
        raise ValueError('Unsupported LKG manifest')
    rows, digest = tree_manifest(root)
    validation = manifest.get('validation', {})
    tools = manifest.get('expected_tool_count', validation.get('core_tools'))
    version = manifest.get('version')
    if (manifest.get('sha256') != digest or manifest.get('files') != rows
            or manifest.get('file_count') != len(rows)):
        raise ValueError('LKG source fingerprint mismatch')
    if (not isinstance(version, str) or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:[a-zA-Z0-9.+-]*)', version)
            or type(tools) is not int or tools <= 0
            or validation.get('version') != version or validation.get('core_tools') != tools):
        raise ValueError('LKG manifest version/tool count mismatch')
    return manifest


def snapshot_record(root, state):
    root, state = Path(root).resolve(), Path(state).resolve()
    if root.parent != state or not root.name.startswith('lkg-'):
        raise ValueError('LKG snapshot must be directly inside state')
    manifest = validate_snapshot(root)
    return {'schema': 1, 'snapshot': root.name, 'version': manifest['version'],
            'expected_tool_count': manifest.get('expected_tool_count', manifest['validation']['core_tools']),
            'sha256': manifest['sha256'],
            'manifest_sha256': hashlib.sha256((root / 'manifest.json').read_bytes()).hexdigest()}


def resolve_selection(home, *, allow_pending=False):
    state = Path(home) / 'state'
    if not allow_pending and (state / 'lkg-active.rollback.json').exists():
        raise ValueError('Unresolved LKG activation; inspect rollback record before starting')
    active = state / 'lkg-active.json'
    if not active.exists():
        # Preserve the pre-existing entry and expectations without importing it.
        return {'source_root': str((state / 'lkg-0.2.0' / 'src').resolve()),
                'version': '0.2.0', 'expected_tool_count': 47, 'legacy': True}
    record = json.loads(active.read_text('utf-8'))
    if not isinstance(record, dict) or type(record.get('schema')) is not int or record['schema'] != 1:
        raise ValueError('Unsupported LKG active selection')
    name = record.get('snapshot')
    if not isinstance(name, str) or Path(name).name != name or '/' in name or '\\' in name:
        raise ValueError('Invalid LKG snapshot path')
    root = state / name
    if root.is_symlink() or snapshot_record(root, state) != record:
        raise ValueError('LKG active fingerprint mismatch')
    return {**record, 'source_root': str((root / 'src').resolve()), 'legacy': False}


def selected_config(config):
    """Overlay only the LKG candidate; formal/current pins remain independent."""
    config = {**config, 'candidates': [dict(c) for c in config['candidates']]}
    selection = resolve_selection(config['home'])
    for candidate in config['candidates']:
        if candidate.get('name') == 'lkg_direct':
            candidate.update(expected_version=selection['version'],
                             expected_tool_count=selection['expected_tool_count'])
    return config


def atomic_bytes(path, content):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != 'nt':
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def maintenance_guard(state):
    """Use the controller's permanent guard inode; never unlink the lock file."""
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True)
    with open(state / 'maintenance.lock.guard', 'a+b', buffering=0) as stream:
        if stream.seek(0, 2) == 0:
            stream.write(b'\0')
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            lease = state / 'maintenance.lock'
            if lease.exists():
                # Do not bypass a legacy live lease or reinterpret bad records.
                raise ValueError('Maintenance lease exists; complete controller maintenance first')
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def activate(home, snapshot, *, probe_fn=probe):
    """Switch selection only, with guarded pre/post probes and exact rollback.

No command/service is dispatched. In-process failures roll back selection;
rollback failure is explicit, and an exact backup remains for manual recovery.
"""
    state = Path(home) / 'state'
    with maintenance_guard(state):
        root = Path(snapshot).resolve()
        record = snapshot_record(root, state)
        tests = validate_snapshot(root).get('tests', {})
        if tests.get('all_passed') is not True or tests.get('full_stack_accepted') is not True:
            raise ValueError('Activation requires full-stack acceptance evidence')
        probe_fn(root, record['version'], record['expected_tool_count'])
        active = state / 'lkg-active.json'
        previous = active.read_bytes() if active.exists() else None
        if previous is not None:
            resolve_selection(home)  # Never overwrite an invalid prior selection.
        backup = state / 'lkg-active.rollback.json'
        if backup.exists():
            raise ValueError('Unresolved LKG activation backup; inspect before retrying')
        try:
            atomic_bytes(backup, json.dumps({'previous': previous.decode('utf-8') if previous is not None else None,
                                          'candidate': record}, ensure_ascii=False).encode('utf-8'))
            atomic_bytes(active, json.dumps(record, ensure_ascii=False, indent=2).encode('utf-8'))
            probe_fn(root, record['version'], record['expected_tool_count'])
            if resolve_selection(home, allow_pending=True)['snapshot'] != root.name:
                raise ValueError('LKG selection changed during activation')
            backup.unlink()
        except BaseException as failure:
            try:
                if previous is None:
                    active.unlink(missing_ok=True)
                else:
                    atomic_bytes(active, previous)
                backup.unlink(missing_ok=True)
            except BaseException as rollback:
                raise RuntimeError(f'LKG rollback failed; retain {backup}: {rollback}') from failure
            raise
        return record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--home', type=Path)
    parser.add_argument('--source-root', type=Path)
    args = parser.parse_args()
    if bool(args.home) == bool(args.source_root):
        parser.error('Specify exactly one of --home or --source-root')
    print(json.dumps(resolve_selection(args.home) if args.home else probe(args.source_root), ensure_ascii=False))


if __name__ == '__main__':
    main()
