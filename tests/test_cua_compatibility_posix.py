import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
from mcp import types
from codex_control_mcp.bridge import Bridge
from codex_control_mcp.common import digest
from codex_control_mcp.computer import select_manifest, probe_tool_contract
from codex_control_mcp.errors import BridgeError
from codex_control_mcp.tools import validate_tool


def manifest(base, version, data):
    folder = base / version
    folder.mkdir()
    path = folder / '.mcp.json'
    path.write_text(json.dumps(data))
    return path


def test_explicit_verified_manifest_never_latest(tmp_path):
    old = manifest(tmp_path, 'verified', {'mcpServers': {'cua_repl': {'env': {'CUA_REPL_NODE_REPL_PATH': 'fixture'}}}})
    manifest(tmp_path, 'new-unknown', {'mcpServers': {'unexpected': {}}})
    selected, cfg = select_manifest(tmp_path, {'version': 'verified', 'manifest_sha256': digest(old.read_bytes())})
    assert selected == old and cfg['env']['CUA_REPL_NODE_REPL_PATH'] == 'fixture'
    with pytest.raises(BridgeError) as exc:
        select_manifest(tmp_path, {})
    assert exc.value.code == 'version_incompatible'
    old.write_text('{}')
    with pytest.raises(BridgeError, match='fingerprint'):
        select_manifest(tmp_path, {'version': 'verified', 'manifest_sha256': digest(b'old')})


@pytest.mark.parametrize('version', ['../escape', '..', '/', 'missing'])
def test_incompatible_manifest_explains_no_fallback(tmp_path, version):
    with pytest.raises(BridgeError) as exc:
        select_manifest(tmp_path, {'version': version, 'manifest_sha256': 'f' * 64})
    assert exc.value.code == 'version_incompatible'


def test_tool_contract_schema_probe():
    tool = types.Tool(name='js', inputSchema={'type': 'object', 'properties': {'code': {'type': 'string'}, 'title': {'type': 'string'}, 'timeout_ms': {'type': 'integer'}}, 'required': ['code'], 'additionalProperties': False})
    probe_tool_contract([tool])
    with pytest.raises(BridgeError):
        probe_tool_contract([types.Tool(name='js', inputSchema={'type': 'object'})])
    tool.inputSchema['properties']['timeout_ms'] = {'type': 'string'}
    with pytest.raises(BridgeError):
        probe_tool_contract([tool])


@pytest.mark.skipif(os.name == 'nt', reason='POSIX execution fixture')
def test_posix_shell_unicode_and_literal_argv(tmp_path):
    b = Bridge.__new__(Bridge)
    validate_tool('exec_command', {'shell': 'sh', 'command': 'printf 中文'})
    argv = b._argv({'command': 'printf 中文'})
    assert argv[0] == '/bin/sh'
    assert subprocess.check_output(argv).decode() == '中文'
    assert b._argv({'argv': [sys.executable, '-c', 'print(1)']})[0] == sys.executable
    # A leading dash, spaces and shell syntax in paths remain literal argv.
    source, destination = tmp_path / "-中文 $(false) file", tmp_path / "destination ' $(false)"
    source.write_text('original')
    size = subprocess.check_output(b._file_size_argv(str(source))).decode().strip()
    assert int(size) == len(b'original')
    subprocess.check_call(b._move_file_argv(str(source), str(destination)))
    assert destination.read_text() == 'original' and not source.exists()
    source.write_text('new')
    result = subprocess.run(b._move_file_argv(str(source), str(destination)), capture_output=True)
    assert result.returncode != 0 and source.read_text() == 'new' and destination.read_text() == 'original'


def test_archived_session_control_does_not_start_runtime(tmp_path):
    from codex_control_mcp.sessions import SessionStore
    b = Bridge.__new__(Bridge)
    b.sessions = SessionStore(tmp_path / 'sessions.json', 2048, 2)
    s = b.sessions.create('g', '.')
    s.archived = True
    b.ensure_ready = lambda: pytest.fail('archived control must not start runtime')
    for tool in ('session_write', 'session_kill', 'session_resize'):
        with pytest.raises(BridgeError) as exc:
            b._do(tool, {'session_id': s.id})
        assert exc.value.code == 'session_lost'
    b.sessions.close()
