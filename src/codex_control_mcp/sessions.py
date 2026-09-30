from __future__ import annotations
import base64, codecs, collections, copy, dataclasses, json, pathlib, sqlite3, threading, uuid
from .common import CURRENT_OPERATION, utc_now
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

    persist: object = None
    archived: bool = False
    output_availability: str = "available"
    persistence_error: str | None = None

    def _persist(self):
        if self.persist:
            try:
                self.persist(self)
                self.persistence_error = None
            except Exception as exc:
                # Preserve live output/real result; durable last snapshot remains
                # consistent, and will reopen as lost if it was not finalized.
                self.persistence_error = type(exc).__name__

    def _bound(self):
        while self.events and (self.bytes_cached > self.capacity or len(self.events) > 8192):
            removed = self.events.popleft()["size"]
            self.bytes_cached -= removed
            self.dropped_output_bytes += removed
            self.truncated = True

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
                    self._persist()
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
            self._bound()
            if params.get("capReached"):
                self.truncated = True
            if self.state == "starting":
                self.state = "running"
            self._persist()

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
            if self.state == "lost":
                self.exit_code = None
            self._bound()
            self._persist()
            self.finished.set()

    def metadata(self):
        with self.lock:
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
                "stdin_supported": not self.archived,
                "pty": self.tty,
                "terminate_supported": not self.archived,
                "next_cursor": self.next_cursor,
                "output_truncated": self.truncated,
                "total_output_bytes": self.total_output_bytes,
                "dropped_output_bytes": self.dropped_output_bytes,
                "event_limit": 8192,
                "reconnectable": not self.archived and self.state in ("starting", "running"),
                "archived": self.archived,
                "output_availability": self.output_availability,
                "persistence_error": self.persistence_error,
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
    """Bounded per-session snapshots. Lock order: session -> db; never db -> session."""

    def __init__(self, path, capacity, max_sessions):
        self.path, self.capacity, self.max_sessions = pathlib.Path(path), capacity, max_sessions
        self.lock = threading.RLock()
        self.db_lock = threading.RLock()
        self.items = {}
        self.previous = []  # compatibility: history is now readable via get()
        self.closed = False
        self.db = sqlite3.connect(self.path.with_suffix(".sqlite3"), check_same_thread=False)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,doc TEXT NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS session_events(session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,cursor INTEGER NOT NULL,event TEXT NOT NULL,PRIMARY KEY(session_id,cursor))")
            self.db.execute("CREATE TABLE IF NOT EXISTS migrations(name TEXT PRIMARY KEY)")
            self.db.execute("BEGIN IMMEDIATE")
            if not self.db.execute("SELECT 1 FROM migrations WHERE name='json-v1'").fetchone():
                if self.path.exists():
                    data = json.loads(self.path.read_text("utf-8"))
                    if not isinstance(data, list):
                        raise BridgeError("session_state_corrupt", "Legacy session history is not a list.")
                    for meta in data[-max_sessions:]:
                        if isinstance(meta, dict) and isinstance(meta.get("session_id"), str):
                            doc = {"version": 1, "metadata": meta, "events": [], "output_availability": "unavailable"}
                            self.db.execute("INSERT OR IGNORE INTO sessions VALUES(?,?)", (meta["session_id"], json.dumps(doc)))
                self.db.execute("INSERT INTO migrations VALUES('json-v1')")
        for sid, raw in self.db.execute("SELECT id,doc FROM sessions").fetchall():
            try:
                doc = json.loads(raw)
                if doc["version"] not in (1, 2):
                    raise ValueError("Unsupported session version")
                meta = doc["metadata"]
                session = Session(sid, meta["runtime_generation"], meta["cwd"], capacity, meta.get("pty", False))
                for field, key in (("created_at", "created_at"), ("state", "state"), ("exit_code", "exit_code"), ("error", "error"), ("next_cursor", "next_cursor"), ("truncated", "output_truncated"), ("total_output_bytes", "total_output_bytes"), ("dropped_output_bytes", "dropped_output_bytes"), ("origin_operation_id", "origin_operation_id")):
                    if key in meta:
                        setattr(session, field, meta[key])
                stored_events = doc.get("events", []) if doc["version"] == 1 else [json.loads(row[0]) for row in self.db.execute("SELECT event FROM session_events WHERE session_id=? ORDER BY cursor", (sid,)).fetchall()]
                session.events = collections.deque(stored_events)
                session.bytes_cached = sum(x["size"] for x in session.events)
                session.archived = True
                session.output_availability = doc.get("output_availability", "available")
                if session.state in ("starting", "running"):
                    session.state, session.exit_code = "lost", None
                    session.error = {"code": "session_lost", "message": "Bridge restarted; original process is not owned or reattached."}
                    # Persist decoder tail so incomplete UTF-8 is explicitly visible.
                    for stream, encoded in doc.get("decoder_tails", {}).items():
                        tail = base64.b64decode(encoded).decode("utf-8", "replace")
                        if tail:
                            session.events.append({"cursor": session.next_cursor, "stream": stream, "text": tail, "data_base64": "", "size": 0})
                            session.next_cursor += 1
                session._bound()
                session.finished.set()
                session.persist = self._save_session
                self.items[sid] = session
                self._save_session(session)
            except (ValueError, KeyError, TypeError) as exc:
                raise BridgeError("session_state_corrupt", "Stored session snapshot is invalid; no command is replayed.") from exc

    def _save_session(self, session):
        # Caller holds session.lock or session is not published yet.
        doc = {"version": 2, "metadata": session.metadata(),
               "output_availability": session.output_availability,
               "decoder_tails": {k: base64.b64encode(v.getstate()[0]).decode() for k, v in session.decoders.items()}}
        raw = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
        with self.db_lock:
            if self.closed:
                raise BridgeError("session_store_closed", "Session persistence has closed.")
            with self.db:
                previous = self.db.execute("SELECT doc FROM sessions WHERE id=?", (session.id,)).fetchone()
                previous_doc = json.loads(previous[0]) if previous else {}
                persisted_cursor = previous_doc.get("metadata", {}).get("next_cursor", 0) if previous_doc.get("version") == 2 else 0
                self.db.execute("INSERT INTO sessions VALUES(?,?) ON CONFLICT(id) DO UPDATE SET doc=excluded.doc", (session.id, raw))
                self.db.executemany("INSERT OR REPLACE INTO session_events VALUES(?,?,?)", ((session.id, event["cursor"], json.dumps(event, ensure_ascii=False, separators=(",", ":"))) for event in session.events if event["cursor"] >= persisted_cursor))
                oldest = session.events[0]["cursor"] if session.events else session.next_cursor
                self.db.execute("DELETE FROM session_events WHERE session_id=? AND cursor<?", (session.id, oldest))

    def create(self, generation, cwd, tty=False):
        with self.lock:
            if sum(s.state in ("starting", "running") for s in self.items.values()) >= self.max_sessions:
                raise BridgeError("resource_limit", "Concurrent session limit reached.")
            old = sorted((s for s in self.items.values() if s.state not in ("starting", "running") and s.finished.is_set()), key=lambda s: s.created_at)
            for session in old[:max(0, len(self.items) - self.max_sessions + 1)]:
                with self.db_lock, self.db:
                    self.db.execute("DELETE FROM sessions WHERE id=?", (session.id,))
                self.items.pop(session.id, None)
            session = Session("s_" + uuid.uuid4().hex, generation, cwd, self.capacity, tty)
            session.persist = self._save_session
            self._save_session(session)  # must succeed before backend dispatch
            self.items[session.id] = session
            return session

    def get(self, id, generation=None):
        with self.lock:
            session = self.items.get(id)
        if not session:
            raise BridgeError("session_lost", "No session owned by this bridge has that ID.")
        if generation and (session.archived or session.generation != generation):
            raise BridgeError("session_lost", "Session belongs to a previous official connection.")
        return session

    def notify(self, message):
        if message.get("method") != "command/exec/outputDelta":
            return
        params = message.get("params", {})
        with self.lock:
            session = self.items.get(params.get("processId"))
        if session and not session.archived:
            session.append(params)

    def save(self):
        with self.lock:
            sessions = list(self.items.values())
        for session in sessions:
            with session.lock:
                self._save_session(session)

    def list(self):
        with self.lock:
            sessions = list(self.items.values())
        return [session.metadata() for session in sessions]

    def close(self):
        with self.db_lock:
            if self.closed:
                return
        try:
            self.save()
        finally:
            with self.db_lock:
                if not self.closed:
                    self.closed = True
                    self.db.close()
