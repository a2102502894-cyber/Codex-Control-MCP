from __future__ import annotations
import copy, sqlite3, threading, time
from .common import digest
from .errors import BridgeError


class Idempotency:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.cache = {}
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS calls(key_hash TEXT PRIMARY KEY,args_hash TEXT NOT NULL,state TEXT NOT NULL,created_at REAL NOT NULL)"
        )
        self.db.commit()

    def reserve(self, key, tool, args):
        if not isinstance(key, str) or not key or len(key) > 200:
            raise BridgeError("invalid_arguments", "Invalid idempotency key.")
        kh = digest(key)
        ah = digest({"tool": tool, "args": args})
        with self.lock:
            row = self.db.execute(
                "SELECT args_hash,state FROM calls WHERE key_hash=?", (kh,)
            ).fetchone()
            if row:
                if row[0] != ah:
                    raise BridgeError(
                        "idempotency_conflict",
                        "Idempotency key was already used with different arguments.",
                    )
                if kh in self.cache:
                    return copy.deepcopy(self.cache[kh])
                raise BridgeError(
                    "execution_state_unknown",
                    "This call was already started or completed in an earlier process. It is not replayed.",
                )
            self.db.execute(
                "INSERT INTO calls VALUES(?,?,?,?)", (kh, ah, "started", time.time())
            )
            self.db.commit()
        return None

    def finish(self, key, result):
        kh = digest(key)
        with self.lock:
            self.db.execute(
                "UPDATE calls SET state=? WHERE key_hash=?", ("complete", kh)
            )
            self.db.commit()
            self.cache[kh] = copy.deepcopy(result)
            if len(self.cache) > 256:
                self.cache.pop(next(iter(self.cache)))

    def close(self):
        self.db.close()
