"""Independent Scheduled Task controller for the Codex-Control-MCP Core.

Exit codes:
  0  Core started and verified
  10 Recover request found Core already healthy (no change)
  20 Another live maintenance lease owns recovery
  30 Invalid configuration or request
  40 Existing Core listener did not stop
  50 All configured start candidates failed
  60 Unexpected controller error

The controller must run in its own Scheduled Task.  It never starts a fallback as
its own child: each candidate is another Scheduler task, so stopping the Core
job cannot terminate the controller or the selected fallback host.
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
from datetime import datetime, timedelta, timezone
import json
import os
import msvcrt
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

EXIT_OK = 0
EXIT_ALREADY_HEALTHY = 10
EXIT_LEASE_HELD = 20
EXIT_CONFIG = 30
EXIT_STOP_TIMEOUT = 40
EXIT_ALL_CANDIDATES_FAILED = 50
EXIT_UNEXPECTED = 60


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")
    os.replace(temporary, path)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    SYNCHRONIZE = 0x00100000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == 0x102
    finally:
        kernel32.CloseHandle(handle)


class Lease:
    def __init__(self, path: Path, seconds: int = 300):
        self.path = path
        self.seconds = seconds
        self.owner = f"core-controller:{os.getpid()}"
        self.owned = False
        self.reclaimed: dict[str, Any] | None = None
        self.stream = None

    def _record(self) -> dict[str, Any]:
        now = utc_now()
        return {
            "schema": 1,
            "owner": self.owner,
            "pid": os.getpid(),
            "created_at": iso(now),
            "expires_at": iso(now + timedelta(seconds=self.seconds)),
        }

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Permanent guard inode + OS byte lock; never unlink a synchronization
        # file after unlocking it (another contender could already own it).
        stream = open(str(self.path) + ".guard", "a+b", buffering=0)
        try:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            stream.close()
            return False
        try:
            raw = self.path.read_bytes() if self.path.exists() else b""
            if raw:
                try:
                    self.reclaimed = json.loads(raw.decode("utf-8-sig"))
                except (json.JSONDecodeError, UnicodeError):
                    self.reclaimed = {"invalid": True}
                if isinstance(self.reclaimed, dict):
                    try:
                        alive = pid_alive(int(self.reclaimed.get("pid", 0)))
                    except (TypeError, ValueError):
                        alive = False
                    if alive:
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                        stream.close()
                        return False
            self.stream = stream
            self.owned = True
            self.refresh()
            return True
        except BaseException:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            stream.close()
            raise

    def refresh(self) -> None:
        if self.owned and self.stream is not None:
            atomic_json(self.path, self._record())

    def release(self) -> None:
        if not self.owned:
            return
        try:
            assert self.stream is not None
            self.path.unlink(missing_ok=True)
            self.stream.seek(0)
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            if self.stream is not None:
                self.stream.close()
                self.stream = None
            self.owned = False


def powershell(script: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "[Console]::OutputEncoding=[Text.Encoding]::UTF8;" + script],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def port_pid(port: int) -> int:
    result = powershell(
        "$x=Get-NetTCPConnection -State Listen -LocalPort "
        + str(port)
        + " -ErrorAction SilentlyContinue|Select-Object -First 1;"
        + "if($x){[Console]::Out.Write($x.OwningProcess)}else{[Console]::Out.Write('0')}"
    )
    try:
        return int(result.stdout.strip() or "0") if result.returncode == 0 else 0
    except ValueError:
        return 0


def wait_port(port: int, wanted: bool, seconds: int) -> int:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        pid = port_pid(port)
        if bool(pid) == wanted:
            return pid
        time.sleep(0.5)
    pid = port_pid(port)
    return pid if bool(pid) == wanted else -1


def task_command(verb: str, task: str) -> subprocess.CompletedProcess[str]:
    return powershell(f"{verb}-ScheduledTask -TaskName {ps_quote(task)} -ErrorAction Stop")


def consume_requests(path: Path) -> tuple[str, list[dict[str, Any]]]:
    path.mkdir(parents=True, exist_ok=True)
    requests: list[dict[str, Any]] = []
    invalid = False
    # Unique files plus atomic rename mean a concurrent writer is either consumed
    # completely now or remains intact for the Scheduler's queued invocation.
    for source in sorted(path.glob("*.json")):
        claimed = source.with_suffix(source.suffix + f".{os.getpid()}.claimed")
        try:
            os.replace(source, claimed)
        except (FileNotFoundError, PermissionError):
            continue
        try:
            request = json.loads(claimed.read_text("utf-8-sig"))
            created = datetime.fromisoformat(str(request["created_at"]).replace("Z", "+00:00"))
            if request.get("operation") not in {"restart", "recover"} or utc_now() - created > timedelta(minutes=5):
                invalid = True
            else:
                requests.append(request)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeError):
            invalid = True
        finally:
            claimed.unlink(missing_ok=True)
    if requests:
        return ("restart" if any(r["operation"] == "restart" for r in requests) else "recover"), requests
    return ("invalid" if invalid else "recover"), requests


def load_config(path: Path) -> dict[str, Any]:
    config = json.loads(path.read_text("utf-8-sig"))
    required = {"home", "port", "expected_version", "lease", "report", "request", "host_tasks", "candidates"}
    if not required.issubset(config) or not config["candidates"]:
        raise ValueError("controller config is missing required fields")
    names = set(config["host_tasks"])
    if any(candidate.get("task") not in names for candidate in config["candidates"]):
        raise ValueError("every candidate task must be in host_tasks")
    return config


async def authenticated_mcp_probe(config: dict[str, Any], expected_tool_count: int | None = None) -> dict[str, Any]:
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    import win32crypt
    # Verification must not import potentially broken current-source code.
    raw = (Path(config["home"]) / "state/http-token.dpapi").read_bytes()
    token = win32crypt.CryptUnprotectData(raw, None, None, None, 0)[1].decode("utf-8").strip()
    url = f"http://127.0.0.1:{int(config['port'])}/mcp"
    async with httpx.AsyncClient(
        trust_env=False,
        timeout=float(config.get("probe_timeout_seconds", 12)),
        headers={"Authorization": "Bearer " + token},
    ) as client:
        async with asyncio.timeout(float(config.get("probe_timeout_seconds", 12))):
            async with streamable_http_client(url, http_client=client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    initialized = await session.initialize()
                    tools = await session.list_tools()
    version = str(initialized.serverInfo.version)
    count = len(tools.tools)
    expected_count = int(config.get("expected_tool_count", 47) if expected_tool_count is None else expected_tool_count)
    if initialized.serverInfo.name != "Codex-Control-MCP":
        raise RuntimeError("mcp_identity_mismatch")
    if any(t.name.startswith(("grok_", "director_")) for t in tools.tools):
        raise RuntimeError("unexpected_static_media_tools")
    if version != str(config["expected_version"]):
        raise RuntimeError(f"mcp_version_mismatch:{version}")
    if count != expected_count:
        raise RuntimeError(f"mcp_tool_count_mismatch:{count}")
    return {"mcp_initialized": True, "version": version, "tool_count": count}


def verify_service(config: dict[str, Any], pid: int, expected_tool_count: int | None = None) -> tuple[bool, Any]:
    if pid <= 0:
        return False, "listener_absent"
    service = Path(config["home"]) / "state" / "service.json"
    try:
        value = json.loads(service.read_text("utf-8-sig"))
        if int(value.get("pid", 0)) != pid:
            return False, {"error": "service_pid_mismatch"}
        if str(value.get("version")) != str(config["expected_version"]):
            return False, {"error": "service_version_mismatch"}
    except (OSError, ValueError, TypeError, json.JSONDecodeError, UnicodeError):
        return False, {"error": "service_record_invalid"}
    try:
        proof = asyncio.run(authenticated_mcp_probe(config, expected_tool_count))
    except BaseException as exc:
        return False, {"error": "authenticated_mcp_probe_failed", "type": type(exc).__name__, "detail": str(exc)[:300]}
    return True, proof


def run(config_path: Path) -> int:
    started = utc_now()
    report: dict[str, Any] = {"schema": 1, "started_at": iso(started), "config": str(config_path), "attempts": []}
    lease: Lease | None = None
    code = EXIT_UNEXPECTED
    try:
        config = load_config(config_path)
        lease = Lease(Path(config["lease"]), int(config.get("lease_seconds", 300)))
        if not lease.acquire():
            report["error"] = "active_maintenance_lease"
            code = EXIT_LEASE_HELD
            return code
        report["operation"], report["requests"] = consume_requests(Path(config["request"]))
        if report["operation"] == "invalid":
            report["error"] = "invalid_or_expired_request"
            code = EXIT_CONFIG
            return code
        if lease.reclaimed is not None:
            report["reclaimed_lease"] = lease.reclaimed
        old_pid = port_pid(int(config["port"]))
        report["old_pid"] = old_pid
        if report["operation"] == "recover" and old_pid:
            healthy, detail = verify_service(config, old_pid)
            if not healthy:
                # A healthy, explicitly configured older fallback must not be
                # restarted repeatedly just because the preferred version rose.
                for candidate in config["candidates"]:
                    if "expected_version" not in candidate:
                        continue
                    fallback_config = {**config,
                        "expected_version": candidate["expected_version"],
                        "expected_tool_count": candidate.get("expected_tool_count", config.get("expected_tool_count", 47))}
                    healthy, detail = verify_service(fallback_config, old_pid)
                    if healthy:
                        break
            if healthy:
                report["result"] = "already_healthy"
                report["proof"] = detail
                code = EXIT_ALREADY_HEALTHY
                return code
        lease.refresh()
        for task in config["host_tasks"]:
            stopped = task_command("Stop", task)
            report.setdefault("stop_results", []).append({"task": task, "returncode": stopped.returncode})
        if wait_port(int(config["port"]), False, int(config.get("stop_timeout_seconds", 20))) == -1:
            report["error"] = "listener_did_not_stop"
            code = EXIT_STOP_TIMEOUT
            return code
        for candidate in config["candidates"]:
            lease.refresh()
            attempt = {"name": candidate["name"], "task": candidate["task"], "started_at": iso(utc_now())}
            result = task_command("Start", candidate["task"])
            attempt["task_start_returncode"] = result.returncode
            if result.returncode:
                attempt["error"] = (result.stderr or result.stdout)[-800:]
                report["attempts"].append(attempt)
                continue
            pid = wait_port(int(config["port"]), True, int(candidate.get("timeout_seconds", 30)))
            candidate_count = candidate.get("expected_tool_count", config.get("expected_tool_count", 47))
            candidate_config = {**config, "expected_version": candidate.get("expected_version", config["expected_version"])}
            if "expected_tool_count" in candidate:
                healthy, detail = verify_service(candidate_config, pid, int(candidate_count))
            else:
                # Keep legacy/test configs compatible while production candidates may pin distinct counts.
                healthy, detail = verify_service(candidate_config, pid)
            attempt.update({"pid": max(pid, 0), "expected_tool_count": int(candidate_count), "verification": detail, "ok": healthy})
            report["attempts"].append(attempt)
            if healthy:
                report.update({"result": "started", "selected": candidate["name"], "new_pid": pid})
                code = EXIT_OK
                return code
            task_command("Stop", candidate["task"])
            stopped = wait_port(int(config["port"]), False, int(config.get("stop_timeout_seconds", 20)))
            if stopped == -1:
                report["error"] = "failed_candidate_listener_did_not_stop"
                code = EXIT_STOP_TIMEOUT
                return code
        report["error"] = "all_candidates_failed"
        code = EXIT_ALL_CANDIDATES_FAILED
        return code
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        report["error"] = f"configuration_error:{type(exc).__name__}:{exc}"
        code = EXIT_CONFIG
        return code
    except BaseException as exc:
        report["error"] = f"unexpected:{type(exc).__name__}:{exc}"
        code = EXIT_UNEXPECTED
        return code
    finally:
        if lease is not None:
            lease.release()
        report["exit_code"] = code
        report["finished_at"] = iso(utc_now())
        try:
            target = Path(config["report"]) if "config" in locals() else config_path.with_suffix(".report.json")
            atomic_json(target, report)
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    return run(args.config)


if __name__ == "__main__":
    raise SystemExit(main())
