"""View-driven streaming policy (Phase 6; pure Python, no Qt, no I/O).

What the grid shows decides what streams:

* :func:`plan_streams` - every camera in a visible slot streams at the same time, local and remote, in
  every grid size (1x1 .. 5x5): its ``grid_quality`` at the grid decode size. In the single-camera
  (fullscreen) view only that camera streams, in its ``fullscreen_quality``. Empty slots, unknown ids and
  repeated ids never stream.
* :func:`reserved_relay_streams` - every viewed camera is reserved a relay slot in the
  :class:`~tapo_desktop.services.remote.RemoteStreamGate`, so remote tiles in the view never wait on
  "Remote - click to view". The ``max_remote_streams`` setting (default 4) now only limits relay streams
  outside the view. Real measurement (13 cameras, 11 through the relay, 4x4 grid): all streamed; relay
  VGA arrives at ~6-7 fps, LAN at ~15-18 fps.
* :func:`view_size` - decode sizes are fixed per quality (grid SD/VGA 640x360, HD 1280x720), so resizing
  the window never restarts ffmpeg; tiles scale the frame aspect-fit.
* :func:`decoder_for` - software decoding (:data:`SOFTWARE_DECODER`) for grid-size (SD) streams, hardware
  (:data:`HARDWARE_DECODER`: d3d11va with software fallback) for HD ones. Measured per ffmpeg process:
  ~110 MB private with d3d11va vs ~38 MB in software for a VGA stream at a few % CPU, so a 4x4 grid in
  software saves ~1 GB. The decoder is chosen when a stream starts; a grid -> fullscreen switch changes
  the stream in place and keeps it.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from tapo_desktop.models import CameraConfig, StreamQuality

#: ``hwaccel`` value for software decoding (see StreamSupervisor).
SOFTWARE_DECODER = "none"
#: ``hwaccel`` value for hardware decoding with software fallback before the first frame.
HARDWARE_DECODER = "auto"
#: Decoded frame size of grid (SD / VGA) streams.
GRID_DECODE_SIZE: tuple[int, int] = (640, 360)
#: Decoded frame size of HD (fullscreen) streams.
HD_DECODE_SIZE: tuple[int, int] = (1280, 720)

_HD_QUALITIES = frozenset({StreamQuality.HD, StreamQuality.TELE_HD})


@dataclass(frozen=True)
class StreamRequest:
    """One live view request: ``controller.start_view(camera_id, *request.as_args())``."""

    quality: StreamQuality
    width: int
    height: int

    def as_args(self) -> tuple[StreamQuality, int, int]:
        return (self.quality, self.width, self.height)


def _is_hd(quality: StreamQuality | str) -> bool:
    return StreamQuality(quality) in _HD_QUALITIES  # ValueError for an unknown quality


def view_size(quality: StreamQuality | str) -> tuple[int, int]:
    """Decoded frame size for ``quality`` (16:9; the supervisor letterboxes)."""
    return HD_DECODE_SIZE if _is_hd(quality) else GRID_DECODE_SIZE


def decoder_for(quality: StreamQuality | str) -> str:
    """``hwaccel`` for a stream of ``quality``: software for SD (grid tiles), hardware for HD."""
    return HARDWARE_DECODER if _is_hd(quality) else SOFTWARE_DECODER


def _request(quality: StreamQuality) -> StreamRequest:
    quality = StreamQuality(quality)
    return StreamRequest(quality, *view_size(quality))


def plan_streams(visible_slots: Sequence[str | None], configs: Mapping[str, CameraConfig],
                 fullscreen: str | None = None) -> dict[str, StreamRequest]:
    """Streams for a view, in slot order: camera id -> request.

    ``visible_slots`` are the slots of the current grid (camera id or None), ``configs`` the known
    cameras and ``fullscreen`` the camera shown alone (ignored unless it is in a visible slot)."""
    visible = [cid for cid in dict.fromkeys(visible_slots) if cid is not None and cid in configs]
    if fullscreen is not None and fullscreen in visible:
        return {fullscreen: _request(configs[fullscreen].fullscreen_quality)}
    return {cid: _request(configs[cid].grid_quality) for cid in visible}


def reserved_relay_streams(views: Mapping[str, object | None]) -> frozenset[str]:
    """Camera ids that must get a relay slot: every camera with a live view request (``views``: camera
    id -> current request or None)."""
    return frozenset(cid for cid, view in views.items() if view is not None)
