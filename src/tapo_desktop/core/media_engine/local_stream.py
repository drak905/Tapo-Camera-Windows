"""Live view over the camera's local media port (:8800 ``POST /stream``), like the Tapo phone app."""
from __future__ import annotations

from typing import Callable

from tapo_desktop.core.media_engine.job_object import JobObject
from tapo_desktop.core.media_engine.limiter import StreamLimiter
from tapo_desktop.core.media_engine.supervisor import Frame, StreamState

__all__ = ["LocalStreamSupervisor", "build_preview_request", "build_stop_request"]


def build_preview_request(resolution: str, channel: int = 0) -> dict:
    raise NotImplementedError


def build_stop_request() -> dict:
    raise NotImplementedError


class LocalStreamSupervisor:
    """Same public surface as StreamSupervisor, fed by a :8800 preview session."""

    def __init__(
        self,
        host: str,
        password: str,
        width: int = 640,
        height: int = 360,
        *,
        resolution: str = "VGA",
        port: int = 8800,
        channel: int = 0,
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
        raise NotImplementedError

    def stop(self, timeout: float = 3.0) -> None:
        raise NotImplementedError

    def switch(self, resolution: str, width: int, height: int) -> None:
        raise NotImplementedError

    @property
    def state(self) -> StreamState:
        raise NotImplementedError

    @property
    def pid(self) -> int | None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError
