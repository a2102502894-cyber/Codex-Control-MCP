"""Portable snapshot/selection faults; no Scheduler, GUI, or live activation.

Synthetic full_stack_accepted=True tests the gate only. It is not Windows
acceptance evidence and is never written to the user's state directory.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


lkg = load_script('lkg_state')
freezer = load_script('freeze_source_lkg')


def project(path, version='0.2.2', count=3):
    source = path / 'src/codex_control_mcp'
    source.mkdir(parents=True)
    (path / 'pyproject.toml').write_text(f'[project]\nversion="{version}"\n', 'utf-8')
    (source / '__init__.py').write_text(f'__version__={version!r}\n', 'utf-8')
    (source / 'tools.py').write_text(f'TOOL_SPECS={dict.fromkeys([f"tool_{n}" for n in range(count)])!r}\n', 'utf-8')
    (source / 'config.py').write_text('DEFAULT_CONFIG="[http]\\nport=8774"\n', 'utf-8')
    scripts = path / 'scripts'
    scripts.mkdir()
    for name in freezer.RECOVERY_FILES:
        shutil.copy2(ROOT / 'scripts' / name, scripts / name)
    return path


def snapshot(tmp_path, version='0.2.2', count=3, accepted=True, suffix=''):
    root = project(tmp_path / ('project-' + version + suffix), version, count)
    home = tmp_path / 'home'
    tests = {'all_passed': True, 'full_stack_accepted': accepted,
             'scope': 'synthetic fault fixture, not real Windows evidence'}
    return home, freezer.freeze(root, home, tests)


def legacy(home):
    root = home / 'state/lkg-0.2.0/src'
    root.mkdir(parents=True, exist_ok=True)
    (root / 'sentinel').write_bytes(b'old LKG untouched')
    return root


def assert_legacy(root):
    assert (root / 'sentinel').read_bytes() == b'old LKG untouched'


@pytest.mark.parametrize('version,count', [('0.2.0', 47), ('0.2.1', 46), ('0.2.2', 3)])
def test_old_and_new_snapshots_bind_actual_version_count_hash(tmp_path, version, count):
    home, root = snapshot(tmp_path, version, count)
    manifest = lkg.validate_snapshot(root)
    assert manifest['version'] == version
    assert manifest['expected_tool_count'] == count
    assert lkg.probe(root, version, count)['version'] == version
    record = lkg.snapshot_record(root, home / 'state')
    assert record['manifest_sha256'] == hashlib.sha256((root / 'manifest.json').read_bytes()).hexdigest()
    assert record['sha256'] == lkg.tree_manifest(root)[1]


def test_default_legacy_entry_and_successful_single_record_switch(tmp_path):
    home, root = snapshot(tmp_path)
    old = legacy(home)
    selection = lkg.resolve_selection(home)
    assert selection['legacy'] and selection['version'] == '0.2.0'
    assert selection['expected_tool_count'] == 47
    assert Path(selection['source_root']) == old
    calls = []
    def checked(path, version, tools):
        calls.append((path, version, tools))
        return lkg.probe(path, version, tools)
    lkg.activate(home, root, probe_fn=checked)
    assert calls == [(root, '0.2.2', 3)] * 2
    assert lkg.resolve_selection(home)['source_root'] == str(root / 'src')
    assert_legacy(old)
    assert not (home / 'state/lkg-active.rollback.json').exists()


def test_selected_config_pins_candidates_independently_and_does_not_write_config(tmp_path):
    home, root = snapshot(tmp_path)
    lkg.activate(home, root)
    config = {'home': str(home), 'expected_version': '0.2.0', 'expected_tool_count': 47,
              'candidates': [{'name': 'formal_task', 'expected_version': '0.2.0', 'expected_tool_count': 47},
                             {'name': 'current_source_direct', 'expected_version': '0.2.1', 'expected_tool_count': 46},
                             {'name': 'lkg_direct', 'expected_version': '0.2.0', 'expected_tool_count': 47}]}
    raw = json.dumps(config).encode()
    path = home / 'state/core-controller-config.json'
    path.write_bytes(raw)
    selected = lkg.selected_config(config)
    assert selected['candidates'][:2] == config['candidates'][:2]
    assert selected['candidates'][2]['expected_version'] == '0.2.2'
    assert selected['candidates'][2]['expected_tool_count'] == 3
    assert config['candidates'][2]['expected_version'] == '0.2.0'
    assert path.read_bytes() == raw


@pytest.mark.parametrize('has_previous', [False, True])
@pytest.mark.parametrize('failure_at', [1, 2])
def test_pre_and_post_probe_failures_restore_exact_selection(tmp_path, has_previous, failure_at):
    home, root = snapshot(tmp_path)
    old = legacy(home)
    active = home / 'state/lkg-active.json'
    if has_previous:
        _, prior = snapshot(tmp_path, '0.2.0', 47)
        lkg.activate(home, prior)
    before = active.read_bytes() if active.exists() else None
    config = home / 'state/core-controller-config.json'
    config.write_bytes(b'unchanged config')
    calls = []
    def fail(path, version, tools):
        calls.append((version, tools))
        if len(calls) == failure_at:
            raise ValueError('injected probe failure')
        return lkg.probe(path, version, tools)
    with pytest.raises(ValueError, match='injected probe'):
        lkg.activate(home, root, probe_fn=fail)
    assert len(calls) == failure_at
    assert (active.read_bytes() if active.exists() else None) == before
    assert config.read_bytes() == b'unchanged config'
    assert_legacy(old)
    assert not (home / 'state/lkg-active.rollback.json').exists()


@pytest.mark.parametrize('which', ['backup', 'active'])
def test_config_write_failure_never_loses_previous_selection(tmp_path, monkeypatch, which):
    home, prior = snapshot(tmp_path, '0.2.0', 47)
    lkg.activate(home, prior)
    _, root = snapshot(tmp_path)
    active = home / 'state/lkg-active.json'
    before = active.read_bytes()
    real = lkg.atomic_bytes
    failed = False
    def fault(path, data):
        nonlocal failed
        wanted = 'lkg-active.rollback.json' if which == 'backup' else 'lkg-active.json'
        if Path(path).name == wanted and not failed:
            failed = True
            raise OSError('injected selection write')
        return real(path, data)
    monkeypatch.setattr(lkg, 'atomic_bytes', fault)
    with pytest.raises(OSError, match='injected selection'):
        lkg.activate(home, root)
    assert active.read_bytes() == before
    assert lkg.resolve_selection(home)['version'] == '0.2.0'


def test_atomic_replace_failure_keeps_bytes_and_removes_temporary(tmp_path, monkeypatch):
    target = tmp_path / 'config.json'
    target.write_bytes(b'previous')
    def fail(*args):
        raise OSError('replace fault')
    monkeypatch.setattr(lkg.os, 'replace', fail)
    with pytest.raises(OSError, match='replace fault'):
        lkg.atomic_bytes(target, b'candidate')
    assert target.read_bytes() == b'previous'
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize('which', ['backup', 'active'])
def test_write_error_after_replace_also_restores_selection(tmp_path, monkeypatch, which):
    home, prior = snapshot(tmp_path, '0.2.0', 47)
    lkg.activate(home, prior)
    _, root = snapshot(tmp_path)
    active = home / 'state/lkg-active.json'
    before = active.read_bytes()
    real = lkg.atomic_bytes
    failed = False
    def fault(path, data):
        nonlocal failed
        result = real(path, data)
        wanted = 'lkg-active.rollback.json' if which == 'backup' else 'lkg-active.json'
        if Path(path).name == wanted and not failed:
            failed = True
            raise OSError('injected post-replace durability fault')
        return result
    monkeypatch.setattr(lkg, 'atomic_bytes', fault)
    with pytest.raises(OSError, match='post-replace'):
        lkg.activate(home, root)
    assert active.read_bytes() == before
    assert lkg.resolve_selection(home)['version'] == '0.2.0'
    assert not (home / 'state/lkg-active.rollback.json').exists()


@pytest.mark.parametrize('stage', ['copy', 'rename', 'probe'])
def test_candidate_intermediate_failure_does_not_touch_old_lkg(tmp_path, monkeypatch, stage):
    root = project(tmp_path / 'project')
    home = tmp_path / 'home'
    old = legacy(home)
    def fail(*args, **kwargs):
        raise OSError('candidate fault')
    if stage == 'copy':
        monkeypatch.setattr(freezer.shutil, 'copy2', fail)
    elif stage == 'rename':
        monkeypatch.setattr(Path, 'rename', fail)
    with pytest.raises(OSError, match='candidate fault'):
        freezer.freeze(root, home, {'all_passed': True, 'full_stack_accepted': False},
                       **({'probe_fn': fail} if stage == 'probe' else {}))
    assert_legacy(old)
    assert sorted(p.name for p in (home / 'state').iterdir()) == ['lkg-0.2.0']


@pytest.mark.parametrize('tamper', ['source', 'manifest', 'pointer', 'path'])
def test_fingerprint_or_path_mismatch_fails_closed(tmp_path, tamper):
    home, root = snapshot(tmp_path)
    lkg.activate(home, root)
    active = home / 'state/lkg-active.json'
    if tamper == 'source':
        (root / 'src/codex_control_mcp/tools.py').write_text('TOOL_SPECS={}\n', 'utf-8')
    elif tamper == 'manifest':
        manifest = json.loads((root / 'manifest.json').read_text())
        manifest['tests']['full_stack_accepted'] = False
        (root / 'manifest.json').write_text(json.dumps(manifest))
    else:
        record = json.loads(active.read_text())
        record['version' if tamper == 'pointer' else 'snapshot'] = '0.9.0' if tamper == 'pointer' else '../outside'
        active.write_text(json.dumps(record))
    with pytest.raises(ValueError, match='fingerprint|path'):
        lkg.resolve_selection(home)


def test_version_and_tools_probe_mismatch_rejected(tmp_path):
    home, root = snapshot(tmp_path)
    for version, tools in [('0.9.0', 3), ('0.2.2', 47)]:
        with pytest.raises(ValueError, match='mismatch'):
            lkg.probe(root, version, tools)


def test_no_windows_evidence_blocks_activation_before_probe(tmp_path):
    home, root = snapshot(tmp_path, accepted=False)
    old = legacy(home)
    def forbidden(*args):
        pytest.fail('must not probe or dispatch after failed acceptance gate')
    with pytest.raises(ValueError, match='acceptance'):
        lkg.activate(home, root, probe_fn=forbidden)
    assert_legacy(old)
    assert not (home / 'state/lkg-active.json').exists()


def test_immutable_evidence_creates_distinct_candidate_not_overwrite(tmp_path):
    source = project(tmp_path / 'project')
    home = tmp_path / 'home'
    one = freezer.freeze(source, home, {'all_passed': True, 'full_stack_accepted': False})
    original = (one / 'manifest.json').read_bytes()
    two = freezer.freeze(source, home, {'all_passed': True, 'full_stack_accepted': True, 'scope': 'synthetic'})
    assert one != two
    assert lkg.tree_manifest(one)[1] == lkg.tree_manifest(two)[1]
    assert (one / 'manifest.json').read_bytes() == original


def test_rollback_failure_reports_backup_and_blocks_restart(tmp_path, monkeypatch):
    home, prior = snapshot(tmp_path, '0.2.0', 47)
    lkg.activate(home, prior)
    _, root = snapshot(tmp_path)
    active = home / 'state/lkg-active.json'
    before = active.read_text()
    real = lkg.atomic_bytes
    writes = 0
    def fail_restore(path, content):
        nonlocal writes
        if Path(path) == active:
            writes += 1
            if writes == 2:
                raise OSError('restore failed')
        return real(path, content)
    calls = 0
    def fail_post(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError('post failure')
    monkeypatch.setattr(lkg, 'atomic_bytes', fail_restore)
    with pytest.raises(RuntimeError, match='rollback failed'):
        lkg.activate(home, root, probe_fn=fail_post)
    backup = home / 'state/lkg-active.rollback.json'
    assert json.loads(backup.read_text())['previous'] == before
    with pytest.raises(ValueError, match='Unresolved'):
        lkg.resolve_selection(home)


def test_shared_maintenance_guard_and_existing_lease_block_selection(tmp_path):
    home, root = snapshot(tmp_path)
    state = home / 'state'
    with lkg.maintenance_guard(state):
        with pytest.raises(OSError):
            lkg.activate(home, root)
    (state / 'maintenance.lock').write_text('untrusted lease')
    with pytest.raises(ValueError, match='lease exists'):
        lkg.activate(home, root)
    assert not (state / 'lkg-active.json').exists()


def test_current_repository_freeze_is_nonactivating_and_full_stack_false(tmp_path):
    home = tmp_path / 'isolated-home'
    root = freezer.freeze(ROOT, home, {'all_passed': True, 'full_stack_accepted': False})
    manifest = lkg.validate_snapshot(root)
    assert manifest['version'] == '0.2.2'
    assert manifest['expected_tool_count'] == lkg.probe(ROOT)['core_tools']
    assert manifest['tests']['full_stack_accepted'] is False
    assert not (home / 'state/lkg-active.json').exists()
