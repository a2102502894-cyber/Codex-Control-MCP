from __future__ import annotations
import collections, concurrent.futures, json, os, subprocess, threading, time, uuid
from . import __version__
from .common import CREATE_NO_WINDOW, CURRENT_OPERATION, digest, is_admin
from .diagnostics import CURRENT_TRACE
from .errors import BridgeError
from .rpc_writer import PipeWriter

READ_RPC_METHODS = frozenset({"initialize", "fs/readFile", "fs/getMetadata", "fs/readDirectory",
                              "mcpServerStatus/list", "remoteControl/status/read"})


class AppServer:
    def __init__(self, runtime, schema, cfg, env, audit, on_notification=None):
        self.runtime, self.schema, self.cfg, self.audit = runtime, schema, cfg, audit
        self.callback = on_notification or (lambda m: None)
        self.write_lock = threading.Lock()
        self.lock = threading.RLock()
        self.pending = {}
        self.uncertain = {}
        self.abandoned = collections.OrderedDict()
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
        self._observe_after_dispatch(
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

    def _observe_after_dispatch(self, event, *, _trace=None, **fields):
        """A secondary audit error cannot discard an already-sent request/result.

        Request intent is recorded synchronously before dispatch in begin().
        Audit failures remain visible in Audit.observation and the call trace.
        """
        trace = _trace if _trace is not None else CURRENT_TRACE.get()
        try:
            self.audit.emit(event, **fields)
        except Exception:
            if trace is not None:
                trace.audit_failed()

    def _sent(self, method, seq, future):
        trace = getattr(future, "ccm_trace", None)
        if trace is not None:
            trace.rpc_event("rpc_send", method, seq, self.generation)
        self._observe_after_dispatch("rpc_send", _trace=trace, method=method,
                                     request_id=seq, param_keys=getattr(future, "ccm_param_keys", []),
                                     generation=self.generation, operation_id=getattr(future, "ccm_operation_id", None))

    def _write(self, msg):
        raw = (
            json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        with self.lock:
            if self.closed:
                raise BridgeError("execution_state_unknown", "Official connection is closed; no replay is allowed.")
            if not hasattr(self, "writer"):
                self.writer = PipeWriter(self.proc.stdin)
            item = self.pending.get(msg.get("id")) if "method" in msg else None
        future = item[1] if item else None
        def failed(error):
            if future is not None:
                self.abandon(future, error)
            else:
                self._observe_after_dispatch("rpc_notification_write_failed", generation=self.generation)
        sent = (lambda: self._sent(item[0], msg["id"], future)) if item else (lambda: None)
        return self.writer.submit(raw, sent, failed)

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
            if method not in READ_RPC_METHODS and (len(getattr(self, "uncertain", {})) +
                    sum(m not in READ_RPC_METHODS for m, _ in self.pending.values())) >= 256:
                raise BridgeError("resource_limit", "Too many unresolved operations; reads remain available and no operation is replayed.")
            self.next_id += 1
            seq = self.next_id
            future = concurrent.futures.Future()
            future.ccm_operation_id = CURRENT_OPERATION.get()
            future.ccm_request_id = seq
            future.ccm_trace = CURRENT_TRACE.get()
            future.ccm_param_keys = sorted(params) if isinstance(params, dict) else []
            self.pending[seq] = (method, future)
            try:
                try:
                    self.audit.emit(
                        "rpc_dispatch_intent", method=method, request_id=seq,
                        param_keys=sorted(params) if isinstance(params, dict) else [],
                        generation=self.generation,
                    )
                except Exception as exc:
                    raise BridgeError(
                        "audit_unavailable",
                        "Pre-dispatch audit failed; no RPC was sent.",
                        details={"origin": "bridge_runtime", "method": method,
                                 "request_id": seq, "operation_id": future.ccm_operation_id},
                    ) from exc
                future.ccm_write = self._write({"id": seq, "method": method, "params": params})
                # In-memory test transports may dispatch synchronously.
                if future.ccm_write is None:
                    self._sent(method, seq, future)
            except Exception:
                self.pending.pop(seq, None)
                raise
        return future

    def abandon(self, future, error=None):
        error = error or BridgeError("execution_state_unknown", "Timed out waiting for the official response; no replay is allowed.",
                                    details={"origin": "execution_transport"})
        seq = getattr(future, "ccm_request_id", None)
        with self.lock:
            item = self.pending.get(seq)
            if not item or item[1] is not future or future.done():
                return
            method = item[0]
            write = getattr(future, "ccm_write", None)
            phase = self.writer.cancel(write) if write is not None and hasattr(self, "writer") else "unknown"
            phase = error.details.get("dispatch_state", phase)
            self.pending.pop(seq, None)
            if not hasattr(self, "uncertain"):
                self.uncertain = {}
                self.abandoned = collections.OrderedDict()
            record = {"method": method, "request_id": seq,
                      "operation_id": getattr(future, "ccm_operation_id", None)}
            if method not in READ_RPC_METHODS and phase not in {"not_started", "cancelled"}:
                self.uncertain[seq] = record
            self.abandoned[seq] = record
            while len(self.abandoned) > 256:
                self.abandoned.popitem(last=False)
            error.details.update(record, runtime_generation=self.generation, dispatch_state=phase)
            future.set_exception(error)
        self._observe_after_dispatch("rpc_wait_abandoned", method=method, request_id=seq,
                                     dispatch_state=phase, generation=self.generation)

    def wait(self, future, timeout):
        try:
            return future.result(max(0, timeout))
        except concurrent.futures.TimeoutError:
            self.abandon(future)
            # A response may have won the race immediately before abandonment.
            if future.done():
                return future.result()
            raise BridgeError("execution_state_unknown", "Official response deadline expired; no replay is allowed.",
                              details={"origin": "execution_transport", "method": self.pending.get(getattr(future, "ccm_request_id", None), (None,))[0]})

    def observation(self):
        with self.lock:
            result = {"pending_requests": len(self.pending),
                      "unresolved_operations": len(getattr(self, "uncertain", {})),
                      "abandoned_history_limit": 256}
        if hasattr(self, "writer"):
            result.update(self.writer.observation())
        return result

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
        deadline = time.monotonic() + timeout
        future = self.begin(method, params)
        return self.wait(future, deadline - time.monotonic())

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
                        late = getattr(self, "uncertain", {}).pop(msg["id"], None)
                        late = getattr(self, "abandoned", {}).pop(msg["id"], None) or late
                    if not item:
                        if late:
                            self._observe_after_dispatch("rpc_late_response", method=late["method"],
                                                         request_id=msg["id"], generation=self.generation,
                                                         operation_id=late["operation_id"], response_kind="error" if "error" in msg else "result")
                        continue
                    method, f = item
                    operation_id = getattr(f, "ccm_operation_id", None)
                    trace = getattr(f, "ccm_trace", None)
                    if f.done():
                        continue
                    if "error" in msg:
                        e = msg["error"]
                        self._observe_after_dispatch(
                            "rpc_error", _trace=trace,
                            method=method,
                            code=e.get("code"),
                            operation_id=operation_id, request_id=msg["id"],
                            generation=self.generation,
                        )
                        if trace is not None:
                            trace.rpc_event("rpc_error", method, msg["id"], self.generation, code=e.get("code"))
                        f.set_exception(
                            BridgeError(
                                "backend_error",
                                f"Official {method} failed (RPC code {e.get('code')}).",
                                details={
                                    "rpc_code": e.get("code"),
                                    "message_hash": digest(e.get("message", "")),
                                    "origin": "codex_app_server",
                                    "operation_id": operation_id,
                                    "request_id": msg["id"],
                                    "runtime_generation": self.generation,
                                },
                            )
                        )
                    else:
                        self._observe_after_dispatch("rpc_result", _trace=trace, method=method, request_id=msg["id"],
                                        operation_id=operation_id, generation=self.generation)
                        if trace is not None:
                            trace.rpc_event("rpc_result", method, msg["id"], self.generation)
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
                        self._observe_after_dispatch(
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
                            self._observe_after_dispatch(
                                "notification_handler_error", method=msg.get("method")
                            )
        except (OSError, ValueError, TypeError, AttributeError, RecursionError):
            reason = "official protocol connection failed"
        finally:
            self.broken = True
            with self.lock:
                items = list(self.pending.values())
                self.pending.clear()
            if hasattr(self, "writer"):
                self.writer.stop()
            for method, f in items:
                if not f.done():
                    f.set_exception(
                        BridgeError(
                            "execution_state_unknown",
                            reason,
                            details={"method": method, "origin": "execution_transport",
                                     "operation_id": getattr(f, "ccm_operation_id", None),
                                     "request_id": getattr(f, "ccm_request_id", None),
                                     "runtime_generation": self.generation},
                        )
                    )
            self._observe_after_dispatch("appserver_disconnect", generation=self.generation)

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
        input_closer = None
        try:
            if hasattr(self, "writer"):
                self.writer.stop()
                input_closer = self.writer.close_stream_when_drained()
            else:
                self.proc.stdin.close()
                input_closer = None
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
            if input_closer is not None:
                input_closer.join(timeout=1)
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
            self._observe_after_dispatch(
                "appserver_stop", pid=self.proc.pid, generation=self.generation,
                pipe_readers_stopped=not self.read_thread.is_alive() and not self.error_thread.is_alive(),
            )
