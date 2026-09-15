"""Owner preapproval must work with a client that cannot show elicitation UI."""
import asyncio
from types import SimpleNamespace

import anyio
import pytest
from mcp import ClientSession, types
from mcp.shared.memory import create_client_server_memory_streams

from codex_control_mcp.common import ELICITATION_FORWARDER
from codex_control_mcp.config import Config
from codex_control_mcp.computer import OfficialComputer
from codex_control_mcp.consent import owner_preapproved_application_consent
from codex_control_mcp.errors import BridgeError
from codex_control_mcp.server import make_server


def request(app="tabbit.exe"):
    return {"mode": "form", "message": "Allow Codex to use this application?",
            "requestedSchema": {"type": "object", "properties": {}},
            "_meta": {"connector_id": "computer-use", "tool_params": {"app": app}}}


def roundtrip(req, preapproved, callback=None):
    events = []
    def execute(name, args):
        decision = ELICITATION_FORWARDER.get()(req)
        return {"ok": True, "result": decision}
    bridge = SimpleNamespace(
        cfg=SimpleNamespace(oauth={}, local_application_consent=False,
                            auto_approve_application_access=preapproved,
                            browser_use_enabled=False, computer_use_enabled=True),
        audit=SimpleNamespace(emit=lambda event, **fields: events.append((event, fields))),
        execute=execute)
    server = make_server(bridge)
    async def run():
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            async with anyio.create_task_group() as group:
                group.start_soon(server.run, *server_streams, server.create_initialization_options())
                async with ClientSession(*client_streams, elicitation_callback=callback) as client:
                    await client.initialize()
                    result = await client.call_tool("computer_snapshot", {})
                group.cancel_scope.cancel()
                return result
    return asyncio.run(run()), events


@pytest.mark.parametrize("app", ["tabbit.exe", "NewlyInstalledFixture.exe", "Example.Package!App"])
def test_standing_owner_choice_needs_no_client_callback(app):
    result, events = roundtrip(request(app), True)
    decision = result.structuredContent["result"]
    assert decision["action"] == "accept" and decision["content"] == {}
    assert decision["_meta"]["codex_control_mcp"]["decision_source"] == "explicit_owner_preapproval"
    assert events == [("application_access_decision", {
        "action": "accept", "decision_source": "explicit_owner_preapproval", "app": app})]


def test_disabling_preapproval_restores_client_decision():
    received = []
    async def decline(context, params):
        received.append(params)
        return types.ElicitResult(action="decline")
    result, events = roundtrip(request(), False, decline)
    assert result.structuredContent["result"]["action"] == "decline"
    assert len(received) == 1 and events == []


def test_data_form_is_not_accepted_by_application_preapproval():
    req = request()
    req["requestedSchema"]["properties"] = {"password": {"type": "string"}}
    received = []
    async def decline(context, params):
        received.append(params)
        return types.ElicitResult(action="decline")
    result, events = roundtrip(req, True, decline)
    assert result.structuredContent["result"]["action"] == "decline"
    assert len(received) == 1 and events == []


@pytest.mark.parametrize("req", [
    {}, {"mode": "url", "url": "https://example.com"},
    {**request(), "_meta": {"connector_id": "other", "tool_params": {"app": "x.exe"}}},
    {**request(), "_meta": "invalid"},
    {**request(), "requestedSchema": {"anyOf": [{"required": ["secret"]}]}},
    {**request(), "requestedSchema": {"type": "object", "required": ["secret"]}},
    request(" "), request("x\0.exe"), request("x" * 513),
])
def test_unrecognized_requests_are_not_preapproved(req):
    assert owner_preapproved_application_consent(req) is None


def test_config_preapproval_is_explicit_and_type_checked(tmp_path):
    assert Config.load(tmp_path).auto_approve_application_access is False
    path = tmp_path / "config.toml"
    path.write_text('auto_approve_application_access = true\n', encoding="utf-8")
    assert Config.load(tmp_path).auto_approve_application_access is True
    path.write_text('auto_approve_application_access = "true"\n', encoding="utf-8")
    with pytest.raises(BridgeError):
        Config.load(tmp_path)


def test_direct_local_adapter_uses_saved_preapproval_without_forwarder(tmp_path):
    computer = OfficialComputer.__new__(OfficialComputer)
    computer.cfg = Config(home=tmp_path, cwd=str(tmp_path), auto_approve_application_access=True)
    computer.current_forwarder = None
    result = computer._application_decision(request())
    assert result["action"] == "accept"
    assert result["_meta"]["codex_control_mcp"]["decision_source"] == "explicit_owner_preapproval"


def test_direct_local_adapter_reports_missing_interactive_handler(tmp_path):
    computer = OfficialComputer.__new__(OfficialComputer)
    computer.cfg = Config(home=tmp_path, cwd=str(tmp_path))
    computer.current_forwarder = None
    result = computer._application_decision(request())
    assert result["action"] == "cancel"
    assert result["_meta"]["codex_control_mcp"]["decision_source"] == "no_interactive_approval_handler"


@pytest.mark.parametrize("computer,browser,backend,auto,local,expected", [
    (True, False, "official", True, False, False),
    (True, False, "official", False, True, False),
    (True, False, "official", False, False, True),
    (False, True, "tabbit", False, False, False),
    (False, True, "official", False, False, True),
    (False, False, "official", False, False, False),
])
def test_http_only_requires_interactive_session_when_needed(tmp_path, computer, browser, backend, auto, local, expected):
    cfg = Config(home=tmp_path, cwd=str(tmp_path), computer_use_enabled=computer,
                 browser_use_enabled=browser, browser={"backend": backend},
                 auto_approve_application_access=auto, local_application_consent=local)
    assert cfg.requires_client_elicitation is expected
