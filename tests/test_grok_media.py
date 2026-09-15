from codex_control_mcp.tools import TOOL_SPECS


def test_grok_is_not_static_core_surface():
    assert not any(name.startswith("grok_") for name in TOOL_SPECS)
    assert {"mcp_manage", "mcp_tool_search", "mcp_tool_inspect", "mcp_tool_call"}.issubset(TOOL_SPECS)
