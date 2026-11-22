"""Kami Home camera service (R2-0 stub): every action says "not supported for Kami cameras".

The capability flags tell the UI what to disable, so no widget needs a vendor check. The live-view lane
turns ``supports_live_view`` on and replaces ``start_stream``; the other flags stay False until a lane adds them.
Nothing here touches the network or any Tapo core module.
"""
from __future__ import annotations

import pathlib
from collections.abc import Callable
from datetime import date
from typing import Any

from tapo_desktop.core.errors import TapoError
from tapo_desktop.models import (
    CameraConfig, DayNightMode, DeviceInfo, MotionEvent, Preset, RecordingClip, StreamQuality,
)
from tapo_desktop.services.camera_service import CameraService

KAMI_UNSUPPORTED = "Not supported for Kami cameras yet"


class KamiUnsupportedError(TapoError):
    """The requested action does not exist for Kami cameras (yet)."""

    def __init__(self, what: str = ""):
        super().__init__(f"{what}: {KAMI_UNSUPPORTED}" if what else KAMI_UNSUPPORTED)


def _no(what: str):
    raise KamiUnsupportedError(what)


class KamiCameraService(CameraService):
    supports_live_view = False
    supports_ptz = False
    supports_controls = False  # privacy, day/night, LED, alarm, motion detection, presets
    supports_playback = False  # recordings, downloads, playback
    supports_events = False  # motion events (never ONVIF)
    supports_rename = False  # renaming on the camera; the controller keeps a local name instead

    def __init__(self, config: CameraConfig, vault: Any = None):
        self.config = config
        self._vault = vault

    def __repr__(self) -> str:
        return f"KamiCameraService(id={self.config.id!r})"

    def connect(self) -> DeviceInfo:
        return DeviceInfo(model=self.config.model, alias=self.config.name, firmware="", device_id=self.config.device_id)

    def start_stream(self, quality: StreamQuality, width: int, height: int, on_frame, on_state) -> None:
        _no("Live view")

    def stop_stream(self) -> None:
        pass

    def ptz_move(self, dx: int, dy: int) -> None:
        _no("Move")

    def ptz_stop(self) -> None:
        _no("Move")

    def get_presets(self) -> list[Preset]:
        _no("Presets")

    def goto_preset(self, preset_id: str) -> None:
        _no("Presets")

    def get_status(self) -> dict:
        _no("Camera status")

    def set_privacy(self, on: bool) -> None:
        _no("Privacy mode")

    def set_day_night(self, mode: DayNightMode) -> None:
        _no("Day/night mode")

    def set_led(self, on: bool) -> None:
        _no("Status LED")

    def set_alarm(self, on: bool) -> None:
        _no("Alarm")

    def set_motion_detection(self, on: bool) -> None:
        _no("Motion detection")

    def days_with_recordings(self, start: date, end: date) -> list[date]:
        _no("Recordings")

    def recordings_for_day(self, day: date) -> list[RecordingClip]:
        _no("Recordings")

    def download_recording(self, clip: RecordingClip, out_path: pathlib.Path,
                           progress: Callable[[float], None] | None = None) -> pathlib.Path:
        _no("Downloads")

    def start_events(self, on_event: Callable[[MotionEvent], None]) -> None:
        pass  # the controller calls this for every camera with enable_onvif_events; Kami has no ONVIF

    def stop_events(self) -> None:
        pass

    def start_playback(self, start_utc: float, end_utc: float, width: int, height: int, on_frame, on_state,
                       on_position, *, segments: list | None = None) -> None:
        _no("Playback")

    def rename(self, name: str) -> None:
        _no("Renaming on the camera")

    def close(self) -> None:
        pass
