"""Maintenance request regression tests; never restart a real service."""
from pathlib import Path
import json
from types import SimpleNamespace

import pytest
from codex_control_mcp.maintenance import MaintenanceError, submit_request, validate_home, main


@pytest.fixture
def home(tmp_path):
    root = tmp_path / "home"
    state = root / "state"
    state.mkdir(parents=True)
    (state / "core-controller-config.json").write_text(json.dumps({
        "home": str(root), "request": str(state / "core-controller-requests")
    }), encoding="utf-8")
    return root


def task(run=None, state=3, enabled=True):
    return SimpleNamespace(Enabled=enabled, State=state, Run=run or (lambda arg: None))


def test_default_check_has_no_side_effects(home):
    calls = []
    out = submit_request(home, task(calls.append), check_only=True)
    assert out["ok"] and not out["request_created"]
    assert not calls and not (home / "state/core-controller-requests").exists()
    assert not out["powershell_invoked"] and not out["execution_policy_changed"]


def test_one_atomic_request_and_one_scheduler_run(home):
    calls = []
    def run(arg):
        files = list((home / "state/core-controller-requests").glob("*.json"))
        assert len(files) == 1 and not list(files[0].parent.glob("*.tmp"))
        calls.append(json.loads(files[0].read_text("utf-8")))
    out = submit_request(home, task(run))
    assert len(calls) == 1 and calls[0]["operation"] == "restart"
    assert calls[0]["request_id"] == out["request_id"]
    assert out["accepted"] and out["restart_completed"] is False


@pytest.mark.parametrize("enabled,state,code", [(False, 3, "controller_disabled"), (True, 4, "controller_busy"), (True, 2, "controller_busy")])
def test_disabled_or_busy_task_never_queues(home, enabled, state, code):
    with pytest.raises(MaintenanceError) as caught:
        submit_request(home, task(state=state, enabled=enabled))
    assert caught.value.code == code
    assert not (home / "state/core-controller-requests").exists()


def test_failed_start_removes_only_its_unclaimed_request(home):
    queue = home / "state/core-controller-requests"
    queue.mkdir()
    # Non-request data is preserved. Pending JSON requests are now tested
    # separately and must prevent any additional restart submission.
    other = queue / "someone-else.keep"
    other.write_text("{}")
    def fail(arg):
        raise OSError("fixture")
    with pytest.raises(MaintenanceError) as caught:
        submit_request(home, task(fail))
    assert caught.value.code == "controller_start_failed"
    assert list(queue.iterdir()) == [other]


def test_claimed_request_is_unknown_not_retried(home):
    calls = []
    def claimed(arg):
        calls.append(arg)
        next((home / "state/core-controller-requests").glob("*.json")).unlink()
        raise OSError("response unavailable after claim")
    with pytest.raises(MaintenanceError) as caught:
        submit_request(home, task(claimed))
    assert caught.value.code == "restart_state_unknown" and len(calls) == 1


def test_different_controller_home_rejected(home, tmp_path):
    path = home / "state/core-controller-config.json"
    obj = json.loads(path.read_text())
    obj["home"] = str(tmp_path)
    path.write_text(json.dumps(obj))
    with pytest.raises(MaintenanceError) as caught:
        validate_home(home)
    assert caught.value.code == "controller_home_mismatch"


def test_missing_installation_does_not_create_it(tmp_path):
    with pytest.raises(MaintenanceError):
        validate_home(tmp_path / "missing")
    assert not (tmp_path / "missing").exists()


def test_cli_default_only_checks(home, monkeypatch, capsys):
    import codex_control_mcp.maintenance as module
    calls = []
    monkeypatch.setattr(module, "controller_task", lambda: task(calls.append))
    assert main(["--home", str(home)]) == 0
    assert json.loads(capsys.readouterr().out)["check_only"] and not calls


def test_cli_error_contains_no_native_exception_data(home, monkeypatch, capsys):
    import codex_control_mcp.maintenance as module
    def fail():
        raise OSError("PRIVATE_FIXTURE_VALUE")
    monkeypatch.setattr(module, "controller_task", fail)
    assert main(["--home", str(home)]) == 1
    text = capsys.readouterr().out
    assert "PRIVATE_FIXTURE_VALUE" not in text
    assert json.loads(text)["error"]["code"] == "maintenance_failed"
