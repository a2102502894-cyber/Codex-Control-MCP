from __future__ import annotations
import copy, json, sqlite3, threading, time
from .common import digest
from .errors import BridgeError


class Idempotency:
    RECEIPT_VERSION = 1

    def __init__(self, path):
        self.lock = threading.RLock()
        self.cache = {}
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.db.execute('PRAGMA synchronous=FULL')
        # Serialize additive migration across independently opened connections.
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute(
                'CREATE TABLE IF NOT EXISTS calls(key_hash TEXT PRIMARY KEY,args_hash TEXT NOT NULL,state TEXT NOT NULL,created_at REAL NOT NULL)'
            )
            columns = {r[1] for r in self.db.execute('PRAGMA table_info(calls)')}
            if 'receipt' not in columns:
                self.db.execute('ALTER TABLE calls ADD COLUMN receipt TEXT')

    def reserve(self, key, tool, args):
        if not isinstance(key, str) or not key or len(key) > 200:
            raise BridgeError('invalid_arguments', 'Invalid idempotency key.')
        kh = digest(key)
        ah = digest({'tool': tool, 'args': args})
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute(
                'SELECT args_hash,state,receipt FROM calls WHERE key_hash=?', (kh,)
            ).fetchone()
            if row:
                if row[0] != ah:
                    raise BridgeError('idempotency_conflict', 'Idempotency key was already used with different arguments.')
                if row[1] == 'complete' and row[2] is not None:
                    try:
                        receipt = json.loads(row[2])
                        if type(receipt.get('version')) is not int or receipt['version'] != self.RECEIPT_VERSION or not isinstance(receipt.get('result'), dict):
                            raise ValueError('Unsupported receipt')
                        return copy.deepcopy(receipt['result'])
                    except (ValueError, TypeError, AttributeError):
                        pass
                raise BridgeError('execution_state_unknown', 'Stored execution has no valid receipt; automatic replay is forbidden.')
            self.db.execute(
                'INSERT INTO calls(key_hash,args_hash,state,created_at) VALUES(?,?,?,?)',
                (kh, ah, 'started', time.time()),
            )
        return None

    def finish(self, key, result):
        kh = digest(key)
        receipt = json.dumps({'version': self.RECEIPT_VERSION, 'result': result}, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        with self.lock, self.db:
            changed = self.db.execute(
                "UPDATE calls SET state='complete',receipt=? WHERE key_hash=? AND state='started'", (receipt, kh)
            ).rowcount
            if changed != 1:
                raise BridgeError('execution_state_unknown', 'Reservation cannot be finalized.')
        # Cache is an optimization only; disk is authoritative.
        self.cache[kh] = copy.deepcopy(result)
        if len(self.cache) > 256:
            self.cache.pop(next(iter(self.cache)))

    def close(self):
        with self.lock:
            self.db.close()
