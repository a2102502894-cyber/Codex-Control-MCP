from __future__ import annotations
import argparse, asyncio, json, os, sys
from . import __version__
from .common import is_admin
from .config import Config, DEFAULT_CONFIG
from .errors import BridgeError


def parser():
    p = argparse.ArgumentParser(
        prog="codex-control-mcp",
        description="Official Codex execution bridge; trusted owner full access, no agent turns.",
    )
    p.add_argument("--home")
    sub = p.add_subparsers(dest="command", required=True)
    for n in ("version", "install", "status", "capabilities", "tunnel"):
        sub.add_parser(n)
    connect = sub.add_parser(
        "connect",
        help="Open five-minute owner enrollment and copy a one-time pairing code locally",
    )
    connect.add_argument("--copy", action="store_true")
    sub.add_parser(
        "connections",
        help="Read local OAuth registration and grant counts without secrets",
    )
    disconnect = sub.add_parser(
        "disconnect",
        help="Revoke all OAuth clients and grants, leaving owner Bearer access unchanged",
    )
    disconnect.add_argument("--all", action="store_true")
    stop = sub.add_parser(
        "stop",
        help="Stop only this owner's core or HTTPS service and its owned execution sessions",
    )
    stop.add_argument("--target", choices=["core", "https"], default="core")
    d = sub.add_parser("doctor")
    d.add_argument("--active", action="store_true")
    d.add_argument("--refresh", action="store_true")
    s = sub.add_parser("serve")
    s.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    c = sub.add_parser("call")
    c.add_argument("tool")
    c.add_argument("--args-json", default="{}")
    t = sub.add_parser("token")
    t.add_argument(
        "--copy",
        action="store_true",
        help="Copy owner token to Windows clipboard without printing it",
    )
    return p


def emit(obj):
    print(
        json.dumps(obj, ensure_ascii=False, indent=2),
        file=sys.stderr if "serve" in sys.argv[1:] else sys.stdout,
        flush=True,
    )


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parser().parse_args()
    if args.command == "version":
        emit({"name": "Codex-Control-MCP", "version": __version__})
        return 0
    try:
        cfg = Config.load(args.home)
        if args.command == "install":
            cfg.initialize_storage()
            path = cfg.home / "config.toml"
            if not path.exists():
                path.write_text(DEFAULT_CONFIG, encoding="utf-8")
            from .auth import owner_token

            owner_token(cfg, create=True)
            emit(
                {
                    "ok": True,
                    "home": str(cfg.home),
                    "config": str(path),
                    "http_token": "encrypted_with_windows_dpapi",
                    "sandbox": "dangerFullAccess",
                    "is_admin": is_admin(),
                    "codex_global_configuration_modified": False,
                }
            )
            return 0
        if args.command in ("status", "doctor", "capabilities", "call"):
            from .client import call_running_service

            tool = (
                "codex_capabilities"
                if args.command == "capabilities"
                else (
                    "codex_health"
                    if args.command in ("doctor", "status")
                    else args.tool
                )
            )
            params = (
                {"active": args.active, "refresh": args.refresh}
                if args.command == "doctor"
                else (json.loads(args.args_json) if args.command == "call" else {})
            )
            running = asyncio.run(call_running_service(cfg, tool, params))
            if running is not None:
                emit(running)
                return 0 if running["ok"] else 1
        if args.command == "status":
            path = cfg.home / "state/runtime.json"
            last = json.loads(path.read_text("utf-8")) if path.exists() else None
            emit(
                {
                    "ok": False,
                    "service": "offline_or_unreachable",
                    "last_runtime_record": last,
                    "record_is_historical": True,
                    "live_process_status": "use doctor or connected codex_health for live verification",
                }
            )
            return 1
        if args.command in ("connect", "connections", "disconnect"):
            if not cfg.oauth.get("enabled"):
                raise BridgeError(
                    "capability_unavailable",
                    "Owner OAuth is not enabled in this configuration.",
                )
            from .oauth import OAuthStore, create_pairing_secret

            if args.command == "connect":
                if not args.copy or os.name != "nt":
                    raise BridgeError(
                        "invalid_arguments",
                        "Run connect --copy on the owner Windows computer.",
                    )
                import win32clipboard

                value = create_pairing_secret(cfg.home)
                try:
                    win32clipboard.OpenClipboard()
                    try:
                        win32clipboard.EmptyClipboard()
                        win32clipboard.SetClipboardText(
                            value, win32clipboard.CF_UNICODETEXT
                        )
                    finally:
                        win32clipboard.CloseClipboard()
                except Exception:
                    store = OAuthStore(cfg.home)
                    try:
                        store.remove_kinds("pairing", "enrollment")
                    finally:
                        store.close()
                    raise BridgeError(
                        "clipboard_unavailable",
                        "Pairing was cancelled because the local clipboard was unavailable.",
                    )
                emit(
                    {
                        "ok": True,
                        "mcp_url": cfg.oauth["issuer"] + "mcp",
                        "pairing_code": "copied_to_local_clipboard",
                        "valid_for_seconds": 300,
                        "single_use": True,
                        "authorization": "explicit user approval on the owner HTTPS consent page required",
                    }
                )
            else:
                store = OAuthStore(cfg.home)
                try:
                    if args.command == "disconnect":
                        if not args.all:
                            raise BridgeError(
                                "invalid_arguments",
                                "Use disconnect --all to revoke all OAuth connections.",
                            )
                        store.remove_kinds(
                            "client",
                            "pending",
                            "code",
                            "pairing",
                            "enrollment",
                            "access",
                            "refresh",
                            "used_refresh",
                        )
                    emit(
                        {
                            "ok": True,
                            "clients": store.count("client"),
                            "active_access_grants": store.count("access"),
                            "refresh_grants": store.count("refresh"),
                            "enrollment_open": bool(store.get("enrollment", "active")),
                            "owner_bearer_unchanged": True,
                        }
                    )
                finally:
                    store.close()
            return 0
        if args.command == "token":
            if not args.copy:
                raise BridgeError(
                    "invalid_arguments",
                    "Use token --copy to copy the credential locally without printing it.",
                )
            if os.name != "nt":
                raise BridgeError(
                    "capability_unavailable", "Clipboard token copying is Windows-only."
                )
            from .auth import owner_token
            import win32clipboard

            token = owner_token(cfg)
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardText(token, win32clipboard.CF_UNICODETEXT)
            finally:
                win32clipboard.CloseClipboard()
            emit(
                {
                    "ok": True,
                    "token": "copied_to_local_clipboard",
                    "warning": "Clipboard contains the full-access MCP credential. Do not paste it into chat or logs.",
                }
            )
            return 0
        if args.command == "stop":
            from .lifecycle import request_stop
            import time

            path = cfg.home / (
                "state/service.json"
                if args.target == "core"
                else "state/tunnel-service.json"
            )
            if not path.exists():
                emit({"ok": True, "target": args.target, "state": "already_stopped"})
                return 0
            record = json.loads(path.read_text("utf-8"))
            identity = record.get("instance_id")
            from .lifecycle import process_alive

            if process_alive(record.get("pid")) is False:
                child = record.get("child_pid")
                if child and process_alive(child) is not False:
                    raise BridgeError(
                        "execution_state_unknown",
                        "Stale service record has a possibly live child; ownership must be inspected before cleanup.",
                    )
                # Do not kill a PID or race-delete a new service record. The next start replaces stale metadata.
                emit(
                    {
                        "ok": True,
                        "target": args.target,
                        "state": "already_stopped",
                        "record_is_historical": True,
                    }
                )
                return 0
            if not record.get("graceful_stop_supported") or not identity:
                raise BridgeError(
                    "version_incompatible",
                    "This older service does not provide a graceful stop signal. Use the verified upgrade transaction, not name-based process termination.",
                )
            request_stop(identity)
            # HTTP drains for up to 20 seconds before closing its owned official
            # runtime (up to 12 seconds plus reader joins). The CLI must outlast
            # both stages; a 25-second deadline falsely reported normal drains.
            deadline = time.monotonic() + 45
            stopped = False
            while time.monotonic() < deadline:
                if not path.exists():
                    stopped = True
                    break
                try:
                    if (
                        json.loads(path.read_text("utf-8")).get("instance_id")
                        != identity
                    ):
                        stopped = True
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(0.2)
            emit(
                {
                    "ok": stopped,
                    "target": args.target,
                    "stop_requested": True,
                    "state": "stopped" if stopped else "shutdown_in_progress",
                }
            )
            return 0 if stopped else 1
        if args.command == "tunnel":
            from .tunnel import run_tunnel

            return run_tunnel(cfg)
        if args.command == "serve":
            from .server import run_stdio, run_http

            asyncio.run(
                run_stdio(cfg)
                if args.transport == "stdio"
                else run_http(cfg, args.host, args.port)
            )
            return 0
        from .bridge import Bridge

        bridge = Bridge(cfg)
        try:
            if args.command == "doctor":
                out = bridge.execute(
                    "codex_health", {"active": args.active, "refresh": args.refresh}
                )
            elif args.command == "capabilities":
                out = bridge.execute("codex_capabilities", {})
            else:
                out = bridge.execute(args.tool, json.loads(args.args_json))
            emit(out)
            return 0 if out["ok"] else 1
        finally:
            bridge.close()
    except BridgeError as e:
        emit({"ok": False, "error": e.as_dict()})
        return 1
    except (OSError, ValueError) as e:
        emit(
            {
                "ok": False,
                "error": {
                    "code": "configuration_or_io_error",
                    "message": type(e).__name__,
                },
            }
        )
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
