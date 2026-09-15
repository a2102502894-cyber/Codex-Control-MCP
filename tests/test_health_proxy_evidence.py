"""Unit tests only: synthetic probe results never count as production acceptance."""
import json
from types import SimpleNamespace
import pytest
from codex_control_mcp.proxy_probe import route_is_verified, probe_argv
from codex_control_mcp.bridge import Bridge

GOOD = dict(connect_verified=True, tls_certificate_verified=True, tls_version='TLSv1.3',
            socket_peer_host='127.0.0.1', socket_peer_port=7897,
            proxy_host_from_environment='127.0.0.1', proxy_port_from_environment=7897,
            http_status=200)


def test_exact_connect_tls_peer_evidence_required():
    assert route_is_verified(GOOD)
    assert not route_is_verified({'http_status': 200})
    assert not route_is_verified(None)
    assert probe_argv()[1] == '-c'


@pytest.mark.parametrize('change', [
    {'connect_verified': False}, {'tls_certificate_verified': False},
    {'socket_peer_host': '1.1.1.1'}, {'socket_peer_port': 443},
    {'tls_version': None}, {'http_status': 502}, {'http_status': '200'},
    {'proxy_port_from_environment': None},
])
def test_direct_or_unverified_route_rejected(change):
    assert not route_is_verified({**GOOD, **change})


def make_bridge(route):
    b = object.__new__(Bridge)
    b.ensure_ready = lambda **kw: None
    b.browser = SimpleNamespace(verified=True)
    b.gui = SimpleNamespace(verified=True)
    b.cfg = SimpleNamespace(cwd='C:/', browser_use_enabled=True, computer_use_enabled=True,
                            application_access_policy='owner_preapproved', requires_client_elicitation=False)
    b.proxy = {'env_names_present': ['HTTPS_PROXY']}
    b._rpc = lambda *a: {}
    responses = iter([
        {'exit_code': 0, 'stdout': 'CODEX_CONTROL_MCP_OK\nTrue'},
        {'exit_code': 0, 'stdout': '{"HTTPS_PROXY":true}'}, route])
    b._command = lambda *a, **kw: next(responses)
    b.audit = SimpleNamespace(methods={})
    b.runtime = SimpleNamespace(as_dict=lambda: {})
    b.rpc = SimpleNamespace(proc=SimpleNamespace(pid=123))
    b.schema_info = {}
    b.pending_update = None
    return b


def test_health_pass_requires_actual_probe_fields():
    b = make_bridge({'exit_code': 0, 'stdout': json.dumps(GOOD)})
    out = b.health(active=True)
    assert out['checks']['proxy'] == 'PASS'
    assert out['overall'] == 'healthy' and out['phase1_complete']
    assert out['proxy']['route_evidence'] == GOOD


@pytest.mark.parametrize('route', [
    {'exit_code': 0, 'stdout': 'PROXY_ROUTE_OK'},
    {'exit_code': 1, 'stdout': json.dumps(GOOD)},
    {'exit_code': 0, 'stdout': json.dumps({'http_status': 200})},
])
def test_health_cannot_promote_http_success_without_route_proof(route):
    out = make_bridge(route).health(active=True)
    assert out['checks']['proxy'] != 'PASS'
    assert out['overall'] != 'healthy' and not out['phase1_complete']
