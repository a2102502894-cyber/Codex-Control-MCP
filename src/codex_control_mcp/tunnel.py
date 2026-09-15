"""Run this project's own Cloudflare tunnel to its authenticated loopback MCP.
No Codex/AgentDock processes, credentials, or configuration are changed.
"""

from __future__ import annotations
import json
import os
import pathlib
import subprocess
import tempfile
import time
import uuid
from .auth import _crypt
from .common import atomic_json, utc_now
from .errors import BridgeError




def ingress_pattern(oauth_enabled):
    if not oauth_enabled:
        return r"^/mcp$"
    return r"^/(mcp|authorize|token|register|revoke|consent|\.well-known/oauth-authorization-server|\.well-known/oauth-protected-resource(/mcp)?)$"


def run_tunnel(cfg):
    record_path = cfg.home / "state/tunnel.json"
    if not record_path.is_file():
        raise BridgeError(
            "capability_unavailable", "Independent HTTPS tunnel is not configured"
        )
    record = json.loads(record_path.read_text("utf-8"))
    exe = pathlib.Path(record["binary"])
    if not exe.is_file():
        raise BridgeError("runtime_missing", "cloudflared binary is missing")
    port = int(cfg.http.get("port", 8774))
    expected_origin = f"http://127.0.0.1:{port}"
    if record.get("origin") != expected_origin:
        raise BridgeError(
            "invalid_config",
            f"This tunnel must target the authenticated local bridge at {expected_origin}",
        )
    encrypted = cfg.home / "state/tunnel-credentials.dpapi"
    credentials = _crypt(encrypted.read_bytes(), True)
    decoded = json.loads(credentials)
    if decoded.get("TunnelID") != record["tunnel_id"]:
        raise BridgeError("invalid_config", "Tunnel credential identity mismatch")
    if cfg.oauth.get("enabled"):
        from .oauth import valid_issuer

        if valid_issuer("https://" + record["hostname"]) != cfg.oauth["issuer"]:
            raise BridgeError(
                "invalid_config", "OAuth issuer and owned tunnel hostname differ"
            )
    logs = cfg.home / "logs"
    logs.mkdir(exist_ok=True)
    lock_path = cfg.home / "state/tunnel.lock"
    lock = lock_path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            if lock_path.stat().st_size == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as e:
                raise BridgeError(
                    "instance_already_running",
                    "This project already owns a tunnel process",
                ) from e
        with tempfile.TemporaryDirectory(
            prefix="ccm-tunnel-", dir=cfg.home / "state"
        ) as temp:
            root = pathlib.Path(temp)
            # Restrict the transient file to the owner and SYSTEM before writing it.
            if os.name == "nt":
                import getpass

                result = subprocess.run(
                    [
                        "icacls.exe",
                        str(root),
                        "/inheritance:r",
                        "/grant:r",
                        getpass.getuser() + ":(OI)(CI)F",
                        "SYSTEM:(OI)(CI)F",
                    ],
                    capture_output=True,
                )
                if result.returncode:
                    raise BridgeError(
                        "permission_denied",
                        "Could not restrict transient tunnel credentials",
                    )
            cred = root / "credentials.json"
            cred.write_bytes(credentials)
            conf = root / "config.yml"
            conf.write_text(
                "\n".join(
                    [
                        "tunnel: " + record["tunnel_id"],
                        "credentials-file: " + json.dumps(str(cred)),
                        "protocol: http2",
                        "metrics: 127.0.0.1:8769",
                        "loglevel: warn",
                        "ingress:",
                        "  - hostname: " + record["hostname"],
                        "    path: " + ingress_pattern(cfg.oauth.get("enabled", False)),
                        f"    service: {expected_origin}",
                        "    originRequest:",
                        f"      httpHostHeader: 127.0.0.1:{port}",
                        "      connectTimeout: 10s",
                        "  - service: http_status:404",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            with (logs / "https-tunnel.log").open("ab") as log:
                child = subprocess.Popen(
                    [
                        str(exe),
                        "tunnel",
                        "--config",
                        str(conf),
                        "--no-autoupdate",
                        "run",
                        record["tunnel_id"],
                    ],
                    stdout=log,
                    stderr=log,
                )
                job = None
                if os.name == "nt":
                    import win32api

                    try:
                        from .lifecycle import create_owned_process_job

                        job = create_owned_process_job(child._handle)
                    except Exception as exc:
                        child.terminate()
                        child.wait(8)
                        if job is not None:
                            win32api.CloseHandle(job)
                        raise BridgeError(
                            "permission_denied",
                            "Could not manage the owned tunnel process lifecycle",
                            details={"failure_type": type(exc).__name__},
                        ) from exc
                from .lifecycle import StopEvent

                instance_id = uuid.uuid4().hex
                stop_event = None
                try:
                    stop_event = StopEvent(instance_id)
                    atomic_json(
                        cfg.home / "state/tunnel-service.json",
                        {
                            "pid": os.getpid(),
                            "child_pid": child.pid,
                            "tunnel_id": record["tunnel_id"],
                            "hostname": record["hostname"],
                            "started_at": utc_now(),
                            "auth": "owner_bearer_or_oauth"
                            if cfg.oauth.get("enabled")
                            else "bearer_required",
                            "credential_at_rest": "dpapi",
                            "owner": "Codex-Control-MCP",
                            "instance_id": instance_id,
                            "graceful_stop_supported": True,
                        },
                    )
                    requested = False
                    while child.poll() is None:
                        if stop_event.requested():
                            requested = True
                            child.terminate()
                            break
                        time.sleep(0.2)
                    result = child.wait(timeout=10)
                    return 0 if requested else result
                finally:
                    if stop_event is not None:
                        stop_event.close()
                    if child.poll() is None:
                        child.terminate()
                        try:
                            child.wait(8)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait(5)
                    if job is not None:
                        win32api.CloseHandle(job)
                    (cfg.home / "state/tunnel-service.json").unlink(missing_ok=True)
    finally:
        lock.close()


if __name__ == "__main__":
    from .config import Config

    raise SystemExit(run_tunnel(Config.load()))
