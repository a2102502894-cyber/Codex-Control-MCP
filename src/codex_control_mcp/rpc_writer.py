"""One bounded writer; a blocked pipe never blocks the request/reader threads."""
from __future__ import annotations

import collections
import threading
from dataclasses import dataclass, field

from .errors import BridgeError


@dataclass(eq=False)
class Write:
    raw: bytes
    sent: object
    failed: object
    phase: str = "queued"
    expired: bool = False
    timer: object = None


class PipeWriter:
    def __init__(self, stream, *, timeout=5.0, capacity=16777216):
        self.stream, self.timeout, self.capacity = stream, timeout, capacity
        self.condition = threading.Condition()
        self.queue = collections.deque()
        self.active = None
        self.bytes = 0
        self.closed = False
        self.thread = threading.Thread(target=self._run, name="codex-rpc-writer", daemon=True)
        self.thread.start()

    def submit(self, raw, sent, failed):
        with self.condition:
            if self.closed or (self.active and self.active.expired):
                raise BridgeError("execution_state_unknown", "Official write channel is unavailable; no replay is allowed.",
                                  details={"origin": "execution_transport", "dispatch_state": "not_started"})
            if self.bytes + len(raw) > self.capacity or len(self.queue) >= 256:
                raise BridgeError("resource_limit", "Official dispatch queue is full; no request was sent.")
            item = Write(raw, sent, failed)
            item.timer = threading.Timer(self.timeout, self._expire, args=(item,))
            item.timer.daemon = True
            self.queue.append(item)
            self.bytes += len(raw)
            item.timer.start()
            self.condition.notify()
            return item

    def cancel(self, item):
        """Only an undispatched queue item can be cancelled with certainty."""
        with self.condition:
            if item.phase == "queued":
                self.queue.remove(item)
                self.bytes -= len(item.raw)
                item.phase = "cancelled"
                item.expired = True
                item.timer.cancel()
                item.raw = b""
                return "not_started"
            return item.phase

    def _expire(self, item):
        with self.condition:
            if item.phase not in {"queued", "writing"} or item.expired:
                return
            phase = self.cancel(item) if item.phase == "queued" else "writing"
            item.expired = True
        item.failed(BridgeError("execution_state_unknown", "Official dispatch exceeded its deadline; no replay is allowed.",
                                details={"origin": "execution_transport", "dispatch_state": phase}))

    def _run(self):
        while True:
            with self.condition:
                while not self.queue and not self.closed:
                    self.condition.wait()
                if self.closed and not self.queue:
                    break
                item = self.queue.popleft()
                item.phase = "writing"
                self.active = item
            error = None
            try:
                item.sent()
                view = memoryview(item.raw)
                while view:
                    count = self.stream.write(view)
                    if not count:
                        raise OSError("write channel made no progress")
                    view = view[count:]
                self.stream.flush()
            except Exception:
                error = BridgeError("execution_state_unknown", "Official write channel failed; no replay is allowed.",
                                    details={"origin": "execution_transport", "dispatch_state": "writing"})
            with self.condition:
                self.bytes -= len(item.raw)
                item.raw = b""
                item.phase = "failed" if error else "written"
                expired = item.expired
                item.timer.cancel()
                self.active = None
            if error and not expired:
                item.failed(error)

    def observation(self):
        with self.condition:
            return {"queued_writes": len(self.queue), "queued_bytes": self.bytes,
                    "write_stalled": bool(self.active and self.active.expired),
                    "dispatch_deadline_ms": round(self.timeout * 1000)}

    def stop(self):
        with self.condition:
            self.closed = True
            queued = list(self.queue)
            for item in queued:
                self.cancel(item)
            if self.active:
                self.active.timer.cancel()
            self.condition.notify_all()
        for item in queued:
            item.failed(BridgeError("execution_state_unknown", "Official connection closed before dispatch.",
                                   details={"origin": "execution_transport", "dispatch_state": "not_started"}))

    def close_stream_when_drained(self):
        # A buffered pipe's close acquires its internal write lock. Wait off the
        # shutdown thread, so an inherited pipe cannot hang service shutdown.
        def finish():
            self.thread.join()
            try:
                self.stream.close()
            except (OSError, ValueError):
                pass
        thread = threading.Thread(target=finish, name="codex-rpc-input-close", daemon=True)
        thread.start()
        return thread
