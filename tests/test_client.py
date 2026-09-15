import asyncio, json, types
import pytest
from codex_control_mcp.config import Config
from codex_control_mcp.errors import BridgeError
from codex_control_mcp import client


class Context:
    def __init__(self, value=None):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *args):
        return False


def setup_client(tmp_path, monkeypatch, fail_at):
    cfg = Config(home=tmp_path, cwd=str(tmp_path))
    (tmp_path / "state").mkdir()
    (tmp_path / "state/service.json").write_text(
        json.dumps({"host": "127.0.0.1", "port": 8767}), encoding="utf-8"
    )
    monkeypatch.setattr(
        client, "owner_token", lambda cfg: "synthetic-test-credential-not-a-real-token"
    )
    monkeypatch.setattr(client.httpx, "AsyncClient", lambda **kwargs: Context(object()))
    monkeypatch.setattr(
        client,
        "streamable_http_client",
        lambda *args, **kwargs: Context((None, None, None)),
    )

    class Session:
        def __init__(self, *args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def initialize(self):
            if fail_at == "initialize":
                raise ConnectionError("Service did not initialize")
            return types.SimpleNamespace(
                serverInfo=types.SimpleNamespace(name="Codex-Control-MCP")
            )

        async def call_tool(self, *args):
            raise ConnectionError("Response lost after dispatch")

    monkeypatch.setattr(client, "ClientSession", Session)
    return cfg


def test_no_fallback_after_dispatched_mutation(tmp_path, monkeypatch):
    cfg = setup_client(tmp_path, monkeypatch, "tool")
    with pytest.raises(BridgeError) as e:
        asyncio.run(
            client.call_running_service(
                cfg, "file_add", {"path": "synthetic.txt", "content": "one"}
            )
        )
    assert e.value.code == "execution_state_unknown"


def test_stale_service_can_fall_back_before_any_tool_dispatch(tmp_path, monkeypatch):
    cfg = setup_client(tmp_path, monkeypatch, "initialize")
    assert asyncio.run(client.call_running_service(cfg, "codex_health", {})) is None
