from codex_control_mcp.config import Config, build_environment
from codex_control_mcp.bridge import Bridge


def test_proxy_environment_is_forwarded_to_wsl_without_changing_parent(tmp_path, monkeypatch):
    monkeypatch.setenv('HTTP_PROXY', 'http://127.0.0.1:18234')
    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:18234')
    monkeypatch.setenv('WSLENV', 'EXISTING/lpu:HTTP_PROXY/w')
    import os
    parent = dict(os.environ)
    env, _ = build_environment(Config(home=tmp_path, cwd=str(tmp_path), inherit_system_proxy=False))
    entries = env['WSLENV'].split(':')
    assert entries[:2] == ['EXISTING/lpu', 'HTTP_PROXY/w']
    assert sum(entry.split('/')[0] == 'HTTP_PROXY' for entry in entries) == 1
    assert 'HTTPS_PROXY/u' in entries and 'NO_PROXY/u' in entries
    assert env['HTTPS_PROXY'] == 'http://127.0.0.1:18234'
    assert env['WSL_UTF8'] == '1'
    assert dict(os.environ) == parent


def test_empty_proxy_is_not_added_and_existing_sharing_flags_are_preserved(tmp_path, monkeypatch):
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('WSLENV', 'KEEP/p:NO_PROXY/up')
    env, _ = build_environment(Config(home=tmp_path, cwd=str(tmp_path), inherit_system_proxy=False))
    assert env['WSLENV'] == 'KEEP/p:NO_PROXY/up'


def test_per_command_wsl_environment_preserves_explicit_owner_control(tmp_path):
    bridge = Bridge(Config(home=tmp_path, cwd=str(tmp_path)))
    supplied = {'CCM_EXPLICIT': '中文', 'CCM_REMOVE': None}
    try:
        forwarded = bridge._command_environment({'shell': 'wsl', 'env': supplied})
        assert forwarded['CCM_EXPLICIT'] == '中文' and forwarded['CCM_REMOVE'] is None
        assert 'CCM_EXPLICIT/u' in forwarded['WSLENV'].split(':')
        assert 'CCM_REMOVE/u' in forwarded['WSLENV'].split(':')
        assert 'WSLENV' not in supplied
        explicit = {'WSLENV': 'OWNER_ONLY/u', 'CCM_EXPLICIT': 'value'}
        assert bridge._command_environment({'shell': 'wsl', 'env': explicit}) == explicit
        assert bridge._command_environment({'shell': 'powershell', 'env': supplied}) == supplied
    finally:
        bridge.close()
