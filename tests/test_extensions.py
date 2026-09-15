"""Contract/unit tests only: these do not substitute for real GUI acceptance."""

import json
import shutil
import subprocess
import threading
import pytest
from codex_control_mcp.browser import OfficialBrowser, BROWSER_JS
from codex_control_mcp.computer import OfficialComputer
from codex_control_mcp.config import Config
from codex_control_mcp.consent import native_application_consent
from codex_control_mcp.errors import BridgeError
from codex_control_mcp.tools import validate_tool
from codex_control_mcp.cli import parser


def test_gui_candidates_disabled_by_default(tmp_path):
    cfg = Config.load(tmp_path)
    assert cfg.browser_use_enabled is False
    assert cfg.computer_use_enabled is False
    assert cfg.local_application_consent is False


@pytest.mark.parametrize(
    "consent_request",
    [
        {},
        {"mode": "url", "url": "https://example.com"},
        {
            "_meta": {"connector_id": "unknown", "tool_params": {"app": "x.exe"}},
            "requestedSchema": {},
        },
        {
            "_meta": {"connector_id": "computer-use", "tool_params": {"app": "x.exe"}},
            "requestedSchema": {"properties": {"password": {"type": "string"}}},
        },
        {
            "_meta": {"connector_id": "computer-use", "tool_params": {"app": ""}},
            "requestedSchema": {},
        },
    ],
)
def test_unknown_consent_requests_are_cancelled(consent_request):
    assert native_application_consent(consent_request) == {"action": "cancel"}


def test_gui_list_is_not_full_action_verification():
    obj = OfficialComputer.__new__(OfficialComputer)
    obj.action_lock = threading.RLock()
    obj.verified_operations = set()
    obj.verified = False
    obj._call = lambda code, forwarder: {"windows": []}
    out = obj.call("computer_snapshot", {})
    assert not obj.verified and not out["verified_operations"]
    obj._call = lambda code, forwarder: {"screenshots": [{"id": "unit-test-only"}]}
    out = obj.call("computer_snapshot", {"window_id": 1})
    assert out["verified_operations"] == ["snapshot"] and not obj.verified


def test_browser_coordinates_are_exclusive():
    obj = OfficialBrowser.__new__(OfficialBrowser)
    with pytest.raises(BridgeError):
        obj.call("browser_click", {"element_index": 2, "x": 3, "y": 4})
    with pytest.raises(BridgeError):
        obj.call("browser_click", {})


def test_browser_schema_matches_official_element_api():
    validate_tool(
        "browser_fill",
        {
            "page_id": "unit-page",
            "snapshot_id": "unit-snapshot",
            "element_index": 3,
            "text": "中文",
        },
    )
    with pytest.raises(BridgeError):
        validate_tool(
            "browser_fill",
            {
                "page_id": "unit-page",
                "snapshot_id": "unit-snapshot",
                "selector": "#not-supported",
                "text": "x",
            },
        )


def test_manual_tunnel_cli_exists():
    args = parser().parse_args(["tunnel"])
    assert args.command == "tunnel"


def test_browser_js_contract_ownership_and_stale_replay(tmp_path):
    # Deterministic fake API exercises adapter logic, not the real browser or user data.
    node = shutil.which("node.exe") or shutil.which("node")
    assert node, "Node is required for the adapter contract test"
    reqs = [
        {
            "tool": "browser_start",
            "args": {"url": "http://127.0.0.1:9999"},
            "page_id": "owned-page",
            "snapshot_id": "snap-1",
        },
        {
            "tool": "browser_click",
            "args": {
                "page_id": "foreign-page",
                "snapshot_id": "snap-1",
                "element_index": 1,
            },
            "snapshot_id": "no",
        },
        {
            "tool": "browser_click",
            "args": {
                "page_id": "owned-page",
                "snapshot_id": "snap-1",
                "element_index": 1,
            },
            "snapshot_id": "snap-2",
        },
        {
            "tool": "browser_click",
            "args": {
                "page_id": "owned-page",
                "snapshot_id": "snap-1",
                "element_index": 1,
            },
            "snapshot_id": "snap-3",
        },
        {
            "tool": "browser_fill",
            "args": {
                "page_id": "owned-page",
                "snapshot_id": "snap-2",
                "element_index": 1,
                "text": '中文 ";throw new Error("INJECTED");//',
            },
            "snapshot_id": "snap-4",
        },
        {
            "tool": "browser_close",
            "args": {"page_id": "owned-page"},
            "snapshot_id": "close",
        },
        {
            "tool": "browser_snapshot",
            "args": {"page_id": "owned-page"},
            "snapshot_id": "after-close",
        },
    ]
    prelude = r"""
const outputs=[];let clicked=0;let filled=null;let closed=0;
globalThis.nodeRepl={write:x=>outputs.push(JSON.parse(x.slice('CCM_RESULT='.length)))};
const tab={getAXState:async()=>"[1] Text input",getScreenshot:async()=>{},click:async()=>{clicked++},
setValue:async(i,text)=>{filled=text},pressKey:async()=>{},scroll:async()=>{},goto:async()=>{},close:async()=>{closed++}};
globalThis.cua={listBrowsers:async()=>[{id:'fake-official-contract'}],getBrowser:async()=>({browserId:'fake-official-contract'}),createBrowserTab:async()=>tab};
"""
    program = (
        prelude
        + "\n(async()=>{\n"
        + "\n".join(
            BROWSER_JS.replace("REQUEST", json.dumps(r, ensure_ascii=True), 1)
            for r in reqs
        )
        + r"""
process.stdout.write(JSON.stringify({outputs,clicked,filled,closed}));
})().catch(e=>{process.stderr.write(String(e));process.exitCode=1});
"""
    )
    path = tmp_path / "browser-contract.js"
    path.write_text(program, "utf-8")
    p = subprocess.run([node, str(path)], capture_output=True, timeout=10)
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
    out = json.loads(p.stdout)
    assert out["outputs"][0]["page_id"] == "owned-page"
    assert out["outputs"][1]["error_code"] == "page_not_owned"
    assert out["outputs"][3]["error_code"] == "stale_snapshot"
    assert out["outputs"][6]["error_code"] == "page_not_owned"
    assert out["clicked"] == 1 and out["closed"] == 1
    assert out["filled"] == reqs[4]["args"]["text"]


def test_windows_stop_signal_roundtrip():
    import uuid
    from codex_control_mcp.lifecycle import StopEvent, request_stop

    identity = uuid.uuid4().hex
    event = StopEvent(identity)
    try:
        assert not event.requested()
        request_stop(identity)
        assert event.requested()
    finally:
        event.close()
    with pytest.raises(BridgeError):
        request_stop(identity)


def test_windows_stop_event_rejects_identifier_reuse():
    import uuid
    from codex_control_mcp.lifecycle import StopEvent

    identity = uuid.uuid4().hex
    event = StopEvent(identity)
    try:
        with pytest.raises(BridgeError):
            StopEvent(identity)
    finally:
        event.close()


def test_windows_stop_event_rejects_untrusted_name():
    from codex_control_mcp.lifecycle import event_name

    with pytest.raises(BridgeError):
        event_name("Global\\unowned-service")


def test_stop_cli_limits_targets():
    assert parser().parse_args(["stop", "--target", "https"]).target == "https"
    with pytest.raises(SystemExit):
        parser().parse_args(["stop", "--target", "all-windows-processes"])
