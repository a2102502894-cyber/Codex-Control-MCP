from __future__ import annotations
import base64, codecs, collections, copy, dataclasses, json, threading, time, uuid
from .common import CURRENT_OPERATION, atomic_json, utc_now
from .errors import BridgeError


@dataclasses.dataclass
class Session:
    id: str
    generation: str
    cwd: str
    capacity: int
    tty: bool = False
    created_at: str = dataclasses.field(default_factory=utc_now)
    state: str = "starting"
    exit_code: int | None = None
    error: dict | None = None
    bytes_cached: int = 0
    next_cursor: int = 0
    truncated: bool = False
    total_output_bytes: int = 0
    dropped_output_bytes: int = 0
    events: collections.deque = dataclasses.field(default_factory=collections.deque)
    future: object = None
    lock: threading.RLock = dataclasses.field(default_factory=threading.RLock)
    decoders: dict = dataclasses.field(default_factory=dict)
    finished: threading.Event = dataclasses.field(default_factory=threading.Event)
    origin_operation_id: str | None = dataclasses.field(default_factory=CURRENT_OPERATION.get)
    timeout_ms: int = 3600000
    started_monotonic: float = dataclasses.field(default_factory=time.monotonic)
    finished_monotonic: float | None = None
    last_output_at: str | None = None
    last_read_at: str | None = None
    last_output_monotonic: float | None = None
    history_only: bool = False
    restored_elapsed_ms: int | None = None
    changed: threading.Condition = dataclasses.field(init=False, repr=False)

    def __post_init__(self):
        self.changed = threading.Condition(self.lock)

    def append(self, params):
        raw = base64.b64decode(params.get("deltaBase64", ""), validate=True)
        if len(raw) > 1024:
            for offset in range(0, len(raw), 1024):
                self.append(
                    {
                        **params,
                        "deltaBase64": base64.b64encode(
                            raw[offset : offset + 1024]
                        ).decode(),
                        "capReached": bool(
                            params.get("capReached") and offset + 1024 >= len(raw)
                        ),
                    }
                )
            return
        if not raw:
            if params.get("capReached"):
                with self.lock:
                    self.truncated = True
            return
        stream = params.get("stream", "stdout")
        with self.lock:
            self.total_output_bytes += len(raw)
            self.last_output_at = utc_now()
            self.last_output_monotonic = time.monotonic()
            decoder = self.decoders.setdefault(
                stream, codecs.getincrementaldecoder("utf-8")("replace")
            )
            text = decoder.decode(raw)
            if len(raw) > self.capacity:
                self.dropped_output_bytes += len(raw) - self.capacity
                raw = raw[-self.capacity :]
                text = raw.decode("utf-8", "replace")
                self.truncated = True
            event = {
                "cursor": self.next_cursor,
                "stream": stream,
                "text": text,
                "data_base64": base64.b64encode(raw).decode(),
                "size": len(raw),
            }
            self.next_cursor += 1
            self.events.append(event)
            self.bytes_cached += len(raw)
            # A byte cap alone permits millions of one-byte Python objects.
            # Bound both raw bytes and per-event allocation overhead.
            while self.events and (
                self.bytes_cached > self.capacity or len(self.events) > 8192
            ):
                removed = self.events.popleft()["size"]
                self.bytes_cached -= removed
                self.dropped_output_bytes += removed
                self.truncated = True
            if params.get("capReached"):
                self.truncated = True
            if self.state == "starting":
                self.state = "running"
            self.changed.notify_all()

    def finish(self, future):
        with self.lock:
            self.finished_monotonic = time.monotonic()
            try:
                data = future.result()
                if not isinstance(data, dict) or type(data.get("exitCode")) is not int:
                    raise BridgeError("execution_state_unknown", "Official execution returned no valid final exit code.")
                self.exit_code = data["exitCode"]
                self.state = "exited"
            except BridgeError as e:
                self.error = e.as_dict()
                self.state = "lost" if e.code == "execution_state_unknown" else "failed"
            except Exception:
                self.error = {
                    "code": "execution_state_unknown",
                    "message": "Session result is unavailable.",
                }
                self.state = "lost"
            for stream, decoder in self.decoders.items():
                tail = decoder.decode(b"", final=True)
                if tail:
                    self.events.append(
                        {
                            "cursor": self.next_cursor,
                            "stream": stream,
                            "text": tail,
                            "data_base64": "",
                            "size": 0,
                        }
                    )
                    self.next_cursor += 1
            self.finished.set()
            self.changed.notify_all()

    def metadata(self):
        with self.lock:
            observed = time.monotonic()
            elapsed = self.restored_elapsed_ms if self.restored_elapsed_ms is not None else round(
                ((self.finished_monotonic or observed) - self.started_monotonic) * 1000)
            return {
                "session_id": self.id,
                "origin_operation_id": self.origin_operation_id,
                "owner": "trusted_local_owner",
                "backend": "codex_app_server.command_exec",
                "runtime_generation": self.generation,
                "cwd": self.cwd,
                "created_at": self.created_at,
                "state": self.state,
                "exit_code": self.exit_code,
                "stdin_supported": not self.history_only,
                "pty": self.tty,
                "terminate_supported": not self.history_only,
                "next_cursor": self.next_cursor,
                "output_truncated": self.truncated,
                "total_output_bytes": self.total_output_bytes,
                "dropped_output_bytes": self.dropped_output_bytes,
                "event_limit": 8192,
                "reconnectable": not self.history_only and self.state in ("starting", "running"),
                "reconnect_scope": "same_bridge_and_official_connection_only",
                "restartable": False,
                "error": copy.deepcopy(self.error),
                "effective_timeout_ms": self.timeout_ms,
                "elapsed_ms": elapsed,
                "last_output_at": self.last_output_at,
                "last_read_at": self.last_read_at,
                "history_only": self.history_only,
                "heartbeat": {
                    "observed_at": utc_now(),
                    "elapsed_ms": elapsed,
                    "output_idle_ms": None if self.history_only else max(0, round(
                        (observed - (self.last_output_monotonic or self.started_monotonic)) * 1000)),
                    "scope": "saved_state_only" if self.history_only else "bridge_session_observation",
                },
                "read_action": {"tool": "session_read", "arguments": {"session_id": self.id, "cursor": 0}},
            }

    def read(self, cursor=0, max_bytes=32768, output_format="legacy", wait_ms=None):
        with self.lock:
            if type(cursor) is not int or not 0 <= cursor <= self.next_cursor:
                raise BridgeError(
                    "invalid_arguments",
                    "Output cursor is outside the available stream.",
                )
            if type(max_bytes) is not int or not 1024 <= max_bytes <= 4194304 or output_format not in {"text", "chunks", "raw", "legacy"}:
                raise BridgeError("invalid_arguments", "Invalid output page options.")
            if wait_ms is not None and (type(wait_ms) is not int or not 0 <= wait_ms <= 10000):
                raise BridgeError("invalid_arguments", "wait_ms must be an integer from 0 to 10000.")
            deadline = time.monotonic() + (wait_ms or 0) / 1000
            while self.state in {"starting", "running"} and not self.error and cursor == self.next_cursor:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                # Release the session lock so output/exit callbacks can wake this reader.
                self.changed.wait(remaining)
            self.last_read_at = utc_now()
            oldest = self.events[0]["cursor"] if self.events else self.next_cursor
            out = []
            size = 0
            next_cursor = max(cursor, oldest)
            for event in self.events:
                if event["cursor"] < cursor:
                    continue
                if out and (size + event["size"] > max_bytes or len(out) >= 1024):
                    break
                out.append(dict(event))
                size += event["size"]
                next_cursor = event["cursor"] + 1
            result = {
                **self.metadata(),
                "next_cursor": next_cursor,
                "oldest_cursor": oldest,
                "cursor_gap": cursor < oldest,
                "has_more": next_cursor < self.next_cursor,
            }
            if output_format in {"text", "legacy"}:
                result.update(stdout="".join(x["text"] for x in out if x["stream"] == "stdout"),
                              stderr="".join(x["text"] for x in out if x["stream"] == "stderr"))
            if output_format != "text":
                keep = {"cursor", "stream", "size"} | ({"data_base64"} if output_format == "raw" else {"text"})
                result["chunks"] = out if output_format == "legacy" else [{k: v for k, v in x.items() if k in keep} for x in out]
            running = self.state in {"starting", "running"}
            known_exit = self.state == "exited" and type(self.exit_code) is int
            unavailable = self.history_only and (self.next_cursor > 0 or self.total_output_bytes > 0)
            output_complete = not (result["has_more"] or self.truncated or unavailable or result["cursor_gap"])
            continuation = running or result["has_more"]
            status = "succeeded" if known_exit and self.exit_code == 0 else "failed" if known_exit or self.state == "failed" else "running" if running else "unknown"
            if self.state == "lost":
                message = "执行结果未知，不能确认进程已结束；请核对原任务和已有日志，不要重发原命令。"
            elif unavailable:
                message = f"已恢复会话状态（退出码 {self.exit_code}）；历史终端输出已不可用，请读取原日志核验，不要重新执行。"
            elif running:
                idle = result["heartbeat"]["output_idle_ms"] or 0
                message = f"执行结果尚未返回，已等待约 {result['elapsed_ms'] / 1000:g} 秒，最近约 {idle / 1000:g} 秒无终端输出；请继续读取同一会话并报告进度。"
            elif result["has_more"]:
                message = f"进程已结束（退出码 {self.exit_code}），仍有输出未读；请继续按游标取完后再交付结果。"
            elif not output_complete:
                message = f"进程已结束（退出码 {self.exit_code}），终端输出存在缺口；请读取原日志核验后再交付。"
            elif not known_exit:
                message = "会话已停止返回结果，请检查错误状态；不能据此宣告任务成功。"
            else:
                message = f"进程已结束（退出码 {self.exit_code}），本会话输出已读完；请立即核对并交回结果，继续用户任务中尚未完成的步骤。"
            result.update(completed=not continuation, process_completed=known_exit,
                          continuation_required=continuation, output_complete=output_complete,
                          final_receipt_ready=known_exit and output_complete, result_status=status,
                          output_format=output_format, page_bytes=size, status_message=message,
                          next_action={"tool": "session_read", "arguments": {"session_id": self.id, "cursor": next_cursor,
                              "max_bytes": max_bytes, "output_format": output_format,
                              "wait_ms": 10000 if wait_ms is None else wait_ms}} if continuation else None)
            if unavailable:
                result["recovery_warning"] = {"code": "session_output_unavailable",
                    "message": "Only terminal metadata was persisted; original output must be checked in the task log."}
            return result


class SessionStore:
    def __init__(self, path, capacity, max_sessions):
        self.path, self.capacity, self.max_sessions = path, capacity, max_sessions
        self.lock = threading.RLock()
        self.items = {}
        self.previous = []
        if path.exists():
            try:
                data = json.loads(path.read_text("utf-8"))
                if not isinstance(data, list):
                    raise ValueError("Session history is not a list")
                records = {
                    x["session_id"]: x
                    for x in data
                    if isinstance(x, dict) and isinstance(x.get("session_id"), str)
                }
                recent = sorted(
                    records.values(), key=lambda x: str(x.get("created_at", ""))
                )[-max_sessions:]
                for item in recent:
                    if item.get("state") in ("starting", "running"):
                        item["state"] = "lost"
                        item["reconnectable"] = False
                        item["error"] = {
                            "code": "session_lost",
                            "message": "Bridge restarted; original session cannot be reattached.",
                        }
                    self.previous.append(item)
            except (OSError, ValueError, TypeError):
                pass

    def create(self, generation, cwd, tty=False):
        with self.lock:
            if (
                sum(s.state in ("starting", "running") for s in self.items.values())
                >= self.max_sessions
            ):
                raise BridgeError("resource_limit", "Concurrent session limit reached.")
            old = [
                k
                for k, s in self.items.items()
                if s.state not in ("starting", "running")
            ]
            before = dict(self.items)
            for k in old[: -self.max_sessions]:
                self.items.pop(k, None)
            s = Session("s_" + uuid.uuid4().hex, generation, cwd, self.capacity, tty)
            self.items[s.id] = s
            try:
                self.save()
            except BaseException:
                self.items = before
                raise
            return s

    def get(self, id, generation=None):
        with self.lock:
            s = self.items.get(id)
            if s is None:
                record = next((x for x in self.previous if x["session_id"] == id), None)
                if record is not None:
                    s = self._restore_metadata(record)
        if not s:
            raise BridgeError(
                "session_lost", "No session owned by this bridge has that ID."
            )
        if generation and (s.history_only or s.generation != generation):
            raise BridgeError(
                "session_lost", "Session belongs to a previous official connection."
            )
        return s

    def _restore_metadata(self, record):
        """Recover an exit receipt, never attach to or replay an old process."""
        s = Session(record["session_id"], record.get("runtime_generation", ""),
                    record.get("cwd", ""), self.capacity, bool(record.get("pty")))
        s.history_only = True
        s.created_at = record.get("created_at", s.created_at)
        s.origin_operation_id = record.get("origin_operation_id")
        s.state = record.get("state", "lost")
        s.exit_code = record.get("exit_code")
        s.error = copy.deepcopy(record.get("error"))
        if s.state not in {"exited", "failed", "lost"} or (s.state == "exited" and type(s.exit_code) is not int):
            s.state, s.exit_code = "lost", None
            s.error = {"code": "session_lost", "message": "History contains no confirmed final execution result."}
        for key in ("next_cursor", "total_output_bytes", "dropped_output_bytes"):
            value = record.get(key, 0)
            setattr(s, key, value if type(value) is int and value >= 0 else 0)
        elapsed = record.get("elapsed_ms", 0)
        s.restored_elapsed_ms = elapsed if type(elapsed) is int and elapsed >= 0 else 0
        s.truncated = bool(record.get("output_truncated"))
        s.timeout_ms = record.get("effective_timeout_ms", 3600000)
        s.last_output_at = record.get("last_output_at")
        s.last_read_at = record.get("last_read_at")
        s.finished.set()
        return s

    def notify(self, message):
        if message.get("method") != "command/exec/outputDelta":
            return
        params = message.get("params", {})
        s = self.items.get(params.get("processId"))
        if s:
            s.append(params)

    def save(self):
        with self.lock:
            records = {x["session_id"]: x for x in self.previous}
            records.update({s.id: s.metadata() for s in self.items.values()})
            # The most recent records must be at the end across every restart.
            ordered = sorted(
                records.values(), key=lambda x: str(x.get("created_at", ""))
            )
            atomic_json(self.path, ordered)

    def list(self):
        with self.lock:
            return [s.metadata() for s in self.items.values()] + [
                self._restore_metadata(record).metadata() for record in self.previous
            ]
