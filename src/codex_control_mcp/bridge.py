from __future__ import annotations
import base64, concurrent.futures, contextvars, json, os, threading, time, uuid, pathlib
from . import __version__
from .common import (
    CURRENT_OPERATION,
    ELICITATION_FORWARDER,
    Audit,
    InstanceLock,
    absolute_path,
    atomic_json,
    digest,
    is_admin,
    ps_argv,
    ps_quote,
    utc_now,
)
from .config import build_environment
from .discovery import Discovery
from .errors import BridgeError
from .idempotency import Idempotency
from .task_runtime import RecoverableTaskStore
from .dynamic_mcp import DynamicMCPManager
from .skill_manager import SkillManager
from .host_runtime import HostManager
from .rpc import AppServer
from .schema import load_or_export, ALLOWED_METHODS
from .sessions import SessionStore
from .tools import validate_tool

BACKEND_TIME = contextvars.ContextVar("backend_time", default=0.0)
READ_TOOLS = {
    "codex_capabilities",
    "codex_health",
    "read_file",
    "list_dir",
    "search_files",
    "search_text",
    "git_status",
    "git_diff",
    "git_log",
    "session_read",
    "session_list",
    "remote_status",
    "computer_snapshot",
    "browser_snapshot",
    "mcp_tool_search",
    "mcp_tool_inspect",
    "host_route",
}
FILE_WRITES = {
    "file_add",
    "file_move",
    "file_delete",
    "file_patch",
    "git_commit",
    "git_branch",
    "task_manage",
    "mcp_manage",
    "mcp_tool_call",
    "host_manage",
    "host_exec",
    "host_files",
    "skill_package",
}


class Bridge:
    def __init__(self, cfg):
        self.cfg = cfg
        cfg.initialize_storage()
        self.audit = Audit(cfg.home / "logs/audit.jsonl")
        self.instance = InstanceLock(cfg.home / "state/owner.lock")
        self.instance.acquire()
        self.idempotency = Idempotency(cfg.home / "state/idempotency.sqlite3")
        self.sessions = SessionStore(
            cfg.home / "state/sessions.json", cfg.session_cache_bytes, cfg.max_sessions
        )
        self.gui = None
        self.browser = None
        self.tasks = RecoverableTaskStore(cfg.home / "state/recoverable-tasks.sqlite3")
        self.dynamic_mcp = DynamicMCPManager(cfg)
        self.skills = SkillManager(cfg)
        self.hosts = HostManager(self, self.dynamic_mcp)
        self.runtime = None
        self.schema = None
        self.schema_info = {}
        self.rpc = None
        self.env, self.proxy = build_environment(cfg)
        self.ready_lock = threading.RLock()
        self.write_lock = threading.RLock()
        self.verified = {}
        self.pending_update = None
        self.last_discovery = 0
        self.closed = False

    def ensure_ready(self, force=False):
        with self.ready_lock:
            if self.closed:
                raise BridgeError("runtime_missing", "Bridge has closed.")
            now = time.monotonic()
            if (
                self.rpc
                and self.rpc.alive
                and not force
                and now - self.last_discovery < 60
            ):
                return
            runtime = Discovery(self.cfg).find()
            self.last_discovery = now
            changed = (
                not self.runtime
                or runtime.file_fingerprint != self.runtime.file_fingerprint
            )
            if self.rpc and self.rpc.alive and not changed and not force:
                return
            if (
                self.rpc
                and self.rpc.alive
                and (
                    self.rpc.pending
                    or any(
                        s["state"] in ("starting", "running")
                        for s in self.sessions.list()
                    )
                )
            ):
                self.pending_update = {
                    "available_version": runtime.cli_version,
                    "available_fingerprint": runtime.file_fingerprint,
                    "reason": "active_operations_or_sessions",
                }
                if force:
                    raise BridgeError(
                        "update_pending",
                        "Runtime refresh deferred until in-flight operations finish; no task was killed.",
                    )
                return
            schema, schema_info = load_or_export(self.cfg, runtime, force)
            replacement = AppServer(
                runtime,
                schema,
                self.cfg,
                self.env,
                self.audit,
                self.sessions.notify,
            )
            previous = self.rpc
            self.rpc = replacement
            self.runtime = runtime
            self.schema, self.schema_info = schema, schema_info
            self.pending_update = None
            if previous:
                previous.close()
            self.verified = {}
            atomic_json(
                self.cfg.home / "state/runtime.json",
                {
                    "runtime": runtime.as_dict(),
                    "schema": self.schema_info,
                    "bridge_pid": os.getpid(),
                    "appserver_pid": self.rpc.proc.pid,
                    "started_at": utc_now(),
                    "effective_sandbox": "dangerFullAccess",
                    "is_admin": is_admin(),
                },
            )

    def _rpc(self, method, params=None, timeout=30):
        # Pin the connection until the request is registered. A concurrent
        # refresh must see the pending request before replacing its transport.
        with self.ready_lock:
            self.ensure_ready()
            rpc = self.rpc
            future = rpc.begin(method, params)
        start = time.perf_counter()
        try:
            try:
                result = future.result(timeout)
            except concurrent.futures.TimeoutError as exc:
                raise BridgeError(
                    "execution_state_unknown",
                    "Timed out waiting for the official response; no replay is allowed.",
                    details={"method": method},
                ) from exc
            if self.rpc is rpc:
                self.verified[method] = {"at": utc_now(), "result": "PASS"}
            return result
        finally:
            BACKEND_TIME.set(BACKEND_TIME.get() + time.perf_counter() - start)

    def _cwd(self, value=None):
        return absolute_path(value or self.cfg.cwd, self.cfg.cwd)

    def _argv(self, a):
        argv = a.get("argv")
        command = a.get("command")
        if bool(argv) == (command is not None):
            raise BridgeError(
                "invalid_arguments", "Provide exactly one of nonempty argv or command."
            )
        if argv:
            if not all(isinstance(s, str) and "\0" not in s for s in argv):
                raise BridgeError(
                    "invalid_arguments", "argv strings must not contain NUL."
                )
            return argv
        if not isinstance(command, str) or not command or "\0" in command:
            raise BridgeError(
                "invalid_arguments", "command must be nonempty text without NUL."
            )
        shell = a.get("shell", "powershell")
        if shell == "powershell":
            return ps_argv(
                "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);$OutputEncoding=[Console]::OutputEncoding;"
                + command
            )
        if shell == "cmd":
            return [
                os.environ.get("COMSPEC", "cmd.exe"),
                "/d",
                "/s",
                "/c",
                "chcp 65001>nul & " + command,
            ]
        if shell == "wsl":
            argv = ["wsl.exe"]
            if a.get("wsl_distribution"):
                argv += ["--distribution", a["wsl_distribution"]]
            return argv + [
                "--cd",
                a.get("wsl_cwd", "~"),
                "--exec",
                "bash",
                "-lc",
                command,
            ]
        raise BridgeError(
            "invalid_arguments", "Supported shells: powershell, cmd, wsl."
        )

    def _command_environment(self, args):
        supplied = args.get("env")
        if args.get("shell") != "wsl" or args.get("argv") or not supplied:
            return supplied
        overrides = dict(supplied)
        if "WSLENV" in overrides:
            return overrides
        shared = [entry for entry in self.env.get("WSLENV", "").split(":") if entry]
        names = {entry.split("/", 1)[0] for entry in shared}
        for name in overrides:
            if any(character in name for character in (":", "/", "=", "\0")):
                raise BridgeError("invalid_arguments", "WSL environment variable name cannot be represented in WSLENV.")
            if name not in names:
                shared.append(name + "/u")
        overrides["WSLENV"] = ":".join(shared)
        return overrides

    def _command(
        self, argv, cwd=None, timeout_ms=30000, env=None, output_limit_bytes=None
    ):
        self.ensure_ready()
        cap = output_limit_bytes or self.cfg.output_limit_bytes
        params = {
            "command": argv,
            "cwd": self._cwd(cwd),
            "sandboxPolicy": {"type": "dangerFullAccess"},
            "timeoutMs": timeout_ms,
            "outputBytesCap": cap,
        }
        if env is not None:
            params["env"] = env
        result = self._rpc("command/exec", params, timeout=timeout_ms / 1000 + 8)
        stdout = result["stdout"]
        stderr = result["stderr"]
        return {
            "exit_code": result["exitCode"],
            "stdout": stdout,
            "stderr": stderr,
            "output_limit_bytes": cap,
            "output_truncation": "possible"
            if max(len(stdout.encode("utf-8")), len(stderr.encode("utf-8"))) >= cap
            else "not_observed",
            "execution_backend": "codex_app_server.command_exec",
            "effective_sandbox": "dangerFullAccess",
        }

    def _require_success(self, data):
        if data["exit_code"] != 0:
            raise BridgeError(
                "command_failed",
                "The official command exited unsuccessfully.",
                details={
                    "exit_code": data["exit_code"],
                    "stdout": data["stdout"],
                    "stderr": data["stderr"],
                },
            )
        return data

    def _read_bytes(self, path):
        result = self._command(
            ps_argv(
                "$ErrorActionPreference='Stop';$f=Get-Item -LiteralPath "
                + ps_quote(path)
                + ";if($f.PSIsContainer){throw 'Expected a file'};[Console]::Write($f.Length)"
            )
        )
        self._require_success(result)
        try:
            size = int(result["stdout"].strip())
        except ValueError:
            raise BridgeError("backend_error", "Could not determine file byte length.")
        if size > self.cfg.file_limit_bytes:
            raise BridgeError(
                "output_limit_exceeded", "File exceeds bounded read limit."
            )
        raw = base64.b64decode(
            self._rpc("fs/readFile", {"path": path})["dataBase64"], validate=True
        )
        if len(raw) > self.cfg.file_limit_bytes:
            raise BridgeError(
                "output_limit_exceeded", "File grew beyond bounded read limit."
            )
        return raw

    def _git(self, args, cwd=None, **kwargs):
        self.ensure_ready()
        if not self.runtime.git_path:
            raise BridgeError(
                "capability_unavailable",
                "Git was not discovered. Configure git_path; no Git reimplementation is used.",
            )
        result = self._command(
            [self.runtime.git_path, "--no-pager", "-c", "core.quotepath=false"] + args,
            cwd,
            **kwargs,
        )
        result["operation_backend"] = "git_cli"
        return result

    def _git_stdin(self, args, text, cwd):
        self.ensure_ready()
        if not self.runtime.git_path:
            raise BridgeError("capability_unavailable", "Git was not discovered.")
        result = self._session_start(
            {
                "argv": [self.runtime.git_path, "--no-pager"] + args,
                "cwd": cwd,
                "timeout_ms": 30000,
            }
        )
        sid = result["session_id"]
        s = self.sessions.get(sid, self.rpc.generation)
        self._rpc(
            "command/exec/write",
            {
                "processId": sid,
                "deltaBase64": base64.b64encode(text.encode("utf-8")).decode(),
                "closeStdin": True,
            },
        )
        try:
            s.future.result(35)
        except concurrent.futures.TimeoutError:
            raise BridgeError(
                "execution_state_unknown",
                "Patch command did not finish in the wait window.",
            )
        s.finished.wait(2)
        data = s.read()
        return {
            "exit_code": s.exit_code,
            "stdout": data["stdout"],
            "stderr": data["stderr"],
            "execution_backend": "codex_app_server.command_exec",
            "operation_backend": "git_cli.apply",
            "native_codex_patch": False,
        }

    def _session_start(self, a):
        argv = self._argv(a)
        cwd = self._cwd(a.get("cwd"))
        params = {
            "command": argv,
            "cwd": cwd,
            "sandboxPolicy": {"type": "dangerFullAccess"},
            "streamStdin": True,
            "streamStdoutStderr": True,
            "disableOutputCap": True,
            "tty": a.get("tty", False),
        }
        timeout = a.get("timeout_ms", 3600000)
        if timeout == 0:
            params["disableTimeout"] = True
        else:
            params["timeoutMs"] = timeout
        environment = self._command_environment(a)
        if environment is not None:
            params["env"] = environment
        with self.ready_lock:
            self.ensure_ready()
            s = self.sessions.create(self.rpc.generation, cwd, a.get("tty", False))
            params["processId"] = s.id
            try:
                s.future = self.rpc.begin("command/exec", params)
            except BaseException as exc:
                s.state = "lost" if isinstance(exc, BridgeError) and exc.code == "execution_state_unknown" else "failed"
                s.error = exc.as_dict() if isinstance(exc, BridgeError) else {"code": "execution_state_unknown", "message": "Session startup failed; no replay performed."}
                s.finished.set()
                self.sessions.save()
                raise
        try:
            def completed(f):
                s.finish(f)
                self.sessions.save()

            s.future.add_done_callback(completed)
        except BridgeError as e:
            s.state = "failed"
            s.error = e.as_dict()
            self.sessions.save()
            raise
        try:
            s.future.result(timeout=0.03)
        except concurrent.futures.TimeoutError:
            pass
        if s.error:
            raise BridgeError(s.error["code"], s.error["message"])
        return s.metadata()

    def _do(self, tool, a):
        if tool.startswith("browser_"):
            if not self.cfg.browser_use_enabled:
                raise BridgeError(
                    "capability_unavailable",
                    "Browser is not enabled for this bridge configuration.",
                )
            with self.ready_lock:
                if self.browser is None or self.browser.closed:
                    if self.cfg.browser.get('backend', 'official') == 'tabbit':
                        from .browser_tabbit import TabbitBrowser
                        self.browser = TabbitBrowser(self.cfg)
                    else:
                        self.ensure_ready()
                        from .browser import OfficialBrowser
                        self.browser = OfficialBrowser(self.cfg)
            started = time.perf_counter()
            try:
                return self.browser.call(tool, a, ELICITATION_FORWARDER.get())
            finally:
                BACKEND_TIME.set(BACKEND_TIME.get() + time.perf_counter() - started)
        if tool.startswith("computer_"):
            if not self.cfg.computer_use_enabled:
                raise BridgeError(
                    "capability_unavailable",
                    "Official Computer Use is not enabled for this bridge configuration.",
                )
            self.ensure_ready()
            if self.gui is None:
                from .computer import OfficialComputer

                self.gui = OfficialComputer(self.cfg)
            started = time.perf_counter()
            try:
                return self.gui.call(tool, a, ELICITATION_FORWARDER.get())
            finally:
                BACKEND_TIME.set(BACKEND_TIME.get() + time.perf_counter() - started)
        if tool == "task_manage":
            return self.tasks.manage(a)
        if tool == "mcp_manage":
            return self.dynamic_mcp.manage(a)
        if tool == "mcp_tool_search":
            return self.dynamic_mcp.search(a)
        if tool == "mcp_tool_inspect":
            return self.dynamic_mcp.inspect(a)
        if tool == "mcp_tool_call":
            return self.dynamic_mcp.call(a)
        if tool == "skill_package":
            return self.skills.package(a)
        if tool == "host_manage":
            return self.hosts.manage(a)
        if tool == "host_exec":
            return self.hosts.exec(a)
        if tool == "host_files":
            return self.hosts.files(a)
        if tool == "host_route":
            return self.hosts.route(a)
        if tool == "codex_capabilities":
            return self.capabilities()
        if tool == "codex_health":
            return self.health(a.get("active", False), a.get("refresh", False))
        if tool == "exec_command":
            if a.get("execution_mode") == "session":
                return self._session_start(a)
            return self._command(
                self._argv(a),
                a.get("cwd"),
                a.get("timeout_ms", 30000),
                self._command_environment(a),
                a.get("output_limit_bytes"),
            )
        if tool == "session_start":
            return self._session_start(a)
        if tool == "session_list":
            return {"sessions": self.sessions.list()}
        if tool == "session_read":
            # Cached output remains readable even if Codex is uninstalled,
            # disconnected or waiting for a compatible update.
            s = self.sessions.get(a["session_id"])
            return s.read(a.get("cursor", 0), a.get("max_bytes", 262144))
        if tool in ("session_write", "session_kill", "session_resize"):
            self.ensure_ready()
            s = self.sessions.get(a["session_id"], self.rpc.generation)
            if s.state not in ("starting", "running"):
                raise BridgeError(
                    "session_not_running", "The requested session has stopped."
                )
            if tool == "session_write":
                self._rpc(
                    "command/exec/write",
                    {
                        "processId": s.id,
                        "deltaBase64": base64.b64encode(
                            a.get("text", "").encode("utf-8")
                        ).decode(),
                        "closeStdin": a.get("close_stdin", False),
                    },
                )
            if tool == "session_kill":
                self._rpc("command/exec/terminate", {"processId": s.id})
            if tool == "session_resize":
                if not s.tty:
                    raise BridgeError(
                        "invalid_arguments", "Only PTY sessions can be resized."
                    )
                self._rpc(
                    "command/exec/resize",
                    {"processId": s.id, "size": {"rows": a["rows"], "cols": a["cols"]}},
                )
            return {**s.metadata(), "control_request_acknowledged": True}
        if tool == "read_file":
            path = absolute_path(a["path"], self.cfg.cwd)
            raw = self._read_bytes(path)
            try:
                content = raw.decode(a.get("encoding", "utf-8"))
            except (UnicodeError, LookupError):
                raise BridgeError(
                    "encoding_error", "File cannot be decoded using requested encoding."
                )
            lines = content.splitlines(keepends=True)
            start = a.get("start_line", 1)
            count = a.get("max_lines", 200)
            if a.get("end_line") is not None:
                if a["end_line"] < start:
                    raise BridgeError(
                        "invalid_arguments", "end_line must be at or after start_line."
                    )
                count = a["end_line"] - start + 1
            text = "".join(lines[start - 1 : start - 1 + count])
            limit = a.get("max_bytes", 262144)
            encoded = text.encode("utf-8")
            truncated = len(encoded) > limit
            if truncated:
                text = encoded[:limit].decode("utf-8", "ignore")
            return {
                "path": path,
                "content": text,
                "sha256": digest(raw),
                "file_bytes": len(raw),
                "total_lines": len(lines),
                "start_line": start,
                "lines_returned": len(text.splitlines()),
                "truncated": truncated or start - 1 + count < len(lines),
                "next_line": None
                if truncated
                else (start + count if start - 1 + count < len(lines) else None),
                "has_utf8_bom": raw.startswith(b"\xef\xbb\xbf"),
                "backend": "codex_app_server.fs.readFile",
            }
        if tool == "list_dir":
            path = absolute_path(a["path"], self.cfg.cwd)
            data = self._rpc("fs/readDirectory", {"path": path})
            offset = a.get("offset", 0)
            count = a.get("limit", 200)
            entries = sorted(data["entries"], key=lambda e: e["fileName"].casefold())
            return {
                "path": path,
                "entries": entries[offset : offset + count],
                "total_entries": len(entries),
                "next_offset": offset + count
                if offset + count < len(entries)
                else None,
                "backend": "codex_app_server.fs.readDirectory",
            }
        if tool == "file_add":
            path = absolute_path(a["path"], self.cfg.cwd)
            raw = a["content"].encode("utf-8")
            if len(raw) > self.cfg.file_limit_bytes:
                raise BridgeError(
                    "output_limit_exceeded", "New file exceeds configured limit."
                )
            target = pathlib.Path(path)
            stage = str(target.parent / (".ccm-" + uuid.uuid4().hex + ".tmp"))
            renamed = False
            try:
                self._rpc(
                    "fs/writeFile",
                    {"path": stage, "dataBase64": base64.b64encode(raw).decode()},
                )
                script = (
                    "$ErrorActionPreference='Stop';[IO.File]::Move("
                    + ps_quote(stage)
                    + ","
                    + ps_quote(path)
                    + ")"
                )
                self._require_success(self._command(ps_argv(script)))
                renamed = True
                if self._read_bytes(path) != raw:
                    raise BridgeError(
                        "concurrent_modification",
                        "File changed before verification; no rollback attempted.",
                    )
            finally:
                if not renamed:
                    try:
                        self._rpc(
                            "fs/remove",
                            {"path": stage, "recursive": False, "force": True},
                        )
                    except Exception:
                        self.audit.emit("staging_cleanup_unverified", tool="file_add")
            return {
                "path": path,
                "bytes": len(raw),
                "sha256": digest(raw),
                "atomic_create_new": True,
                "backend": "codex_app_server.fs.writeFile -> Windows.File.Move(no overwrite)",
            }
        if tool == "file_delete":
            path = absolute_path(a["path"], self.cfg.cwd)
            self._rpc(
                "fs/remove",
                {
                    "path": path,
                    "recursive": a.get("recursive", False),
                    "force": a.get("force", False),
                },
            )
            return {
                "path": path,
                "removed": True,
                "recursive": a.get("recursive", False),
                "backend": "codex_app_server.fs.remove",
            }
        if tool == "file_move":
            src = absolute_path(a["source"], self.cfg.cwd)
            dst = absolute_path(a["destination"], self.cfg.cwd)
            self._require_success(
                self._command(
                    ps_argv(
                        "$ErrorActionPreference='Stop';[IO.File]::Move("
                        + ps_quote(src)
                        + ","
                        + ps_quote(dst)
                        + ")"
                    )
                )
            )
            return {
                "source": src,
                "destination": dst,
                "overwrite": False,
                "atomicity": "not_guaranteed_across_volumes",
                "backend": "codex_app_server.command_exec -> Windows.File.Move",
            }
        if tool == "file_patch":
            cwd = self._cwd(a.get("cwd"))
            patch = a["patch"]
            if not patch.endswith("\n"):
                raise BridgeError(
                    "invalid_arguments", "Unified patch must end with newline."
                )
            for path, expected in a.get("expected_sha256", {}).items():
                if digest(self._read_bytes(absolute_path(path, cwd))) != expected:
                    raise BridgeError(
                        "concurrent_modification",
                        "Patch base does not match expected SHA-256.",
                    )
            self._require_success(
                self._git_stdin(
                    ["apply", "--check", "--whitespace=nowarn", "-"], patch, cwd
                )
            )
            result = self._git_stdin(["apply", "--whitespace=nowarn", "-"], patch, cwd)
            result["external_writer_atomicity"] = "not_guaranteed"
            return result
        if tool in ("search_files", "search_text"):
            self.ensure_ready()
            if not self.runtime.rg_path:
                raise BridgeError(
                    "capability_unavailable",
                    "Official bundled or installed ripgrep was not found.",
                )
            path = absolute_path(a["path"], self.cfg.cwd)
            args = [
                self.runtime.rg_path,
                "--hidden",
                "--glob",
                "!.git/**",
                "--maxdepth",
                str(a.get("max_depth", 6)),
            ]
            if a.get("glob"):
                args += ["--glob", a["glob"]]
            if tool == "search_files":
                args += ["--files", "--", path]
            else:
                args += (
                    [
                        "--json",
                        "--max-count",
                        str(a.get("max_results", 100) + 1),
                        "--max-filesize",
                        "2M",
                    ]
                    + ([] if a.get("regex", False) else ["--fixed-strings"])
                    + ["--", a["query"], path]
                )
            result = self._command(args, output_limit_bytes=self.cfg.output_limit_bytes)
            if result["exit_code"] not in (0, 1):
                return result
            if tool == "search_files":
                values = result["stdout"].splitlines()
            else:
                values = []
                for line in result["stdout"].splitlines():
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if record.get("type") == "match":
                        d = record["data"]
                        values.append(
                            {
                                "path": d["path"].get("text"),
                                "line": d["line_number"],
                                "text": d["lines"].get("text"),
                            }
                        )
            count = a.get("max_results", 100)
            return {
                "matches": values[:count],
                "truncated": len(values) > count
                or result["output_truncation"] == "possible",
                "backend": "codex_app_server.command_exec -> ripgrep",
            }
        if tool == "git_status":
            return self._git(["status", "--porcelain=v1", "--branch"], a.get("cwd"))
        if tool == "git_diff":
            return self._git(
                ["diff", "--no-ext-diff", "--no-textconv"]
                + (["--cached"] if a.get("staged", False) else [])
                + ["--"]
                + a.get("paths", []),
                a.get("cwd"),
            )
        if tool == "git_log":
            return self._git(
                [
                    "log",
                    "--no-decorate",
                    "--oneline",
                    "--max-count=" + str(a.get("limit", 20)),
                ],
                a.get("cwd"),
            )
        if tool == "git_branch":
            action = a.get("action", "list")
            if action == "list":
                return self._git(["branch", "--list", "--no-color"], a.get("cwd"))
            name = a.get("name", "")
            if not name or name.startswith("-"):
                raise BridgeError(
                    "invalid_arguments", "A valid branch name is required."
                )
            self._require_success(
                self._git(["check-ref-format", "--branch", name], a.get("cwd"))
            )
            if action == "create":
                return self._git(["branch", "--", name], a.get("cwd"))
            if action == "switch":
                return self._git(["switch", name], a.get("cwd"))
            raise BridgeError("invalid_arguments", "Unknown branch action.")
        if tool == "git_commit":
            paths = a["paths"]
            if not paths or not a["message"].strip():
                raise BridgeError(
                    "invalid_arguments",
                    "Commit requires explicit files and a nonempty message.",
                )
            self._require_success(self._git(["add", "--"] + paths, a.get("cwd")))
            result = self._git(
                ["commit", "--only", "-m", a["message"], "--"] + paths,
                a.get("cwd"),
                timeout_ms=120000,
            )
            result["staging_may_have_changed"] = True
            return result
        if tool == "remote_status":
            return {
                "scope": "bridge_owned_app_server",
                "desktop_remote_status": "unknown",
                "status": self._rpc("remoteControl/status/read"),
            }
        raise BridgeError(
            "capability_unavailable",
            "No verified adapter for this tool in the current build.",
        )

    def capabilities(self):
        self.ensure_ready()
        runtime_view = self.runtime.as_dict()
        if self.browser and self.browser.verified:
            runtime_view["browser_supported"] = "verified"
        if self.gui and self.gui.verified:
            runtime_view["computer_use_supported"] = "verified"
        extensions_verified = (
            (not self.cfg.browser_use_enabled or bool(self.browser and self.browser.verified))
            and (not self.cfg.computer_use_enabled or bool(self.gui and self.gui.verified))
        )
        return {
            "runtime": runtime_view,
            "generation": self.rpc.generation,
            "pending_update": self.pending_update,
            "schema_hash": self.schema.hash,
            "permission_mode": self.cfg.permission_mode,
            "application_access_policy": self.cfg.application_access_policy,
            "client_elicitation_required": self.cfg.requires_client_elicitation,
            "effective_sandbox": "dangerFullAccess",
            "is_admin": is_admin(),
            "rpc_surface": {
                m: {
                    "present": m in self.schema.methods,
                    "callability": "verified" if m in self.verified else "unverified",
                    "last_test": self.verified.get(m),
                }
                for m in sorted(ALLOWED_METHODS)
            },
            "browser": {
                "enabled": self.cfg.browser_use_enabled,
                "configured_backend": self.cfg.browser.get('backend', 'official'),
                "verified_operations": sorted(self.browser.verified_operations)
                if self.browser
                else [],
                "full_action_chain_verified": bool(
                    self.browser and self.browser.verified
                ),
                "reason": None
                if self.browser and self.browser.verified
                else "configured_browser_adapter_not_yet_verified",
                "runtime": self.browser.info if self.browser else None,
            },
            "computer_use": {
                "enabled": self.cfg.computer_use_enabled,
                "verified_operations": sorted(self.gui.verified_operations)
                if self.gui
                else [],
                "full_action_chain_verified": bool(self.gui and self.gui.verified),
                "reason": None
                if self.gui and self.gui.verified
                else (getattr(self.gui, "last_failure", None) or "official_adapter_not_yet_verified"),
                "runtime": self.gui.info if self.gui else None,
            },
            "web_chatgpt": {
                "verified": False,
                "reason": "external_https_connector_not_yet_tested",
            },
            "client_refresh_required": "client_dependent_not_observable",
            "phase1_complete": extensions_verified,
        }

    def health(self, active=False, refresh=False):
        self.ensure_ready(force=refresh)
        checks = {
            "codex": "PASS",
            "app_server": "PASS",
            "schema": "PASS",
            "shell": "NOT_TESTED",
            "files": "NOT_TESTED",
            "browser": "UNVERIFIED",
            "computer_use": "UNVERIFIED",
            "proxy": "NOT_TESTED",
        }
        if self.browser and self.browser.verified:
            checks['browser'] = 'PASS'
        if self.gui and self.gui.verified:
            checks['computer_use'] = 'PASS'
        child_admin = None
        if active:
            out = self._command(
                ps_argv(
                    "Write-Output 'CODEX_CONTROL_MCP_OK';([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)"
                )
            )
            checks["shell"] = (
                "PASS"
                if out["exit_code"] == 0 and "CODEX_CONTROL_MCP_OK" in out["stdout"]
                else "FAIL"
            )
            child_admin = "True" in out["stdout"]
            self._rpc("fs/readDirectory", {"path": self.cfg.cwd})
            checks["files"] = "PASS"
            child = self._command(
                ps_argv(
                    "$n=@('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY');$r=@{};foreach($k in $n){$r[$k]=!![Environment]::GetEnvironmentVariable($k)};$r|ConvertTo-Json -Compress"
                )
            )
            try:
                presence = json.loads(child["stdout"])
            except ValueError:
                presence = {}
            self.proxy["child_environment_verified"] = bool(presence) and all(
                presence.get(k) for k in self.proxy["env_names_present"]
            )
            route_verified = False
            self.proxy.pop("route_evidence", None)
            self.proxy.pop("route_failure", None)
            if self.proxy["child_environment_verified"] and self.proxy["env_names_present"]:
                from .proxy_probe import probe_argv, route_is_verified

                try:
                    route = self._command(probe_argv(), timeout_ms=30000)
                    evidence = json.loads(route.get("stdout", ""))
                    route_verified = route["exit_code"] == 0 and route_is_verified(evidence)
                    self.proxy["route_evidence"] = evidence
                    if not route_verified:
                        self.proxy["route_failure"] = "connect_tls_or_peer_verification_failed"
                except (BridgeError, OSError, ValueError, RuntimeError) as exc:
                    # A failed probe is not a successful direct route. Do not
                    # leak stderr or environment values containing credentials.
                    self.proxy["route_failure"] = type(exc).__name__
            self.proxy["runtime_route_verified"] = "verified" if route_verified else "unknown"
            checks["proxy"] = (
                "PASS" if route_verified else (
                    "PARTIAL" if self.proxy["child_environment_verified"] else "NOT_TESTED"
                )
            )
        else:
            if "command/exec" in self.verified:
                checks["shell"] = "PASS"
            if "fs/readDirectory" in self.verified or "fs/readFile" in self.verified:
                checks["files"] = "PASS"
        inference = sum(
            v for k, v in self.audit.methods.items() if k not in ALLOWED_METHODS
        )
        runtime_view = self.runtime.as_dict()
        if self.browser and self.browser.verified:
            runtime_view["browser_supported"] = "verified"
        if self.gui and self.gui.verified:
            runtime_view["computer_use_supported"] = "verified"
        required = {"codex", "app_server", "schema", "shell", "files", "proxy"}
        if self.cfg.browser_use_enabled:
            required.add("browser")
        if self.cfg.computer_use_enabled:
            required.add("computer_use")
        phase1_complete = inference == 0 and all(checks[name] == "PASS" for name in required)
        overall = "degraded" if "FAIL" in checks.values() else (
            "healthy" if phase1_complete else "core_available_extensions_unverified"
        )
        return {
            "overall": overall,
            "checks": checks,
            "runtime": runtime_view,
            "appserver_pid": self.rpc.proc.pid,
            "bridge_version": __version__,
            "application_access_policy": self.cfg.application_access_policy,
            "client_elicitation_required": self.cfg.requires_client_elicitation,
            "is_admin": is_admin(),
            "child_is_admin_verified": child_admin,
            "effective_sandbox": "dangerFullAccess",
            "proxy": self.proxy,
            "schema": self.schema_info,
            "pending_update": self.pending_update,
            "model_evidence": {
                "unapproved_rpc_calls_observed": inference,
                "outgoing_methods": dict(self.audit.methods),
                "network_attempts": "unknown",
                "model_free_verification_scope": "bridge emitted RPC allowlist only; see controlled integration report for endpoint observations",
            },
            "phase1_complete": phase1_complete,
        }

    def execute(self, tool, args=None):
        start = time.perf_counter()
        token = BACKEND_TIME.set(0.0)
        operation_id = uuid.uuid4().hex
        operation_token = CURRENT_OPERATION.set(operation_id)
        input_is_object = args is None or isinstance(args, dict)
        a = dict(args or {}) if input_is_object else {}
        key = a.pop("idempotency_key", None)
        risk = (
            "read"
            if tool in READ_TOOLS
            or (tool == "git_branch" and a.get("action", "list") == "list")
            else (
                "dangerous"
                if tool
                in ("exec_command", "session_start", "file_delete", "session_kill", "host_exec", "mcp_tool_call")
                else "write"
            )
        )
        reserved = False
        try:
            if not input_is_object:
                raise BridgeError(
                    "invalid_arguments", "Tool arguments must be an object."
                )
            validate_tool(tool, dict(args or {}))
            if key and risk != "read":
                cached = self.idempotency.reserve(key, tool, a)
                if cached is not None:
                    return {**cached, "idempotent_replay": True}
                reserved = True
            self.audit.emit(
                "tool_start", tool=tool, risk_level=risk, param_keys=sorted(a)
            )
            if tool in FILE_WRITES:
                with self.write_lock:
                    data = self._do(tool, a)
            else:
                data = self._do(tool, a)
            ok = not (
                tool
                in (
                    "exec_command",
                    "file_patch",
                    "git_commit",
                    "git_branch",
                    "git_status",
                    "git_diff",
                    "git_log",
                    "search_files",
                    "search_text",
                )
                and isinstance(data, dict)
                and data.get("exit_code") not in (None, 0)
            )
            out = {
                "ok": ok,
                "tool": tool,
                "risk_level": risk,
                "result": data,
                "error": None
                if ok
                else {
                    "code": "command_failed",
                    "message": "Command returned a nonzero exit code.",
                    "retryable": False,
                },
            }
        except BridgeError as e:
            out = {
                "ok": False,
                "tool": tool,
                "risk_level": risk,
                "result": None,
                "error": e.as_dict(),
            }
        except (KeyError, ValueError, TypeError, UnicodeError) as e:
            out = {
                "ok": False,
                "tool": tool,
                "risk_level": risk,
                "result": None,
                "error": {
                    "code": "invalid_arguments",
                    "message": f"Input rejected ({type(e).__name__}).",
                    "retryable": False,
                },
            }
        except Exception as e:
            out = {
                "ok": False,
                "tool": tool,
                "risk_level": risk,
                "result": None,
                "error": {
                    "code": "internal_error",
                    "message": f"Operation failed ({type(e).__name__}); no fallback executor used.",
                    "retryable": False,
                },
            }
        finally:
            backend = BACKEND_TIME.get()
            BACKEND_TIME.reset(token)
            CURRENT_OPERATION.reset(operation_token)
        out["timing"] = {
            "total_ms": round((time.perf_counter() - start) * 1000, 3),
            "official_rpc_wait_ms": 0 if tool.startswith('browser_') and self.cfg.browser.get('backend') == 'tabbit' else round(backend * 1000, 3),
            "execution_backend_wait_ms": round(backend * 1000, 3),
            "bridge_overhead_ms": round(
                max(0, time.perf_counter() - start - backend) * 1000, 3
            ),
        }
        out["evidence"] = {
            "runtime_version": self.runtime.cli_version if self.runtime else None,
            "schema_hash": self.schema.hash if self.schema else None,
            "effective_sandbox": "dangerFullAccess",
            "bridge_model_rpc_allowlist_enforced": True,
            "full_network_model_verification": "unknown",
        }
        if isinstance(out.get("result"), dict) and "_images" in out["result"]:
            out["_image_blocks"] = out["result"].pop("_images")
        self.audit.emit(
            "tool_finish",
            tool=tool,
            operation_id=operation_id,
            version=self.runtime.cli_version if self.runtime else None,
            proxy_source=self.proxy.get("source"),
            exit_code=(out.get("result") or {}).get("exit_code")
            if isinstance(out.get("result"), dict)
            else None,
            ok=out["ok"],
            risk_level=risk,
            duration_ms=out["timing"]["total_ms"],
            error_code=(out.get("error") or {}).get("code"),
            application_authorization=(out.get("result") or {}).get("application_authorization")
            if isinstance(out.get("result"), dict) else None,
        )
        if reserved:
            self.idempotency.finish(key, out)
        return out

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if self.browser:
                self.browser.close()
            if self.gui:
                self.gui.close()
            if self.rpc:
                self.rpc.close()
            self.sessions.save()
            self.tasks.close()
            self.idempotency.close()
        finally:
            self.instance.close()
