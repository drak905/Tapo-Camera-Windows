"""Per-camera RTSP concurrency limiter  [spec:stream.rtsp.limits: max_simultaneous_rtsp_streams = 2]."""
from __future__ import annotations

import threading
import time

from tapo_desktop.core.errors import StreamLimitError

__all__ = ["StreamLimiter", "StreamLease"]


class StreamLease:
    """A held stream slot for one host. Context manager; ``release()`` is idempotent."""

    __slots__ = ("_limiter", "_host", "_released", "_lock")

    def __init__(self, limiter: "StreamLimiter", host: str):
        self._limiter = limiter
        self._host = host
        self._released = False
        self._lock = threading.Lock()

    @property
    def host(self) -> str:
        """The limiter key this lease counts against."""
        return self._host

    def release(self) -> None:
        """Give the slot back to the limiter. Calling it again is a no-op."""
        with self._lock:
            if self._released:
                return
            self._released = True
        self._limiter._release(self._host)

    def __enter__(self) -> "StreamLease":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    def __repr__(self) -> str:
        return f"StreamLease(host={self._host!r}, released={self._released})"


class StreamLimiter:
    """Thread-safe counting limiter keyed by host. Camera firmware allows max 2 concurrent RTSP sessions."""

    def __init__(self, max_per_host: int = 2):
        if max_per_host < 1:
            raise ValueError("max_per_host must be >= 1")
        self._max = int(max_per_host)
        self._active: dict[str, int] = {}
        self._cond = threading.Condition(threading.Lock())

    @property
    def max_per_host(self) -> int:
        """Configured cap per host."""
        return self._max

    def acquire(self, host: str, timeout: float = 0.0) -> StreamLease:
        """Take one slot for ``host``.

        Blocks up to ``timeout`` seconds waiting for a slot to free up (0 = do not wait).
        Raises :class:`StreamLimitError` when no slot becomes available.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cond:
            while self._active.get(host, 0) >= self._max:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise StreamLimitError(
                        f"stream limit reached for {host}: {self._max} concurrent stream(s) already open"
                    )
                self._cond.wait(remaining)
            self._active[host] = self._active.get(host, 0) + 1
        return StreamLease(self, host)

    def active(self, host: str) -> int:
        """Number of leases currently held for ``host``."""
        with self._cond:
            return self._active.get(host, 0)

    def _release(self, host: str) -> None:
        with self._cond:
            count = self._active.get(host, 0) - 1
            if count > 0:
                self._active[host] = count
            else:
                self._active.pop(host, None)
            self._cond.notify_all()
