from __future__ import annotations

import asyncio
import json
import os
import pathlib
import subprocess
import zipfile
from types import SimpleNamespace

import pytest

from codex_control_mcp.bridge import Bridge
from codex_control_mcp.dynamic_mcp import DynamicMCPManager
from codex_control_mcp.errors import BridgeError
from codex_control_mcp.host_runtime import HostManager
from codex_control_mcp.skill_manager import SkillManager


def dynamic(tmp_path, schema=None):
    manager = DynamicMCPManager(SimpleNamespace(home=tmp_path))
    manager.manage({'action': 'register', 'name': 'node', 'transport': 'stdio', 'command': 'python'})
    manager.servers['node'].update(status='ready', tools=[{'name': 'echo', 'inputSchema': schema or {'type': 'object'}}])
    return manager


def test_dynamic_validation_never_returns_argument_values(tmp_path):
    manager = dynamic(tmp_path, {'type': 'object', 'properties': {'password': {'type': 'integer'}}})
    with pytest.raises(BridgeError) as info:
        manager.call({'name': 'node:echo', 'arguments': {'password': 'synthetic-private-value'}})
    assert 'synthetic-private-value' not in json.dumps(info.value.as_dict())


def test_dynamic_configuration_invalidates_cached_tools(tmp_path):
    manager = dynamic(tmp_path)
    manager.manage({'action': 'update', 'name': 'node', 'command': 'other-python'})
    assert manager.servers['node']['tools'] == []
    assert manager.servers['node']['status'] == 'never_refreshed'


def test_dynamic_public_url_hides_credentials(tmp_path):
    manager = DynamicMCPManager(SimpleNamespace(home=tmp_path))
    out = manager.manage({'action': 'register', 'name': 'node', 'transport': 'streamable_http',
                          'url': 'https://alice:synthetic-password@example.test/mcp?token=synthetic-query'})
    assert 'synthetic-password' not in json.dumps(out)
    assert 'synthetic-query' not in json.dumps(out)


def test_dynamic_refresh_collects_all_pages(tmp_path, monkeypatch):
    manager = dynamic(tmp_path)
    calls = []
    class Tool:
        def __init__(self, name): self.name = name
        def model_dump(self, **_): return {'name': self.name, 'inputSchema': {'type': 'object'}}
    class Session:
        async def list_tools(self, cursor=None):
            calls.append(cursor)
            return SimpleNamespace(tools=[Tool('first' if cursor is None else 'second')], nextCursor='page-2' if cursor is None else None)
    monkeypatch.setattr(manager, '_run', lambda item, operation: asyncio.run(operation(Session())))
    result = manager.refresh('node')
    assert [t['name'] for t in result['tools']] == ['first', 'second']
    assert calls == [None, 'page-2']


def test_dynamic_repeated_cursor_fails_without_publishing_partial_cache(tmp_path, monkeypatch):
    manager = dynamic(tmp_path)
    class Session:
        async def list_tools(self, cursor=None): return SimpleNamespace(tools=[], nextCursor='same')
    monkeypatch.setattr(manager, '_run', lambda item, operation: asyncio.run(operation(Session())))
    with pytest.raises(BridgeError): manager.refresh('node')
    assert manager.servers['node']['tools'][0]['name'] == 'echo'


def test_dynamic_schema_cannot_resolve_external_references(tmp_path, monkeypatch):
    manager = dynamic(tmp_path, {'$ref': (tmp_path / 'missing-schema.json').as_uri()})
    monkeypatch.setattr(manager, '_run', lambda *a, **k: pytest.fail('External schema reached execution'))
    with pytest.raises(BridgeError) as info: manager.call({'name': 'node:echo', 'arguments': {}})
    assert info.value.code == 'mcp_schema_invalid'


class FakeBridge:
    def __init__(self, tmp_path):
        self.cfg = SimpleNamespace(home=tmp_path)
        self.calls = []
    def _do(self, tool, args):
        self.calls.append((tool, args))
        return {'exit_code': 0, 'stdout': ''}


def shell_host(tmp_path, platform='windows'):
    bridge = FakeBridge(tmp_path)
    manager = HostManager(bridge, None)
    manager.manage({'action': 'register', 'name': 'node', 'transport': 'docker', 'container': 'fixture', 'platform': platform})
    return manager


@pytest.mark.parametrize('action,option', [('read', {'recursive': True}), ('list', {'content': 'ignored'}), ('delete', {'query': 'ignored'})])
def test_host_files_rejects_irrelevant_options(tmp_path, action, option):
    manager = shell_host(tmp_path)
    with pytest.raises(BridgeError): manager.files({'host': 'node', 'action': action, 'path': 'file', **option})
    assert manager.bridge.calls == []


def test_host_local_force_write_not_silently_ignored(tmp_path):
    manager = HostManager(FakeBridge(tmp_path), None)
    with pytest.raises(BridgeError): manager.files({'host': 'local', 'action': 'write', 'path': 'file', 'content': 'x', 'force': True})
    assert manager.bridge.calls == []


def test_host_recursive_delete_and_missing_force_are_distinct(tmp_path):
    manager = shell_host(tmp_path)
    command = []
    manager._remote_command = lambda item, text, options: command.append(text) or {'exit_code': 0}
    manager.files({'host': 'node', 'action': 'delete', 'path': 'dir', 'recursive': True, 'force': True})
    assert '-Recurse' in command[0]
    assert 'SilentlyContinue' not in command[0]


def run_windows(manager, command, options):
    script = manager._remote_script(manager.hosts['node'], command, options)
    result = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', script], capture_output=True, text=True, encoding="utf-8", errors="replace",
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode: raise BridgeError('command_failed', 'Test command failed')
    return {'exit_code': result.returncode, 'stdout': result.stdout}


@pytest.mark.skipif(os.name != 'nt', reason='Windows shell behavior')
def test_remote_force_delete_does_not_hide_nonempty_directory_error(tmp_path):
    manager = shell_host(tmp_path)
    manager._remote_command = lambda item, command, options: run_windows(manager, command, options)
    target = tmp_path / 'nonempty'; target.mkdir(); (target / 'keep.txt').write_text('keep')
    with pytest.raises(BridgeError): manager.files({'host': 'node', 'action': 'delete', 'path': str(target), 'force': True, 'recursive': False})
    assert (target / 'keep.txt').read_text() == 'keep'


def skill_source(tmp_path, version='1'):
    source = tmp_path / ('source-' + version); source.mkdir()
    (source / 'SKILL.md').write_text('---\nname: fixture\nversion: ' + version + '\n---\ncontent-' + version, encoding='utf-8')
    return source


def test_skill_digest_and_install_exclude_git_files(tmp_path):
    manager = SkillManager(SimpleNamespace(home=tmp_path / 'home'))
    source = skill_source(tmp_path); (source / '.git').mkdir(); (source / '.git' / 'private').write_text('synthetic-private-value')
    result = manager.install({'source': str(source), 'activate': False})
    assert not (pathlib.Path(result['path']) / '.git').exists()


def test_skill_archive_rejects_oversized_before_extraction(tmp_path, monkeypatch):
    manager = SkillManager(SimpleNamespace(home=tmp_path / 'home'))
    archive = tmp_path / 'source.zip'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as out:
        out.writestr('SKILL.md', '---\nname: fixture\n---\n' + 'a' * 4096)
    monkeypatch.setattr(zipfile.ZipFile, 'extractall', lambda *a, **k: pytest.fail('Oversized archive extracted before budget validation'))
    with pytest.raises(BridgeError) as info: manager.validate({'source': str(archive), 'max_bytes': 1024})
    assert info.value.code == 'skill_too_large'


@pytest.mark.parametrize('member', ['../source-escape/evil', 'pkg/../../evil', r'..\evil', 'C:/evil'])
def test_skill_archive_rejects_unsafe_paths(tmp_path, member):
    manager = SkillManager(SimpleNamespace(home=tmp_path / 'home'))
    archive = tmp_path / 'source.zip'
    with zipfile.ZipFile(archive, 'w') as out:
        out.writestr('SKILL.md', '---\nname: fixture\n---\ncontent'); out.writestr(member, 'evil')
    with pytest.raises(BridgeError): manager.validate({'source': str(archive)})


def test_skill_failed_activation_preserves_runtime_and_state(tmp_path, monkeypatch):
    manager = SkillManager(SimpleNamespace(home=tmp_path / 'home'))
    manager.install({'source': str(skill_source(tmp_path, '1'))})
    manager.install({'source': str(skill_source(tmp_path, '2')), 'activate': False})
    before = manager.state_path.read_bytes()
    original = pathlib.Path.replace
    def fail_install(path, target):
        if '.sync-' in path.name: raise OSError('controlled switch failure')
        return original(path, target)
    monkeypatch.setattr(pathlib.Path, 'replace', fail_install)
    with pytest.raises(OSError): manager.activate({'skill': 'fixture', 'version': '2'})
    assert manager.state_path.read_bytes() == before
    assert manager.state['skills']['fixture']['active'] == '1'
    assert 'content-1' in (manager.runtime_root / 'fixture' / 'SKILL.md').read_text('utf-8')


def test_read_long_unicode_line_can_continue_without_losing_bytes(tmp_path):
    bridge = Bridge.__new__(Bridge); bridge.cfg = SimpleNamespace(cwd=str(tmp_path))
    body = ('你好🙂' * 700) + '\nnext\n'; bridge._read_bytes = lambda path: body.encode('utf-8')
    first = bridge._do('read_file', {'path': str(tmp_path / 'file'), 'max_bytes': 1024})
    pages = [first['content']]; result = first
    for _ in range(20):
        if result.get('next_utf8_offset') is None: break
        result = bridge._do('read_file', {'path': str(tmp_path / 'file'), 'max_bytes': 1024,
                                        'utf8_offset': result['next_utf8_offset'], 'expected_sha256': first['sha256']})
        pages.append(result['content'])
    assert ''.join(pages) == body


def test_read_continuation_rejects_changed_content_and_split_character(tmp_path):
    bridge = Bridge.__new__(Bridge); bridge.cfg = SimpleNamespace(cwd=str(tmp_path))
    bridge._read_bytes = lambda path: ('你' * 700).encode('utf-8')
    first = bridge._do('read_file', {'path': str(tmp_path / 'file'), 'max_bytes': 1024})
    with pytest.raises(BridgeError) as info:
        bridge._do('read_file', {'path': str(tmp_path / 'file'), 'utf8_offset': 1, 'expected_sha256': first['sha256']})
    assert info.value.code == 'invalid_arguments'
    bridge._read_bytes = lambda path: b'changed'
    with pytest.raises(BridgeError) as info: bridge._do('read_file', first['next_action']['arguments'])
    assert info.value.code == 'concurrent_modification'


def test_dynamic_false_schema_is_not_replaced_with_permissive_default(tmp_path, monkeypatch):
    manager = dynamic(tmp_path); manager.servers['node']['tools'][0]['inputSchema'] = False
    monkeypatch.setattr(manager, '_run', lambda *a, **k: pytest.fail('False schema allowed tool execution'))
    with pytest.raises(BridgeError): manager.call({'name': 'node:echo', 'arguments': {}})


def test_dynamic_local_schema_refs_still_work(tmp_path, monkeypatch):
    manager = dynamic(tmp_path, {'type': 'object', 'properties': {'x': {'$ref': '#/definitions/x'}}, 'definitions': {'x': {'type': 'integer'}}})
    monkeypatch.setattr(manager, '_run', lambda *a, **k: {'isError': False})
    assert manager.call({'name': 'node:echo', 'arguments': {'x': 1}})['result']['isError'] is False


def test_skill_failed_state_commit_restores_runtime_and_allows_retry(tmp_path, monkeypatch):
    manager = SkillManager(SimpleNamespace(home=tmp_path / 'home'))
    manager.install({'source': str(skill_source(tmp_path, '1'))})
    source = skill_source(tmp_path, '2'); before = manager.state_path.read_bytes(); save = manager._save
    monkeypatch.setattr(manager, '_save', lambda: (_ for _ in ()).throw(OSError('controlled state failure')))
    with pytest.raises(OSError): manager.install({'source': str(source)})
    assert manager.state_path.read_bytes() == before
    assert '2' not in manager.state['skills']['fixture']['versions']
    assert not (manager.root / 'fixture' / '2').exists()
    assert 'content-1' in (manager.runtime_root / 'fixture' / 'SKILL.md').read_text('utf-8')
    monkeypatch.setattr(manager, '_save', save)
    assert manager.install({'source': str(source)})['activated']


def test_skill_uninstall_state_failure_restores_packages(tmp_path, monkeypatch):
    manager = SkillManager(SimpleNamespace(home=tmp_path / 'home'))
    manager.install({'source': str(skill_source(tmp_path))})
    before = manager.state_path.read_bytes()
    monkeypatch.setattr(manager, '_save', lambda: (_ for _ in ()).throw(OSError('controlled state failure')))
    with pytest.raises(OSError): manager.uninstall({'skill': 'fixture'})
    assert manager.state_path.read_bytes() == before
    assert (manager.root / 'fixture' / '1' / 'SKILL.md').is_file()
    assert (manager.runtime_root / 'fixture' / 'SKILL.md').is_file()


def test_skill_changed_installed_content_cannot_activate(tmp_path):
    manager = SkillManager(SimpleNamespace(home=tmp_path / 'home'))
    result = manager.install({'source': str(skill_source(tmp_path)), 'activate': False})
    (pathlib.Path(result['path']) / 'SKILL.md').write_text('tampered')
    with pytest.raises(BridgeError) as info: manager.activate({'skill': 'fixture', 'version': '1'})
    assert info.value.code == 'skill_digest_mismatch'
    assert manager.state['skills']['fixture']['active'] == ''


def test_skill_directory_rejects_links_without_copying_target(tmp_path):
    manager = SkillManager(SimpleNamespace(home=tmp_path / 'home'))
    source = skill_source(tmp_path); external = tmp_path / 'external'; external.mkdir()
    (external / 'private').write_text('synthetic-private-value')
    link = source / 'linked'
    if os.name == 'nt':
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(external)], capture_output=True,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        assert result.returncode == 0
    else: link.symlink_to(external, target_is_directory=True)
    try:
        with pytest.raises(BridgeError): manager.validate({'source': str(source)})
        assert (external / 'private').read_text() == 'synthetic-private-value'
    finally:
        if os.name == 'nt': link.rmdir()
        else: link.unlink()


@pytest.mark.skipif(os.name != 'nt', reason='Windows shell behavior')
def test_remote_shell_recursive_delete_missing_force_and_unicode_write(tmp_path):
    manager = shell_host(tmp_path)
    manager._remote_command = lambda item, command, options: run_windows(manager, command, options)
    path = tmp_path / 'folder' / 'file.txt'
    manager.files({'host': 'node', 'action': 'write', 'path': str(path), 'content': '中文🙂'})
    assert path.read_text('utf-8') == '中文🙂'
    with pytest.raises(BridgeError): manager.files({'host': 'node', 'action': 'write', 'path': str(path), 'content': 'overwritten'})
    assert path.read_text('utf-8') == '中文🙂'
    manager.files({'host': 'node', 'action': 'write', 'path': str(path), 'content': 'changed', 'force': True})
    assert path.read_text('utf-8') == 'changed'
    manager.files({'host': 'node', 'action': 'delete', 'path': str(path.parent), 'recursive': True})
    assert not path.parent.exists()
    manager.files({'host': 'node', 'action': 'delete', 'path': str(path.parent), 'force': True})
    with pytest.raises(BridgeError): manager.files({'host': 'node', 'action': 'delete', 'path': str(path.parent)})


@pytest.mark.parametrize('state', [[], None, 'invalid'])
def test_schema_cache_recovers_from_nonobject_state(tmp_path, monkeypatch, state):
    from codex_control_mcp import schema
    from codex_control_mcp.config import Config
    from test_unit import schema_fixture
    cfg = Config(home=tmp_path / 'home', cwd=str(tmp_path)); cfg.initialize_storage()
    (cfg.home / 'state' / 'schema.json').write_text(json.dumps(state), encoding='utf-8')
    runtime = SimpleNamespace(file_fingerprint='f' * 64, codex_path='fixture-not-executable')
    def export(args, **_):
        if '--help' in args: return SimpleNamespace(returncode=0, stdout='--out --experimental')
        schema_fixture(pathlib.Path(args[args.index('--out') + 1])); return SimpleNamespace(returncode=0)
    monkeypatch.setattr(schema, 'run_metadata', export)
    registry, info = schema.load_or_export(cfg, runtime)
    assert registry.hash == info['schema_hash']


def test_dynamic_search_reports_bad_node_and_continues_healthy_nodes(tmp_path, monkeypatch):
    manager = dynamic(tmp_path)
    manager.manage({'action': 'register', 'name': 'aaa-offline', 'transport': 'stdio', 'command': 'unavailable'})
    monkeypatch.setattr(manager, 'refresh', lambda name, **kwargs: (_ for _ in ()).throw(BridgeError('mcp_unavailable', 'Controlled offline fixture')))
    result = manager.search({'query': 'echo'})
    assert [tool['qualified_name'] for tool in result['tools']] == ['node:echo']
    assert result['partial'] and result['errors'][0]['server'] == 'aaa-offline'


@pytest.mark.parametrize('fields', [{'transport': 'docker', 'container': '--privileged'},
                                    {'transport': 'ssh', 'user': '-oProxyCommand=x', 'address': 'example.test'},
                                    {'transport': 'ssh', 'user': 'owner', 'address': 'example.test invalid'}])
def test_host_destination_cannot_be_parsed_as_options(tmp_path, fields):
    manager = HostManager(FakeBridge(tmp_path), None)
    with pytest.raises(BridgeError): manager.manage({'action': 'register', 'name': 'node', **fields})
    assert manager.hosts == {}


@pytest.mark.parametrize('manager_type,filename', [(DynamicMCPManager, 'dynamic-mcp.json'), (SkillManager, 'skills.json'), (HostManager, 'hosts.json')])
def test_registries_do_not_silently_reset_nonobject_data(tmp_path, manager_type, filename):
    (tmp_path / 'state').mkdir(); (tmp_path / 'state' / filename).write_text('[]')
    with pytest.raises(BridgeError):
        if manager_type is HostManager: manager_type(FakeBridge(tmp_path), None)
        else: manager_type(SimpleNamespace(home=tmp_path))
    assert (tmp_path / 'state' / filename).read_text() == '[]'


@pytest.mark.parametrize('manager_type', [DynamicMCPManager, HostManager])
def test_registry_failed_save_does_not_publish_memory_changes(tmp_path, monkeypatch, manager_type):
    if manager_type is HostManager:
        import codex_control_mcp.host_runtime as module
        manager = manager_type(FakeBridge(tmp_path), None)
        request = {'action': 'register', 'name': 'node', 'transport': 'docker', 'container': 'fixture'}
        attribute = 'hosts'
    else:
        import codex_control_mcp.dynamic_mcp as module
        manager = manager_type(SimpleNamespace(home=tmp_path))
        request = {'action': 'register', 'name': 'node', 'transport': 'stdio', 'command': 'python'}
        attribute = 'servers'
    save = module.atomic_json
    monkeypatch.setattr(module, 'atomic_json', lambda *a, **k: (_ for _ in ()).throw(OSError('controlled save failure')))
    with pytest.raises(OSError): manager.manage(request)
    assert getattr(manager, attribute) == {}
    monkeypatch.setattr(module, 'atomic_json', save)
    assert manager.manage(request)['action'] == 'register'


def test_skill_rollback_does_not_require_deleting_new_runtime(tmp_path, monkeypatch):
    import codex_control_mcp.skill_manager as module
    manager = SkillManager(SimpleNamespace(home=tmp_path / 'home'))
    manager.install({'source': str(skill_source(tmp_path, '1'))})
    manager.install({'source': str(skill_source(tmp_path, '2')), 'activate': False})
    before = manager.state_path.read_bytes(); target = manager.runtime_root / 'fixture'
    real_remove = module.shutil.rmtree
    def refuse_runtime_delete(path, *args, **kwargs):
        if pathlib.Path(path) == target: raise PermissionError('controlled locked file')
        return real_remove(path, *args, **kwargs)
    monkeypatch.setattr(module.shutil, 'rmtree', refuse_runtime_delete)
    monkeypatch.setattr(manager, '_save', lambda: (_ for _ in ()).throw(OSError('controlled save failure')))
    with pytest.raises(OSError): manager.activate({'skill': 'fixture', 'version': '2'})
    assert manager.state_path.read_bytes() == before
    assert 'content-1' in (target / 'SKILL.md').read_text('utf-8')


def test_skill_failed_restore_returns_recovery_paths(tmp_path, monkeypatch):
    manager = SkillManager(SimpleNamespace(home=tmp_path / 'home'))
    manager.install({'source': str(skill_source(tmp_path, '1'))})
    manager.install({'source': str(skill_source(tmp_path, '2')), 'activate': False})
    real_replace = pathlib.Path.replace
    def fail_switch_and_restore(path, target):
        if '.sync-' in path.name or '.backup-' in path.name: raise PermissionError('controlled rename failure')
        return real_replace(path, target)
    monkeypatch.setattr(pathlib.Path, 'replace', fail_switch_and_restore)
    with pytest.raises(BridgeError) as info: manager.activate({'skill': 'fixture', 'version': '2'})
    assert info.value.code == 'skill_switch_unverified'
    assert pathlib.Path(info.value.details['backup_path']).is_dir()


def test_dynamic_pathological_validation_has_deadline_and_never_dispatches(tmp_path, monkeypatch):
    import time
    manager = dynamic(tmp_path, {'type': 'object', 'properties': {'x': {'type': 'string', 'pattern': '(a+)+$'}}})
    manager.servers['node']['timeout_ms'] = 1000
    monkeypatch.setattr(manager, '_run', lambda *a, **k: pytest.fail('Invalid input reached tool dispatch'))
    start = time.monotonic()
    with pytest.raises(BridgeError) as info: manager.call({'name': 'node:echo', 'arguments': {'x': 'a' * 80 + '!'}})
    assert info.value.code == 'mcp_validation_timeout'
    assert time.monotonic() - start < 3


def test_dynamic_refresh_cannot_publish_schema_for_replaced_connection(tmp_path, monkeypatch):
    manager = dynamic(tmp_path)
    def changed(item, operation):
        manager.manage({'action': 'update', 'name': 'node', 'command': 'different-python'})
        return [{'name': 'old-tool', 'inputSchema': {'type': 'object'}}]
    monkeypatch.setattr(manager, '_run', changed)
    with pytest.raises(BridgeError) as info: manager.refresh('node')
    assert info.value.code == 'mcp_registry_changed'
    assert manager.servers['node']['tools'] == []


def test_frozen_validation_entrypoint_is_private_computation_only(monkeypatch):
    import sys
    from codex_control_mcp.dynamic_mcp import validation_argv
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    assert validation_argv() == [sys.executable, '--internal-validate-mcp-arguments']
    result = subprocess.run([sys.executable, '-m', 'codex_control_mcp', '--internal-validate-mcp-arguments'],
                            input=json.dumps([{'type': 'object', 'properties': {'password': {'type': 'integer'}}},
                                              {'password': 'synthetic-private-value'}]).encode(),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert result.returncode == 0
    assert json.loads(result.stdout) == {'valid': False, 'rule': 'type'}
    assert b'synthetic-private-value' not in result.stdout + result.stderr


@pytest.mark.parametrize('archive', [False, True])
def test_skill_package_preserves_empty_directories(tmp_path, archive):
    manager = SkillManager(SimpleNamespace(home=tmp_path / 'home'))
    source = skill_source(tmp_path); (source / 'data').mkdir()
    if archive:
        path = tmp_path / 'source.zip'
        with zipfile.ZipFile(path, 'w') as out:
            out.write(source / 'SKILL.md', 'SKILL.md'); out.writestr('data/', '')
        source = path
    result = manager.install({'source': str(source)})
    assert (pathlib.Path(result['path']) / 'data').is_dir()
    assert (manager.runtime_root / 'fixture' / 'data').is_dir()


@pytest.mark.parametrize('dialect', ['https://json-schema.org/draft/2020-12/schema', 'https://example.test/unsupported-dialect'])
def test_dynamic_declared_dialect_is_respected_or_rejected(tmp_path, monkeypatch, dialect):
    manager = dynamic(tmp_path, {'$schema': dialect, 'type': 'object', 'properties': {'x': {'type': 'integer'}}, 'unevaluatedProperties': False})
    monkeypatch.setattr(manager, '_run', lambda *a, **k: pytest.fail('Unsupported or invalid schema allowed dispatch'))
    with pytest.raises(BridgeError) as info: manager.call({'name': 'node:echo', 'arguments': {'x': 1, 'unexpected': 2}})
    assert info.value.code == ('invalid_arguments' if '2020-12' in dialect else 'mcp_schema_invalid')
