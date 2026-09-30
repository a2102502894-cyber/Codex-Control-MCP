"""Unit tests for controller policy and lease handling.

Scheduler and port calls are mocked here; these tests are not production recovery
acceptance.  The parent handoff contains separate real Scheduled Task commands.
"""
from __future__ import annotations

from datetime import timedelta
import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import create_autospec

ROOT = Path(__file__).resolve().parents[1]
import pytest
pytest.importorskip("msvcrt", reason="Windows controller OS lease tests")

SPEC = importlib.util.spec_from_file_location(
    "core_recovery_controller", ROOT / "scripts/core_recovery_controller.py"
)
controller = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(controller)


def test_empty_lock_is_reclaimed_and_owner_release_is_safe(tmp_path):
    path = tmp_path / "maintenance.lock"
    path.write_bytes(b"")
    lease = controller.Lease(path, seconds=30)
    assert lease.acquire()
    record = json.loads(path.read_text("utf-8"))
    assert record["pid"] == os.getpid()
    assert record["owner"].startswith("core-controller:")
    assert "expires_at" in record
    lease.release()
    assert not path.exists()


def test_live_unexpired_lease_is_not_stolen(tmp_path):
    path = tmp_path / "maintenance.lock"
    path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "owner": "another-controller",
                "expires_at": controller.iso(controller.utc_now() + timedelta(minutes=2)),
            }
        ),
        "utf-8",
    )
    lease = controller.Lease(path)
    assert not lease.acquire()
    assert json.loads(path.read_text("utf-8"))["owner"] == "another-controller"


def make_config(tmp_path):
    home = tmp_path / "home"
    (home / "state").mkdir(parents=True)
    config = {
        "home": str(home),
        "port": 18774,
        "expected_version": "0.2.0",
        "lease": str(home / "state/maintenance.lock"),
        "report": str(home / "state/controller-report.json"),
        "request": str(home / "state/controller-request.json"),
        "host_tasks": ["formal", "current", "lkg"],
        "candidates": [
            {"name": "formal_task", "task": "formal", "timeout_seconds": 1},
            {"name": "current_source_direct", "task": "current", "timeout_seconds": 1},
            {"name": "lkg_direct", "task": "lkg", "timeout_seconds": 1},
        ],
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), "utf-8")
    return path, config


def test_deterministic_formal_current_lkg_order(monkeypatch, tmp_path):
    path, config = make_config(tmp_path)
    calls = []
    monkeypatch.setattr(controller, "port_pid", lambda port: 0)
    monkeypatch.setattr(controller, "wait_port", lambda port, wanted, seconds: 1234 if wanted else 0)

    def task_command(verb, task):
        calls.append((verb, task))
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    outcomes = iter([(False, "formal_bad"), (False, "current_bad"), (True, "ready")])
    counts = []
    def verify(candidate, pid, expected_tool_count=None):
        counts.append(expected_tool_count)
        return next(outcomes)
    monkeypatch.setattr(controller, "task_command", task_command)
    monkeypatch.setattr(controller, "verify_service", create_autospec(controller.verify_service, side_effect=verify))
    assert controller.run(path) == controller.EXIT_OK
    report = json.loads(Path(config["report"]).read_text("utf-8"))
    assert [item["name"] for item in report["attempts"]] == [
        "formal_task",
        "current_source_direct",
        "lkg_direct",
    ]
    assert report["selected"] == "lkg_direct"
    assert counts == [None, None, 47]
    assert [(v, t) for v, t in calls if v == "Start"] == [
        ("Start", "formal"),
        ("Start", "current"),
        ("Start", "lkg"),
    ]


def test_recover_healthy_is_noop(monkeypatch, tmp_path):
    path, config = make_config(tmp_path)
    monkeypatch.setattr(controller, "port_pid", lambda port: 222)
    monkeypatch.setattr(controller, "verify_service", lambda c, p: (True, "ready"))
    touched = []
    monkeypatch.setattr(controller, "task_command", lambda *a: touched.append(a))
    assert controller.run(path) == controller.EXIT_ALREADY_HEALTHY
    assert touched == []
    report = json.loads(Path(config["report"]).read_text("utf-8"))
    assert report["exit_code"] == controller.EXIT_ALREADY_HEALTHY


def test_old_fallback_keeps_its_own_version_contract(monkeypatch, tmp_path):
    path, config = make_config(tmp_path)
    config['expected_version'] = '0.2.1'
    config['candidates'][-1]['expected_version'] = '0.2.0'
    path.write_text(json.dumps(config), encoding='utf-8')
    monkeypatch.setattr(controller, 'port_pid', lambda port: 0)
    monkeypatch.setattr(controller, 'wait_port', lambda *args: 1234)
    monkeypatch.setattr(controller, 'task_command', lambda *args:
        type('Result', (), {'returncode': 0, 'stdout': '', 'stderr': ''})())
    versions = []
    counts = []
    def verify(candidate, pid, expected_tool_count=None):
        versions.append(candidate['expected_version'])
        counts.append(expected_tool_count)
        return len(versions) == 3, 'fixture'
    monkeypatch.setattr(controller, 'verify_service', create_autospec(controller.verify_service, side_effect=verify))
    assert controller.run(path) == controller.EXIT_OK
    assert versions == ['0.2.1', '0.2.1', '0.2.0']
    assert counts == [None, None, 47]


def test_distinct_candidate_versions_and_counts_reach_verifier(monkeypatch, tmp_path):
    path, config = make_config(tmp_path)
    config['candidates'][0].update(expected_version='0.2.1', expected_tool_count=46)
    config['candidates'][1].update(expected_version='0.2.2', expected_tool_count=48)
    path.write_text(json.dumps(config), encoding='utf-8')
    monkeypatch.setattr(controller, 'port_pid', lambda port: 0)
    monkeypatch.setattr(controller, 'wait_port', lambda *args: 1234)
    monkeypatch.setattr(controller, 'task_command', lambda *args:
        type('Result', (), {'returncode': 0, 'stdout': '', 'stderr': ''})())
    seen = []
    def verify(candidate, pid, expected_tool_count=None):
        seen.append((candidate['expected_version'], expected_tool_count))
        return len(seen) == 3, 'fixture'
    monkeypatch.setattr(controller, 'verify_service', create_autospec(controller.verify_service, side_effect=verify))
    assert controller.run(path) == controller.EXIT_OK
    assert seen == [('0.2.1', 46), ('0.2.2', 48), ('0.2.0', 47)]


@pytest.mark.parametrize('explicit', [None, 13])
def test_real_verifier_forwards_optional_count_without_changing_version(monkeypatch, tmp_path, explicit):
    home = tmp_path / 'home'
    (home / 'state').mkdir(parents=True)
    (home / 'state/service.json').write_text(json.dumps({'pid': 1234, 'version': '0.2.2'}))
    config = {'home': str(home), 'expected_version': '0.2.2', 'expected_tool_count': 47}
    async def probe(candidate, expected_tool_count=None):
        assert candidate == config and expected_tool_count == explicit
        return {'version': candidate['expected_version'],
                'tool_count': candidate['expected_tool_count'] if expected_tool_count is None else expected_tool_count}
    monkeypatch.setattr(controller, 'authenticated_mcp_probe', probe)
    healthy, proof = controller.verify_service(config, 1234, explicit)
    assert healthy and proof == {'version': '0.2.2', 'tool_count': 47 if explicit is None else explicit}


def test_healthy_old_fallback_is_not_restarted(monkeypatch, tmp_path):
    path, config = make_config(tmp_path)
    config['expected_version'] = '0.2.1'
    config['candidates'][-1]['expected_version'] = '0.2.0'
    path.write_text(json.dumps(config), encoding='utf-8')
    monkeypatch.setattr(controller, 'port_pid', lambda port: 222)
    monkeypatch.setattr(controller, 'verify_service', lambda c, p:
        (c['expected_version'] == '0.2.0', 'fixture'))
    touched = []
    monkeypatch.setattr(controller, 'task_command', lambda *args: touched.append(args))
    assert controller.run(path) == controller.EXIT_ALREADY_HEALTHY
    assert not touched


def test_expired_restart_request_is_rejected(monkeypatch, tmp_path):
    path, config = make_config(tmp_path)
    Path(config["request"]).mkdir()
    (Path(config["request"])/'expired.json').write_text(
        json.dumps(
            {
                "operation": "restart",
                "created_at": controller.iso(controller.utc_now() - timedelta(minutes=6)),
            }
        ),
        "utf-8",
    )
    assert controller.run(path) == controller.EXIT_CONFIG
    report = json.loads(Path(config["report"]).read_text("utf-8"))
    assert report["error"] == "invalid_or_expired_request"
