"""Per-camera RTSP concurrency limiter  [spec:stream.rtsp.limits: max_simultaneous_rtsp_streams = 2].

Wave 2 stub: signatures and docstrings only (see docs/CONTRACT_MEDIA.md).
"""
from __future__ import annotations

from tapo_desktop.core.errors import StreamLimitError  # noqa: F401  (raised by acquire)

__all__ = ["StreamLimiter", "StreamLease"]


class StreamLease:
    """A held stream slot for one host. Context manager; ``release()`` is idempotent."""

    def release(self) -> None:
        """Give the slot back to the limiter. Calling it again is a no-op."""
        raise NotImplementedError

    def __enter__(self) -> "StreamLease":
        raise NotImplementedError

    def __exit__(self, exc_type, exc, tb) -> None:
        raise NotImplementedError


class StreamLimiter:
    """Thread-safe counting limiter keyed by host. Camera firmware allows max 2 concurrent RTSP sessions."""

    def __init__(self, max_per_host: int = 2):
        raise NotImplementedError

    def acquire(self, host: str, timeout: float = 0.0) -> StreamLease:
        """Take one slot for ``host``.

        Blocks up to ``timeout`` seconds waiting for a slot to free up (0 = do not wait).
        Raises :class:`StreamLimitError` when no slot becomes available.
        """
        raise NotImplementedError

    def active(self, host: str) -> int:
        """Number of leases currently held for ``host``."""
        raise NotImplementedError
