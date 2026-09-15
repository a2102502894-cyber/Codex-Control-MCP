"""Read-only Tabbit GUI acceptance through MCP, with no elicitation callback."""
from __future__ import annotations
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from codex_control_mcp import __version__
from codex_control_mcp.auth import owner_token
from codex_control_mcp.config import Config, DEFAULT_CONFIG, build_environment

ROOT = Path(__file__).resolve().parents[1]
ap = argparse.ArgumentParser(description=__doc__)
ap.add_argument("--out", type=Path, required=True)
ap.add_argument("--exe", type=Path)
ap.add_argument("--existing-home", type=Path)
ap.add_argument("--public-url")
options = ap.parse_args()
out = options.out.resolve()
out.mkdir(parents=True, exist_ok=False)
home = options.existing_home or out / "home"
command = [str(options.exe.resolve())] if options.exe else [sys.executable, "-B", "-m", "codex_control_mcp"]
owned = None
log = None
report = {"version": __version__, "client_elicitation_callback": False,
          "only_read_only_gui_calls": True, "business_actions": 0,
          "started_at": time.time(), "existing_service": bool(options.existing_home)}
if options.exe:
    report["executable_sha256"] = hashlib.sha256(options.exe.read_bytes()).hexdigest()


async def inspect():
    token = owner_token(cfg)
    url = options.public_url or f"http://127.0.0.1:{cfg.http['port']}/mcp"
    extra = {}
    if options.public_url:
        sys.path.insert(0, str(ROOT / "scripts"))
        from public_transport import SafeConnectTransport
        env, _ = build_environment(cfg)
        extra["transport"] = SafeConnectTransport(proxy=env.get("HTTPS_PROXY"))
    async with httpx.AsyncClient(trust_env=False, timeout=230,
        headers={"Authorization": "Bearer " + token}, **extra) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, session_id):
            async with ClientSession(read, write) as client:
                init = await client.initialize()
                assert init.serverInfo.name == "Codex-Control-MCP"
                assert init.serverInfo.version == __version__
                report["transport"] = "public_https" if options.public_url else "local_http"
                listed = await client.call_tool("computer_snapshot", {})
                data = listed.structuredContent
                if not data or not data.get("ok"):
                    raise RuntimeError("Window enumeration failed: " + json.dumps((data or {}).get("error")))
                candidates = [w for w in data["result"].get("windows", [])
                              if "tabbit" in json.dumps(w.get("app", "")).casefold()]
                if not candidates:
                    raise RuntimeError("No currently returned Tabbit window; no app was launched")
                window = candidates[0]
                report["target"] = {"window_id": window["id"], "app": window.get("app")}
                snapshots = []
                for _ in range(2):
                    result = await client.call_tool("computer_snapshot", {"window_id": window["id"]})
                    data = result.structuredContent
                    if not data or not data.get("ok"):
                        raise RuntimeError("Tabbit snapshot failed: " + json.dumps((data or {}).get("error")))
                    payload = data["result"]
                    images = [x for x in result.content if x.type == "image"]
                    snapshots.append({"ok": True, "image_count": len(images),
                        "authorization": payload.get("application_authorization"),
                        "application_access_policy": payload["runtime"].get("application_access_policy"),
                        "backend": payload.get("backend"), "operation_id": data.get("operation_id")})
                    assert images and snapshots[-1]["application_access_policy"] == "owner_preapproved"
                report["snapshots"] = snapshots
                report["http_session_id_present"] = bool(session_id())
                report["client_elicitation_required"] = cfg.requires_client_elicitation
                if not cfg.requires_client_elicitation:
                    assert not report["http_session_id_present"]


try:
    if not options.existing_home:
        home.mkdir()
        with socket.socket() as port_socket:
            port_socket.bind(("127.0.0.1", 0))
            port = port_socket.getsockname()[1]
        text = 'computer_use_enabled = true\nauto_approve_application_access = true\n' + DEFAULT_CONFIG.replace("8774", str(port))
        (home / "config.toml").write_text(text, encoding="utf-8")
        cfg = Config.load(home)
        cfg.initialize_storage()
        owner_token(cfg, create=True)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src")
        env["PYTHONIOENCODING"] = "utf-8"
        log = (out / "service.log").open("wb")
        owned = subprocess.Popen(command + ["--home", str(home), "serve", "--transport", "streamable-http"],
            cwd=ROOT, env=env, stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.monotonic() + 30
        while not (home / "state/service.json").exists():
            if owned.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("Owned acceptance server failed to start")
            time.sleep(0.2)
    else:
        cfg = Config.load(home)
    assert cfg.auto_approve_application_access is True
    asyncio.run(inspect())
    report["pass"] = True
except Exception as exc:
    report["pass"] = False
    report["error"] = str(exc)
finally:
    if owned:
        stopped = subprocess.run(command + ["--home", str(home), "stop", "--target", "core"],
            cwd=ROOT, env=env, capture_output=True, timeout=45, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            owned.wait(15)
        except subprocess.TimeoutExpired:
            report["pass"] = False
            report["owned_service_stop_failed"] = True
        report["owned_service_stopped"] = owned.poll() is not None
    if log:
        log.close()
    report["finished_at"] = time.time()
    (out / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True), flush=True)
raise SystemExit(0 if report["pass"] else 1)
