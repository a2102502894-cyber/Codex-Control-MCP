from __future__ import annotations
import contextvars
import base64, ctypes, hashlib, json, os, pathlib, subprocess, tempfile, threading
from datetime import datetime, timezone
from .errors import BridgeError

CURRENT_OPERATION = contextvars.ContextVar("current_operation", default=None)
ELICITATION_FORWARDER = contextvars.ContextVar("elicitation_forwarder", default=None)
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    raw = (
        value
        if isinstance(value, bytes)
        else json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    )
    return hashlib.sha256(raw).hexdigest()


def atomic_json(path, value):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def ps_argv(script):
    exe = str(
        pathlib.Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    return [
        exe,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-EncodedCommand",
        base64.b64encode(script.encode("utf-16-le")).decode(),
    ]


def ps_quote(value):
    return "'" + value.replace("'", "''") + "'"


def run_metadata(argv, *, timeout=15, env=None):
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise BridgeError(
            "runtime_missing", f"Metadata command could not run ({type(e).__name__})."
        ) from e


def is_admin():
    if os.name == "nt":
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return hasattr(os, "geteuid") and os.geteuid() == 0


def absolute_path(value, cwd):
    if not isinstance(value, str) or not value or "\0" in value:
        raise BridgeError(
            "invalid_arguments", "A nonempty path without NUL is required."
        )
    p = pathlib.Path(value).expanduser()
    if not p.is_absolute():
        p = pathlib.Path(cwd) / p
    return str(p.resolve(strict=False))


class Audit:
    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.methods = {}

    def emit(self, event, **fields):
        with self.lock:
            row = {
                "at": utc_now(),
                "event": event,
                "operation_id": CURRENT_OPERATION.get(),
                **fields,
            }
            if event == "rpc_send":
                name = fields.get("method", "?")
                self.methods[name] = self.methods.get(name, 0) + 1
            if self.path.exists() and self.path.stat().st_size > 4 * 1024 * 1024:
                os.replace(self.path, self.path.with_suffix(".previous.jsonl"))
            with self.path.open("a", encoding="utf-8") as f:
                f.write(
                    json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                )


class InstanceLock:
    def __init__(self, path):
        self.path, self.file = path, None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            self.file.close()
            self.file = None
            raise BridgeError(
                "instance_running", "Another bridge owns this state directory."
            ) from e

    def close(self):
        if self.file:
            try:
                self.file.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            finally:
                self.file.close()
                self.file = None
