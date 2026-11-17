"""SD-card playback streaming (stub, Phase 5 red)."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from tapo_desktop.core.media_engine.local_stream import LocalStreamSupervisor

__all__ = ["END_OF_RECORDINGS", "PLAYBACK_SPEEDS", "PlaybackStreamSupervisor", "RelayPlaybackStreamSupervisor",
           "build_pause_request", "build_range_playback_request", "build_resume_request"]

END_OF_RECORDINGS = "end of recordings"
PLAYBACK_SPEEDS: dict[str, float] = {"1/1": 1.0, "2/1": 2.0, "4/1": 4.0, "8/1": 8.0}


def build_range_playback_request(start_utc: int, end_utc: int, *, client_id: int = 1, channel: int = 0,
                                 event_types: tuple[int, ...] = (1, 2)) -> dict:
    raise NotImplementedError


def build_pause_request() -> dict:
    raise NotImplementedError


def build_resume_request() -> dict:
    raise NotImplementedError


class PlaybackStreamSupervisor(LocalStreamSupervisor):
    def __init__(self, host: str, password: str, width: int = 640, height: int = 360, *, start_utc: float,
                 end_utc: float, segments: Sequence[Any] = (), on_frame: Callable, on_state: Callable | None = None,
                 on_position: Callable[[float], None] | None = None, **kwargs: Any):
        raise NotImplementedError

    def pause(self) -> None:
        raise NotImplementedError

    def resume(self) -> None:
        raise NotImplementedError

    def seek(self, utc_seconds: float) -> None:
        raise NotImplementedError

    def set_speed(self, scale: str) -> None:
        raise NotImplementedError


class RelayPlaybackStreamSupervisor(PlaybackStreamSupervisor):
    def __init__(self, relay: Any, device_id: str, width: int = 640, height: int = 360, **kwargs: Any):
        raise NotImplementedError
