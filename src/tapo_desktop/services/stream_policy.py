"""View-driven streaming (Phase 6; pure Python, no Qt). Stub: implemented in the green step."""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from tapo_desktop.models import CameraConfig, StreamQuality

SOFTWARE_DECODER = "none"
HARDWARE_DECODER = "auto"
GRID_DECODE_SIZE: tuple[int, int] = (640, 360)
HD_DECODE_SIZE: tuple[int, int] = (1280, 720)


@dataclass(frozen=True)
class StreamRequest:
    quality: StreamQuality
    width: int
    height: int

    def as_args(self) -> tuple[StreamQuality, int, int]:
        raise NotImplementedError


def view_size(quality: StreamQuality) -> tuple[int, int]:
    raise NotImplementedError


def decoder_for(quality: StreamQuality) -> str:
    raise NotImplementedError


def plan_streams(visible_slots: Sequence[str | None], configs: Mapping[str, CameraConfig],
                 fullscreen: str | None = None) -> dict[str, StreamRequest]:
    raise NotImplementedError


def reserved_relay_streams(views: Mapping[str, object | None]) -> frozenset[str]:
    raise NotImplementedError


__all__ = ["GRID_DECODE_SIZE", "HARDWARE_DECODER", "HD_DECODE_SIZE", "SOFTWARE_DECODER", "StreamRequest",
           "decoder_for", "plan_streams", "reserved_relay_streams", "view_size", "Iterable"]
