"""Cache publication and failed-refresh recovery, with labelled schema fixtures."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from codex_control_mcp import schema
from codex_control_mcp.bridge import Bridge
from codex_control_mcp.config import Config
from codex_control_mcp.errors import BridgeError
from test_unit import schema_fixture


def test_schema_refresh_publishes_new_generation_without_destroying_old(
    tmp_path, monkeypatch
):
    cfg = Config(home=tmp_path / "home", cwd=str(tmp_path))
    cfg.initialize_storage()
    runtime = SimpleNamespace(
        file_fingerprint="f" * 64, codex_path="fixture-not-an-executable"
    )

    def metadata(args, **kwargs):
        if "--help" in args:
            return SimpleNamespace(returncode=0, stdout="--out --experimental")
        folder = Path(args[args.index("--out") + 1])
        schema_fixture(folder)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(schema, "run_metadata", metadata)
    first, before = schema.load_or_export(cfg, runtime)
    second, after = schema.load_or_export(cfg, runtime, True)
    assert first.hash == second.hash
    assert before["schema_path"] != after["schema_path"]
    assert (Path(before["schema_path"]) / "ClientRequest.json").is_file()
    cached, info = schema.load_or_export(cfg, runtime)
    assert cached.hash == second.hash and not info["exported"]

    def fail(args, **kwargs):
        return SimpleNamespace(returncode=1, stdout="")

    monkeypatch.setattr(schema, "run_metadata", fail)
    with pytest.raises(BridgeError):
        schema.load_or_export(cfg, runtime, True)
    state = json.loads((cfg.home / "state/schema.json").read_text("utf-8"))
    assert state["schema_path"] == after["schema_path"]
    assert (Path(state["schema_path"]) / "ClientRequest.json").is_file()


def test_failed_refresh_preserves_live_official_connection(tmp_path, monkeypatch):
    import codex_control_mcp.bridge as module

    bridge = Bridge(Config(home=tmp_path / "home", cwd=str(tmp_path)))
    previous = SimpleNamespace(alive=True, pending={}, closed=False)
    previous.close = lambda: setattr(previous, "closed", True)
    bridge.rpc = previous
    bridge.runtime = SimpleNamespace(file_fingerprint="same", cli_version="fixture")
    monkeypatch.setattr(module.Discovery, "find", lambda self: bridge.runtime)

    def fail(*args, **kwargs):
        raise BridgeError("version_incompatible", "Injected failed schema export")

    monkeypatch.setattr(module, "load_or_export", fail)
    try:
        with pytest.raises(BridgeError):
            bridge.ensure_ready(force=True)
        assert bridge.rpc is previous and not previous.closed
    finally:
        bridge.close()
