"""AppController: the only object the UI talks to (contract: docs/CONTRACT_UI.md).

Behaviour (binding for Wave 3; the lane-tdd-ui tests check all of it):

* Cameras are loaded from ``config_store.load()`` in ``__init__``. One :class:`CameraService` per
  camera, created lazily with ``service_factory(config)`` on first use (default factory:
  ``TapoCameraService(config, vault, limiter=<shared StreamLimiter>, pin_store=<PinStore in data dir>)``,
  imported lazily). Construction does no network I/O.
* Every service call runs OFF the GUI thread (worker threads / pool). Calls for one camera are
  executed in submission order (so ``ptz_move`` then ``ptz_stop`` reach the camera in that order).
  Before the first call on a service the controller calls ``service.connect()`` once; after the
  first successful connect it calls ``service.start_events(...)`` if ``config.enable_onvif_events``.
* All signals are emitted on the GUI thread (marshal from workers; a DirectConnection slot must
  observe the GUI thread).
* ``frame_ready`` carries a deep-copied ``QImage`` (Format_RGB888) of exactly
  ``frame.width x frame.height`` built from the rgb24 bytes. Frames may be coalesced (latest wins).
* ``stream_state`` carries ``StreamState.name`` and the reason (``""`` if None). If ``connect()``
  fails during ``start_view`` the controller emits ``stream_state(id, "FAILED", str(exc))``.
* Error reporting: a failing operation emits ``command_failed(camera_id, action, message)`` where
  ``action`` is the controller method name (``"set_privacy"``, ``"set_day_night"``, ``"set_led"``,
  ``"set_alarm"``, ``"set_motion_detection"``, ``"ptz_move"``, ``"ptz_stop"``, ``"goto_preset"``,
  ``"refresh_status"``, ``"load_presets"``, ``"load_recordings"``) and ``message`` is non-empty.
  Downloads report errors through ``download_finished`` instead.
* Status: ``refresh_status`` emits ``status_changed(id, full_status_dict)`` and caches it. The
  ``set_*`` toggles are optimistic: BEFORE returning they emit ``status_changed`` with the cached
  dict updated to the new value; on failure they emit ``command_failed`` and then
  ``status_changed`` with the old value restored (if nothing was cached yet, they refresh instead).
* ``download_progress`` values are non-decreasing in 0..1; on success the last one is 1.0 and
  ``download_finished(id, str(path), "")`` follows; on failure ``download_finished(id, "", message)``.
* ``add_camera`` stores both credentials in the vault (``CredentialKind.CLOUD`` and
  ``CredentialKind.CAMERA_ACCOUNT``), saves the config and emits ``cameras_changed``; a duplicate
  ``config.id`` raises ``ValueError``. ``update_camera`` replaces the config with the same id,
  saves, updates whichever credentials are given and, if credentials or host/ports changed,
  closes the existing service (the next operation creates a new one); unknown id -> ``KeyError``.
  ``remove_camera`` stops its stream/events, closes its service, ``vault.delete(id)``, saves, emits
  ``cameras_changed``.
* ``stop_all`` is synchronous: when it returns ``stop_stream()`` and ``stop_events()`` have been
  called on every service that was started.

Lane-tdd-ui additions to the contract (needed by the UI, reported to the orchestrator):
``config_store`` property (settings access for MainWindow/TrayController) and
``load_presets(camera_id)`` + ``presets_ready(camera_id, list[Preset])`` signal.
"""
from __future__ import annotations

import pathlib
from collections.abc import Callable
from datetime import date
from typing import TYPE_CHECKING

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QImage  # noqa: F401  (signal payload type)

from tapo_desktop.models import CameraConfig, Credentials, DayNightMode, RecordingClip, StreamQuality
from tapo_desktop.services.camera_service import CameraService
from tapo_desktop.services.config_store import ConfigStore

if TYPE_CHECKING:  # pragma: no cover
    from tapo_desktop.core.auth.vault import CredentialVault


class AppController(QObject):
    cameras_changed = pyqtSignal()
    frame_ready = pyqtSignal(str, QImage)             # camera_id, frame (emitted on GUI thread)
    stream_state = pyqtSignal(str, str, str)          # camera_id, StreamState.name, reason ("" if none)
    status_changed = pyqtSignal(str, dict)            # camera_id, status dict (see get_status)
    command_failed = pyqtSignal(str, str, str)        # camera_id, action, message -> UI rolls back
    motion_event = pyqtSignal(str, object)            # camera_id, MotionEvent
    recordings_ready = pyqtSignal(str, object, list)  # camera_id, date, list[RecordingClip]
    download_progress = pyqtSignal(str, float)        # camera_id, 0..1
    download_finished = pyqtSignal(str, str, str)     # camera_id, path, error ("" = ok)
    presets_ready = pyqtSignal(str, list)             # camera_id, list[Preset]  (lane addition)

    def __init__(self, config_store: ConfigStore, vault: CredentialVault,
                 service_factory: Callable[[CameraConfig], CameraService] | None = None, parent=None):
        super().__init__(parent)
        raise NotImplementedError

    @property
    def config_store(self) -> ConfigStore:
        """The ConfigStore given to ``__init__`` (settings access for the UI)."""
        raise NotImplementedError

    def cameras(self) -> list[CameraConfig]:
        raise NotImplementedError

    def add_camera(self, config: CameraConfig, cloud: Credentials, camera_account: Credentials) -> None:
        raise NotImplementedError

    def update_camera(self, config: CameraConfig, cloud: Credentials | None = None,
                      camera_account: Credentials | None = None) -> None:
        raise NotImplementedError

    def remove_camera(self, camera_id: str) -> None:
        raise NotImplementedError

    def start_view(self, camera_id: str, quality: StreamQuality, width: int, height: int) -> None:
        raise NotImplementedError

    def stop_view(self, camera_id: str) -> None:
        raise NotImplementedError

    def stop_all(self) -> None:
        """Stops streams + events of every camera; synchronous; used on quit."""
        raise NotImplementedError

    def refresh_status(self, camera_id: str) -> None:
        raise NotImplementedError

    def set_privacy(self, camera_id: str, on: bool) -> None:
        raise NotImplementedError

    def set_day_night(self, camera_id: str, mode: DayNightMode) -> None:
        raise NotImplementedError

    def set_led(self, camera_id: str, on: bool) -> None:
        raise NotImplementedError

    def set_alarm(self, camera_id: str, on: bool) -> None:
        raise NotImplementedError

    def set_motion_detection(self, camera_id: str, on: bool) -> None:
        raise NotImplementedError

    def ptz_move(self, camera_id: str, dx: int, dy: int) -> None:
        raise NotImplementedError

    def ptz_stop(self, camera_id: str) -> None:
        raise NotImplementedError

    def load_presets(self, camera_id: str) -> None:
        """Fetch presets off-thread and emit ``presets_ready`` (lane addition)."""
        raise NotImplementedError

    def goto_preset(self, camera_id: str, preset_id: str) -> None:
        raise NotImplementedError

    def load_recordings(self, camera_id: str, day: date) -> None:
        raise NotImplementedError

    def download_recording(self, camera_id: str, clip: RecordingClip, out_path: pathlib.Path) -> None:
        raise NotImplementedError

    def wait_idle(self, timeout_ms: int = 5000) -> bool:
        """Test helper: block until no background job is queued/running (signals already queued to
        the GUI thread may still need ``processEvents``). Returns False on timeout."""
        raise NotImplementedError
