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

ROOT = Path(__file__).resolve().parents[1]
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
    monkeypatch.setattr(controller, "task_command", task_command)
    monkeypatch.setattr(controller, "verify_service", lambda c, p: next(outcomes))
    assert controller.run(path) == controller.EXIT_OK
    report = json.loads(Path(config["report"]).read_text("utf-8"))
    assert [item["name"] for item in report["attempts"]] == [
        "formal_task",
        "current_source_direct",
        "lkg_direct",
    ]
    assert report["selected"] == "lkg_direct"
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
