"""Per-camera service facade over the core modules (contract: docs/CONTRACT_UI.md).

All methods are BLOCKING; :class:`tapo_desktop.services.controller.AppController` runs them on
worker threads. Core types are imported lazily / under TYPE_CHECKING so this module imports even
while the core lanes are still stubs.

Status dict returned by :meth:`CameraService.get_status`::

    {"privacy": bool, "day_night": DayNightMode, "led": bool, "motion_detection": bool, "alarm": bool}

PTZ convention (shared by the whole UI lane): ``dx > 0`` = pan right, ``dx < 0`` = pan left,
``dy > 0`` = tilt up, ``dy < 0`` = tilt down; ``(0, 0)`` is not a valid move (use ``ptz_stop``).
"""
from __future__ import annotations

import abc
import pathlib
from collections.abc import Callable
from datetime import date
from typing import TYPE_CHECKING

from tapo_desktop.models import (
    CameraConfig,
    DayNightMode,
    DeviceInfo,
    MotionEvent,
    Preset,
    RecordingClip,
    StreamQuality,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tapo_desktop.core.auth.vault import CredentialVault
    from tapo_desktop.core.control_client.transport import PinStore
    from tapo_desktop.core.media_engine.limiter import StreamLimiter
    from tapo_desktop.core.media_engine.supervisor import Frame, StreamState

STATUS_KEYS: tuple[str, ...] = ("privacy", "day_night", "led", "motion_detection", "alarm")


class CameraService(abc.ABC):
    """Blocking per-camera API. Errors are raised as ``tapo_desktop.core.errors.TapoError``."""

    config: CameraConfig

    @abc.abstractmethod
    def connect(self) -> DeviceInfo:
        """Log in (control client) and return device info. Idempotent."""
        raise NotImplementedError

    @abc.abstractmethod
    def start_stream(self, quality: StreamQuality, width: int, height: int,
                     on_frame: Callable[[Frame], None],
                     on_state: Callable[[StreamState, str | None], None]) -> None:
        """Start (or switch) the live stream; callbacks fire on a worker thread. Non-blocking
        after the stream supervisor has been started."""
        raise NotImplementedError

    @abc.abstractmethod
    def stop_stream(self) -> None:
        """Stop the live stream (idempotent)."""
        raise NotImplementedError

    @abc.abstractmethod
    def ptz_move(self, dx: int, dy: int) -> None:
        """Continuous move; see module docstring for the sign convention."""
        raise NotImplementedError

    @abc.abstractmethod
    def ptz_stop(self) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def get_presets(self) -> list[Preset]:
        raise NotImplementedError

    @abc.abstractmethod
    def goto_preset(self, preset_id: str) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def get_status(self) -> dict:
        """Return the status dict described in the module docstring (all 5 keys)."""
        raise NotImplementedError

    @abc.abstractmethod
    def set_privacy(self, on: bool) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def set_day_night(self, mode: DayNightMode) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def set_led(self, on: bool) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def set_alarm(self, on: bool) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def set_motion_detection(self, on: bool) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def days_with_recordings(self, start: date, end: date) -> list[date]:
        raise NotImplementedError

    @abc.abstractmethod
    def recordings_for_day(self, day: date) -> list[RecordingClip]:
        raise NotImplementedError

    @abc.abstractmethod
    def download_recording(self, clip: RecordingClip, out_path: pathlib.Path,
                           progress: Callable[[float], None] | None = None) -> pathlib.Path:
        raise NotImplementedError

    @abc.abstractmethod
    def start_events(self, on_event: Callable[[MotionEvent], None]) -> None:
        """Start the ONVIF pull-point listener; ``on_event`` fires on a worker thread."""
        raise NotImplementedError

    @abc.abstractmethod
    def stop_events(self) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def close(self) -> None:
        """Stop stream + events and release every network resource. Idempotent."""
        raise NotImplementedError


class TapoCameraService(CameraService):
    """Real wiring: TapoControlClient (HTTPS :443, cloud password, user "admin"), StreamSupervisor
    (RTSP :554, camera account), OnvifClient/PullPoint (:2020, camera account) and
    RecordingDownloader (:8800, cloud password).

    Credentials are read from ``vault.get(config.id, CredentialKind.CLOUD | CAMERA_ACCOUNT)``.
    Construction performs no network I/O.
    """

    def __init__(self, config: CameraConfig, vault: CredentialVault, *,
                 limiter: StreamLimiter | None = None, pin_store: PinStore | None = None):
        raise NotImplementedError

    def connect(self) -> DeviceInfo:
        raise NotImplementedError

    def start_stream(self, quality, width, height, on_frame, on_state) -> None:
        raise NotImplementedError

    def stop_stream(self) -> None:
        raise NotImplementedError

    def ptz_move(self, dx: int, dy: int) -> None:
        raise NotImplementedError

    def ptz_stop(self) -> None:
        raise NotImplementedError

    def get_presets(self) -> list[Preset]:
        raise NotImplementedError

    def goto_preset(self, preset_id: str) -> None:
        raise NotImplementedError

    def get_status(self) -> dict:
        raise NotImplementedError

    def set_privacy(self, on: bool) -> None:
        raise NotImplementedError

    def set_day_night(self, mode: DayNightMode) -> None:
        raise NotImplementedError

    def set_led(self, on: bool) -> None:
        raise NotImplementedError

    def set_alarm(self, on: bool) -> None:
        raise NotImplementedError

    def set_motion_detection(self, on: bool) -> None:
        raise NotImplementedError

    def days_with_recordings(self, start: date, end: date) -> list[date]:
        raise NotImplementedError

    def recordings_for_day(self, day: date) -> list[RecordingClip]:
        raise NotImplementedError

    def download_recording(self, clip, out_path, progress=None) -> pathlib.Path:
        raise NotImplementedError

    def start_events(self, on_event) -> None:
        raise NotImplementedError

    def stop_events(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError
