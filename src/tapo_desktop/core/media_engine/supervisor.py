"""ffmpeg-based RTSP stream supervisor: decode, reconnect, stall detection, concurrency lease.

Wave 2 stub: signatures and docstrings only (see docs/CONTRACT_MEDIA.md).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Callable

from tapo_desktop.core.media_engine.job_object import JobObject
from tapo_desktop.core.media_engine.limiter import StreamLimiter

__all__ = ["StreamState", "Frame", "StreamSupervisor"]


class StreamState(enum.Enum):
    IDLE = "idle"
    CONNECTING = "connecting"
    PLAYING = "playing"
    RECONNECTING = "reconnecting"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True)
class Frame:
    """One decoded video frame, rgb24, ``len(data) == width * height * 3``."""

    width: int
    height: int
    data: bytes
    index: int
    timestamp: float  # time.monotonic() when the frame was fully read


class StreamSupervisor:
    """Owns one ffmpeg child that decodes ``url`` to rgb24 frames of exactly ``width`` x ``height``.

    Behaviour (binding, see docs/CONTRACT_MEDIA.md):
    * ``start()`` is non-blocking; state goes IDLE -> CONNECTING -> PLAYING (first frame).
    * ffmpeg exit or no frame for ``stall_timeout`` s -> RECONNECTING, retry with exponential
      backoff (``backoff_initial`` .. ``backoff_max``), reset after a successful frame; while the
      stream was recently playing the backoff stays small so the first frame after the camera is
      reachable again arrives within 3 s. ``max_retries`` consecutive failures -> FAILED.
    * Holds a ``limiter`` lease (key ``limiter_key`` or the URL host) while running; if acquire
      fails -> FAILED with a reason mentioning the limit, without spawning ffmpeg.
    * ``hwaccel="auto"``: try d3d11va, fall back to software decoding when the hardware path fails
      before the first frame. Any other explicit hwaccel value that fails also falls back to software.
      ``stats()["decoder"]`` reports the decoder actually in use: ``"d3d11va"``/``<hwaccel>`` or ``"software"``.
    * Every ffmpeg child is assigned to ``job`` (``default_job()`` when None).
    * ``on_state(state, reason)`` is called on every transition; reasons/logs never contain the password.
    * ``on_audio`` receives s16le 8 kHz mono chunks (even length) when given.
    """

    def __init__(
        self,
        url: str,
        width: int = 640,
        height: int = 360,
        *,
        on_frame: Callable[[Frame], None],
        on_state: Callable[[StreamState, str | None], None] | None = None,
        on_audio: Callable[[bytes], None] | None = None,
        ffmpeg_path: str | None = None,
        hwaccel: str = "auto",
        limiter: StreamLimiter | None = None,
        limiter_key: str | None = None,
        job: JobObject | None = None,
        backoff_initial: float = 0.5,
        backoff_max: float = 10.0,
        stall_timeout: float = 5.0,
        max_retries: int | None = None,
    ):
        raise NotImplementedError

    def start(self) -> None:
        """Start the worker thread (non-blocking)."""
        raise NotImplementedError

    def stop(self, timeout: float = 3.0) -> None:
        """Kill ffmpeg, join threads, release the lease; state -> STOPPED. Idempotent."""
        raise NotImplementedError

    def switch(self, url: str, width: int, height: int) -> None:
        """Switch to another URL / output size (e.g. stream2 grid -> stream1 fullscreen) keeping the lease."""
        raise NotImplementedError

    @property
    def state(self) -> StreamState:
        raise NotImplementedError

    @property
    def pid(self) -> int | None:
        """PID of the current ffmpeg child, None when none is running."""
        raise NotImplementedError

    def stats(self) -> dict:
        """``{"frames": int, "reconnects": int, "fps": float, "last_frame_age": float | None, "decoder": str}``."""
        raise NotImplementedError
