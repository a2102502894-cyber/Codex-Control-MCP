from __future__ import annotations
import base64, codecs, collections, copy, dataclasses, json, threading, uuid
from .common import atomic_json, utc_now
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

    def finish(self, future):
        with self.lock:
            try:
                data = future.result()
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

    def metadata(self):
        with self.lock:
            return {
                "session_id": self.id,
                "owner": "trusted_local_owner",
                "backend": "codex_app_server.command_exec",
                "runtime_generation": self.generation,
                "cwd": self.cwd,
                "created_at": self.created_at,
                "state": self.state,
                "exit_code": self.exit_code,
                "stdin_supported": True,
                "pty": self.tty,
                "terminate_supported": True,
                "next_cursor": self.next_cursor,
                "output_truncated": self.truncated,
                "total_output_bytes": self.total_output_bytes,
                "dropped_output_bytes": self.dropped_output_bytes,
                "event_limit": 8192,
                "reconnectable": self.state in ("starting", "running"),
                "reconnect_scope": "same_bridge_and_official_connection_only",
                "restartable": False,
                "error": copy.deepcopy(self.error),
            }

    def read(self, cursor=0, max_bytes=262144):
        with self.lock:
            if type(cursor) is not int or not 0 <= cursor <= self.next_cursor:
                raise BridgeError(
                    "invalid_arguments",
                    "Output cursor is outside the available stream.",
                )
            oldest = self.events[0]["cursor"] if self.events else self.next_cursor
            out = []
            size = 0
            next_cursor = max(cursor, oldest)
            for event in self.events:
                if event["cursor"] < cursor:
                    continue
                if out and size + event["size"] > max_bytes:
                    break
                out.append(dict(event))
                size += event["size"]
                next_cursor = event["cursor"] + 1
            return {
                **self.metadata(),
                "chunks": out,
                "stdout": "".join(x["text"] for x in out if x["stream"] == "stdout"),
                "stderr": "".join(x["text"] for x in out if x["stream"] == "stderr"),
                "next_cursor": next_cursor,
                "oldest_cursor": oldest,
                "cursor_gap": cursor < oldest,
                "has_more": next_cursor < self.next_cursor,
            }


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
            for k in old[: -self.max_sessions]:
                self.items.pop(k, None)
            s = Session("s_" + uuid.uuid4().hex, generation, cwd, self.capacity, tty)
            self.items[s.id] = s
            self.save()
            return s

    def get(self, id, generation=None):
        with self.lock:
            s = self.items.get(id)
        if not s:
            raise BridgeError(
                "session_lost", "No session owned by this bridge has that ID."
            )
        if generation and s.generation != generation:
            raise BridgeError(
                "session_lost", "Session belongs to a previous official connection."
            )
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
            return [s.metadata() for s in self.items.values()] + copy.deepcopy(
                self.previous
            )
