"""Keep all test-owned child processes in the background on Windows."""

import json
import os
from pathlib import Path
import subprocess

import pytest


@pytest.fixture(scope="session", autouse=True)
def background_test_processes():
    original = subprocess.Popen
    children = []
    source = str(Path(__file__).resolve().parents[1] / "src")
    old_path = os.environ.get("PYTHONPATH")
    os.environ["PYTHONPATH"] = os.pathsep.join(filter(None, [source, old_path]))

    def start(*args, **kwargs):
        if os.name == "nt":
            kwargs["creationflags"] = (
                kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
            )
        proc = original(*args, **kwargs)
        children.append(
            {
                "pid": proc.pid,
                "no_console_window": bool(
                    kwargs.get("creationflags", 0)
                    & getattr(subprocess, "CREATE_NO_WINDOW", 0)
                ),
            }
        )
        return proc

    subprocess.Popen = start
    try:
        yield
    finally:
        subprocess.Popen = original
        if old_path is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = old_path
        evidence = Path(__file__).resolve().parents[1] / "evidence"
        evidence.mkdir(exist_ok=True)
        profile = os.environ.get('CCM_TEST_PROFILE')
        assert profile in (None, 'standard', 'admin')
        name = 'children-' + profile + '.json' if profile else 'test-child-processes.json'
        (evidence / name).write_text(
            json.dumps(children, indent=2), encoding="utf-8"
        )


# These cases use real Windows kernel/DPAPI primitives, even when other parts
# of their fixtures are mocked. Keep them enabled on Windows; do not replace
# encryption, stop events, or Job Objects with permissive test implementations.
WINDOWS_CASES = {
    "test_extensions.py": {"test_windows_stop_signal_roundtrip", "test_windows_stop_event_rejects_identifier_reuse"},
    "test_hardening.py": {"test_actual_dead_process_record_is_not_treated_as_running"},
    "test_lifecycle_integration.py": {"test_actual_http_service_graceful_stop", "test_owned_job_really_terminates_child_when_closed"},
    "test_http_boundaries.py": {"test_registration_rejects_invalid_json_before_storing_client", "test_sdk_basic_auth_works_without_duplicate_form_client_id"},
    "test_oauth.py": {"test_browser_form_retains_origin_without_cross_origin_referrer", "test_sdk_flow_single_use_dpapi_persistence_and_issuer", "test_wrong_pkce_resource_and_refresh_replay", "test_consent_requires_owner_secret_cookie_origin_and_csrf", "test_authorization_error_contains_issuer_and_no_consent", "test_revocation_removes_whole_grant_family"},
    "test_oauth_edges.py": {"test_non_ascii_csrf_is_rejected_not_internal_error", "test_duplicate_resource_field_is_rejected_before_grant_consumption", "test_duplicate_json_registration_fields_are_rejected", "test_opaque_oauth_records_have_single_consumer_across_connections", "test_expired_access_record_is_refused"},
}
WINDOWS_MODULES = {"test_oauth_atomic.py", "test_oauth_transport.py"}


def pytest_collection_modifyitems(items):
    for item in items:
        filename = item.path.name
        if (filename, item.originalname) in {("test_lifecycle_integration.py", "test_actual_http_service_graceful_stop"), ("test_oauth_transport.py", "test_real_oauth_http_official_execution_and_shutdown")}:
            item.add_marker(pytest.mark.integration)
        if filename in WINDOWS_MODULES or item.originalname in WINDOWS_CASES.get(filename, set()):
            item.add_marker(pytest.mark.windows)
            if os.name != "nt":
                item.add_marker(pytest.mark.skip(reason="Requires real Windows DPAPI/kernel lifecycle primitives"))
