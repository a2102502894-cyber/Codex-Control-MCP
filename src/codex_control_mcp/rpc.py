from __future__ import annotations
import concurrent.futures, json, os, subprocess, threading, uuid
from . import __version__
from .common import CREATE_NO_WINDOW, digest, is_admin
from .errors import BridgeError


class AppServer:
    def __init__(self, runtime, schema, cfg, env, audit, on_notification=None):
        self.runtime, self.schema, self.cfg, self.audit = runtime, schema, cfg, audit
        self.callback = on_notification or (lambda m: None)
        self.write_lock = threading.Lock()
        self.lock = threading.RLock()
        self.pending = {}
        self.next_id = 0
        self.closed = False
        self.broken = False
        self.stderr_lines = 0
        self.generation = uuid.uuid4().hex
        self.process_job = None
        self.proc = subprocess.Popen(
            [
                runtime.codex_path,
                "app-server",
                "--stdio",
                "-c",
                'sandbox_mode="danger-full-access"',
                "-c",
                'approval_policy="never"',
                "-c",
                "analytics.enabled=false",
                "-c",
                "feedback.enabled=false",
            ],
            cwd=cfg.cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )
        if os.name == "nt":
            from .lifecycle import create_owned_process_job

            try:
                # Assign before initialize can start any configured helpers.
                # Descendants may inherit stdout/stderr and outlive the server;
                # owning their lifetime prevents blocked pipe-reader shutdown.
                self.process_job = create_owned_process_job(self.proc._handle)
            except BaseException:
                self.proc.terminate()
                self.proc.wait(4)
                for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                    stream.close()
                raise
        self.read_thread = threading.Thread(
            target=self._read, name="codex-rpc", daemon=True
        )
        self.read_thread.start()
        self.error_thread = threading.Thread(
            target=self._drain_stderr, name="codex-stderr", daemon=True
        )
        self.error_thread.start()
        try:
            self.initialized = self.call(
                "initialize",
                {
                    "clientInfo": {
                        "name": "codex_control_mcp",
                        "title": "Codex-Control-MCP",
                        "version": __version__,
                    },
                    "capabilities": {"experimentalApi": cfg.experimental},
                },
                timeout=20,
            )
            self._write({"method": "initialized"})
        except Exception:
            self.close()
            raise
        audit.emit(
            "appserver_start",
            pid=self.proc.pid,
            generation=self.generation,
            version=runtime.cli_version,
            sandbox="dangerFullAccess",
            parent_is_admin=is_admin(),
        )

    @property
    def alive(self):
        return not self.closed and not self.broken and self.proc.poll() is None

    def _write(self, msg):
        raw = (
            json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        try:
            with self.write_lock:
                if self.closed:
                    raise OSError("connection closed")
                self.proc.stdin.write(raw)
                self.proc.stdin.flush()
        except (OSError, ValueError) as e:
            raise BridgeError(
                "execution_state_unknown",
                "Official connection lost; operations are not replayed.",
            ) from e

    def begin(self, method, params):
        self.schema.validate(method, params)
        with self.lock:
            if self.closed or self.broken or self.proc.poll() is not None:
                raise BridgeError(
                    "execution_state_unknown",
                    "Official connection is unavailable; operations are not replayed.",
                )
            if len(self.pending) >= 256:
                raise BridgeError(
                    "resource_limit",
                    "Official connection has too many outstanding requests.",
                )
            self.next_id += 1
            seq = self.next_id
            future = concurrent.futures.Future()
            self.pending[seq] = (method, future)
            try:
                self._write({"id": seq, "method": method, "params": params})
                self.audit.emit(
                    "rpc_send",
                    method=method,
                    request_id=seq,
                    param_keys=sorted(params) if isinstance(params, dict) else [],
                    generation=self.generation,
                )
            except Exception:
                self.pending.pop(seq, None)
                raise
        return future

    @staticmethod
    def validate_frame(msg):
        if not isinstance(msg, dict) or ("jsonrpc" in msg and msg["jsonrpc"] != "2.0"):
            raise ValueError("Invalid protocol envelope")
        if "id" in msg and type(msg["id"]) not in (int, str):
            raise ValueError("Invalid protocol request ID")
        if "method" in msg:
            if not isinstance(msg["method"], str) or not msg["method"]:
                raise ValueError("Invalid protocol method")
            if "result" in msg or "error" in msg:
                raise ValueError("Ambiguous protocol envelope")
            if (
                "params" in msg
                and msg["params"] is not None
                and not isinstance(msg["params"], (dict, list))
            ):
                raise ValueError("Invalid protocol parameters")
        else:
            if "id" not in msg or ("result" in msg) == ("error" in msg):
                raise ValueError("Invalid protocol response")
            if "error" in msg:
                error = msg["error"]
                if (
                    not isinstance(error, dict)
                    or type(error.get("code")) is not int
                    or not isinstance(error.get("message"), str)
                ):
                    raise ValueError("Invalid protocol error")

    def call(self, method, params=None, timeout=30):
        future = self.begin(method, params)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError as e:
            raise BridgeError(
                "execution_state_unknown",
                "Timed out waiting for the official response; the operation may have executed.",
                details={"method": method},
            ) from e

    def _read(self):
        reason = "official App Server disconnected"
        try:
            while True:
                line = self.proc.stdout.readline(32 * 1024 * 1024 + 1)
                if not line:
                    break
                if len(line) > 32 * 1024 * 1024:
                    reason = "official response exceeded frame limit"
                    break
                try:
                    msg = json.loads(line)
                    self.validate_frame(msg)
                except ValueError:
                    reason = "invalid official protocol frame"
                    break
                if "id" in msg and "method" not in msg:
                    with self.lock:
                        item = self.pending.pop(msg["id"], None)
                    if not item:
                        continue
                    method, f = item
                    if f.done():
                        continue
                    if "error" in msg:
                        e = msg["error"]
                        self.audit.emit(
                            "rpc_error",
                            method=method,
                            code=e.get("code"),
                            generation=self.generation,
                        )
                        f.set_exception(
                            BridgeError(
                                "backend_error",
                                f"Official {method} failed (RPC code {e.get('code')}).",
                                details={
                                    "rpc_code": e.get("code"),
                                    "message_hash": digest(e.get("message", "")),
                                },
                            )
                        )
                    else:
                        try:
                            self.schema.validate_response(method, msg.get("result"))
                        except Exception as e:
                            f.set_exception(
                                e
                                if isinstance(e, BridgeError)
                                else BridgeError(
                                    "version_incompatible",
                                    "Official response validation failed.",
                                )
                            )
                        else:
                            f.set_result(msg.get("result"))
                elif "method" in msg:
                    if "id" in msg:
                        self.audit.emit(
                            "unexpected_callback",
                            method=msg["method"],
                            generation=self.generation,
                        )
                        self._write(
                            {
                                "id": msg["id"],
                                "error": {
                                    "code": -32601,
                                    "message": "No model or approval-agent handler is installed",
                                },
                            }
                        )
                    else:
                        try:
                            self.callback(msg)
                        except Exception:
                            self.audit.emit(
                                "notification_handler_error", method=msg.get("method")
                            )
        except (OSError, ValueError, TypeError, AttributeError, RecursionError):
            reason = "official protocol connection failed"
        finally:
            self.broken = True
            with self.lock:
                items = list(self.pending.values())
                self.pending.clear()
            for method, f in items:
                if not f.done():
                    f.set_exception(
                        BridgeError(
                            "execution_state_unknown",
                            reason,
                            details={"method": method},
                        )
                    )
            self.audit.emit("appserver_disconnect", generation=self.generation)

    def _drain_stderr(self):
        try:
            while self.proc.stderr.readline(65536):
                self.stderr_lines += 1
        except (OSError, ValueError):
            pass

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
        try:
            with self.write_lock:
                self.proc.stdin.close()
            try:
                self.proc.wait(4)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(4)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(4)
        finally:
            self.closed = True
            if self.process_job is not None:
                self.process_job.Close()
                self.process_job = None
            self.read_thread.join(timeout=1)
            self.error_thread.join(timeout=1)
            for reader, stream in ((self.read_thread, self.proc.stdout),
                                   (self.error_thread, self.proc.stderr)):
                # Buffered close waits for an active reader's lock. Never let a
                # misbehaving inherited pipe turn a bounded shutdown into a hang.
                if reader.is_alive():
                    continue
                try:
                    stream.close()
                except Exception:
                    pass
            self.audit.emit(
                "appserver_stop", pid=self.proc.pid, generation=self.generation,
                pipe_readers_stopped=not self.read_thread.is_alive() and not self.error_thread.is_alive(),
            )
