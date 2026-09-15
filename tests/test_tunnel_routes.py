"""Validate generated tunnel routing with both Python and actual cloudflared."""

import json
import re
import subprocess
from pathlib import Path

import pytest
from codex_control_mcp.tunnel import ingress_pattern


def test_ingress_pattern_uses_literal_metadata_dot():
    pattern = ingress_pattern(True)
    allowed = [
        "/mcp",
        "/authorize",
        "/token",
        "/register",
        "/revoke",
        "/consent",
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
    ]
    for path in allowed:
        assert re.fullmatch(pattern, path), path
    for path in [
        "/admin",
        "/",
        "/xwell-known/oauth-authorization-server",
        "/mcp/extra",
        "/authorize/extra",
    ]:
        assert not re.fullmatch(pattern, path), path
    assert re.fullmatch(ingress_pattern(False), "/mcp")
    assert not re.fullmatch(
        ingress_pattern(False), "/.well-known/oauth-authorization-server"
    )


def test_real_cloudflared_matches_oauth_discovery_and_blocks_other_routes(tmp_path):
    metadata = Path.home() / ".codex-control-mcp/state/tunnel.json"
    if not metadata.is_file():
        pytest.skip("The actual owner cloudflared installation is not configured")
    record = json.loads(metadata.read_text("utf-8"))
    binary = Path(record["binary"])
    if not binary.is_file():
        pytest.skip("The actual cloudflared executable is not installed")
    config = tmp_path / "routing-fixture.yml"
    config.write_text(
        "\n".join(
            [
                "ingress:",
                "  - hostname: isolated-owner.test",
                "    path: " + ingress_pattern(True),
                "    service: http://127.0.0.1:8774",
                "  - service: http_status:404",
                "",
            ]
        ),
        "utf-8",
    )
    for path, expected in [
        ("/mcp", 0),
        ("/.well-known/oauth-authorization-server", 0),
        ("/.well-known/oauth-protected-resource/mcp", 0),
        ("/token", 0),
        ("/admin", 1),
    ]:
        result = subprocess.run(
            [
                str(binary),
                "tunnel",
                "--config",
                str(config),
                "ingress",
                "rule",
                "https://isolated-owner.test" + path,
            ],
            capture_output=True,
            timeout=15,
        )
        output = (result.stdout + result.stderr).decode("utf-8", "replace")
        assert result.returncode == 0, output[-1500:]
        assert re.search(r"Matched rule #" + str(expected) + r"\b", output), output[
            -1500:
        ]
