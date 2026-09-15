from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_control_mcp.dynamic_mcp import DynamicMCPManager
from codex_control_mcp.errors import BridgeError
from codex_control_mcp.host_runtime import HostManager
from codex_control_mcp.skill_manager import SkillManager
from codex_control_mcp.task_runtime import RecoverableTaskStore
from codex_control_mcp.tools import TOOL_SPECS, validate_tool


def test_new_tool_contracts_and_grok_removed():
    expected = {
        "task_manage", "mcp_manage", "mcp_tool_search", "mcp_tool_inspect", "mcp_tool_call",
        "host_manage", "host_exec", "host_files", "host_route", "skill_package",
    }
    assert expected.issubset(TOOL_SPECS)
    assert not any(name.startswith("grok_") for name in TOOL_SPECS)
    validate_tool("task_manage", {"action": "create", "title": "t", "goal": "g"})
    validate_tool("host_exec", {"host": "local", "command": "echo ok"})
    validate_tool("mcp_tool_call", {"name": "fixture:echo", "arguments": {"text": "x"}})


def test_recoverable_task_persists_and_enforces_closeout(tmp_path):
    path = tmp_path / "tasks.sqlite3"
    store = RecoverableTaskStore(path)
    created = store.manage({
        "action": "create", "title": "release", "goal": "finish safely",
        "completion_conditions": ["tests pass"],
        "steps": [{"id": "build", "title": "Build"}, {"id": "verify", "title": "Verify"}],
    })
    task_id = created["task_id"]
    rev = created["task_summary"]["revision"]
    first = store.manage({"action": "checkpoint", "task_id": task_id, "completed_step_ids": ["build"], "current_step_id": "verify", "summary": "built", "expected_revision": rev})
    with pytest.raises(BridgeError, match="changed since"):
        store.manage({"action": "checkpoint", "task_id": task_id, "step_id": "verify", "step_status": "completed", "expected_revision": rev})
    blocked = store.manage({"action": "block", "task_id": task_id, "summary": "awaiting fixture", "expected_revision": first["task_summary"]["revision"]})
    resumed = store.manage({"action": "resume", "task_id": task_id, "summary": "fixture ready", "expected_revision": blocked["task_summary"]["revision"]})
    done_steps = store.manage({"action": "checkpoint", "task_id": task_id, "step_id": "verify", "step_status": "completed", "summary": "verified", "expected_revision": resumed["task_summary"]["revision"]})
    with pytest.raises(BridgeError, match="final_review"):
        store.manage({"action": "complete", "task_id": task_id, "expected_revision": done_steps["task_summary"]["revision"]})
    review = store.manage({"action": "final_review", "task_id": task_id, "review_status": "pass", "summary": "all checks passed", "verified": ["tests pass"], "expected_revision": done_steps["task_summary"]["revision"]})
    finished = store.manage({"action": "complete", "task_id": task_id, "expected_revision": review["task_summary"]["revision"]})
    assert finished["task_summary"]["status"] == "completed"
    store.close()

    reopened = RecoverableTaskStore(path)
    task = reopened.manage({"action": "get", "task_id": task_id})["task"]
    assert task["status"] == "completed" and task["final_review"]["status"] == "pass"
    assert any(event["type"] == "blocked" for event in task["events"])
    reopened.close()


def _write_fixture_mcp(path: Path):
    path.write_text(r'''import asyncio
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

server = Server("fixture-dynamic-mcp", version="1")

@server.list_tools()
async def list_tools():
    return [types.Tool(name="echo", description="Echo text", inputSchema={"type":"object","properties":{"text":{"type":"string"}},"required":["text"],"additionalProperties":False})]

@server.call_tool(validate_input=False)
async def call_tool(name, arguments):
    if name != "echo":
        raise ValueError("unknown")
    return types.CallToolResult(content=[types.TextContent(type="text", text=arguments["text"])], structuredContent={"echo": arguments["text"]})

async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())

asyncio.run(main())
''', encoding="utf-8")


def test_dynamic_mcp_stdio_refresh_search_inspect_call_and_redaction(tmp_path):
    script = tmp_path / "fixture_mcp.py"
    _write_fixture_mcp(script)
    cfg = SimpleNamespace(home=tmp_path, cwd=str(tmp_path))
    manager = DynamicMCPManager(cfg)
    registered = manager.manage({
        "action": "register", "name": "fixture", "transport": "stdio",
        "command": sys.executable, "args": [str(script)],
        "env": {"FIXTURE_SECRET": "never-return-me"},
    })
    assert registered["server"]["env_keys"] == ["FIXTURE_SECRET"]
    assert "never-return-me" not in str(registered)
    refreshed = manager.manage({"action": "refresh", "name": "fixture"})
    assert refreshed["server"]["tool_count"] == 1
    found = manager.search({"query": "echo"})
    assert found["tools"][0]["qualified_name"] == "fixture:echo"
    inspected = manager.inspect({"name": "fixture:echo"})
    assert inspected["tool"]["inputSchema"]["required"] == ["text"]
    with pytest.raises(BridgeError, match="schema validation"):
        manager.call({"name": "fixture:echo", "arguments": {}})
    called = manager.call({"name": "fixture:echo", "arguments": {"text": "hello"}})
    assert called["result"]["structuredContent"]["echo"] == "hello"


def _skill_dir(root: Path, version: str, body: str):
    root.mkdir()
    (root / "SKILL.md").write_text(f"---\nname: test-skill\ndescription: test skill\nversion: {version}\n---\n\n{body}\n", encoding="utf-8")
    return root


def test_skill_install_activate_and_rollback(tmp_path):
    cfg = SimpleNamespace(home=tmp_path, git_path=None)
    manager = SkillManager(cfg)
    v1 = _skill_dir(tmp_path / "src-v1", "1.0.0", "one")
    v2 = _skill_dir(tmp_path / "src-v2", "2.0.0", "two")
    valid = manager.package({"action": "validate", "source": str(v1)})
    assert valid["valid"] and valid["skill"] == "test-skill"
    one = manager.package({"action": "install", "source": str(v1), "channel": "stable"})
    two = manager.package({"action": "install", "source": str(v2), "channel": "development"})
    assert one["activated"] and one["synced"] and two["previous_active"] == "1.0.0"
    runtime_skill = tmp_path / "runtime-home" / "skills" / "test-skill" / "SKILL.md"
    assert "two" in runtime_skill.read_text(encoding="utf-8")
    inspected = manager.package({"action": "inspect", "skill": "test-skill"})
    assert inspected["version"] == "2.0.0" and "two" in inspected["body"]
    rolled = manager.package({"action": "rollback", "skill": "test-skill"})
    assert rolled["to_version"] == "1.0.0" and "one" in runtime_skill.read_text(encoding="utf-8")
    listed = manager.package({"action": "list"})
    assert listed["skills"][0]["version_count"] == 2
    removed = manager.package({"action": "uninstall", "skill": "test-skill"})
    assert removed["active_version"] == "" and not runtime_skill.parent.exists()


class FakeBridge:
    def __init__(self, root):
        self.cfg = SimpleNamespace(home=root)
        self.calls = []
    def _do(self, tool, args):
        self.calls.append((tool, args))
        return {"tool": tool, "args": args, "exit_code": 0, "stdout": "CCM_HOST_OK\n"}


class FakeDynamic:
    def refresh(self, name):
        return {"server": {"name": name, "status": "ready"}}
    def call(self, args):
        return {"forwarded": args}


def test_host_registry_local_and_remote_mcp_route(tmp_path):
    bridge = FakeBridge(tmp_path)
    manager = HostManager(bridge, FakeDynamic())
    items = manager.manage({"action": "list"})
    assert items["hosts"][0]["name"] == "local"
    manager.manage({"action": "register", "name": "render1", "transport": "mcp", "mcp_server": "node-render1", "platform": "windows"})
    route = manager.route({"host": "render1", "operation": "exec"})
    assert route["route"] == "mcp" and route["supported"]
    called = manager.exec({"host": "render1", "command": "hostname"})
    assert called["result"]["forwarded"]["name"] == "node-render1:exec_command"
    local = manager.exec({"host": "local", "command": "echo x"})
    assert local["result"]["tool"] == "exec_command"
