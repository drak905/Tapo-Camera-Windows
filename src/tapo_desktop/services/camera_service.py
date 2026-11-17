"""Per-camera service facade over the core modules (contract: docs/CONTRACT_UI.md).

All methods are BLOCKING; :class:`tapo_desktop.services.controller.AppController` runs them on
worker threads. Core types are imported lazily / under TYPE_CHECKING so this module imports even
while the core lanes are still stubs.

Status dict returned by :meth:`CameraService.get_status`::

    {"privacy": bool, "day_night": DayNightMode, "led": bool, "motion_detection": bool, "alarm": bool}

PTZ convention (shared by the whole UI lane): ``dx > 0`` = pan right, ``dx < 0`` = pan left,
``dy > 0`` = tilt up, ``dy < 0`` = tilt down; ``(0, 0)`` is not a valid move (use ``ptz_stop``).
This matches the camera's own convention [spec:control.ptz.move]: ``+x`` right, ``+y`` UP (the
app computes ``y = (taps_up - taps_down) * step``, [spec:control.ptz.click_offset_algorithm]),
so no sign flip is needed on the way to ``motorMove``.
"""
from __future__ import annotations

import abc
import logging
import pathlib
import threading
from collections.abc import Callable
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from tapo_desktop.core.errors import TapoAuthError, TapoDeviceError, TapoError
from tapo_desktop.models import (
    CameraConfig,
    CredentialKind,
    Credentials,
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

log = logging.getLogger(__name__)

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



#: Coordinate units per PTZ unit for diagonal relative moves (``motorMove``); the app's default
#: "medium" sensitivity step [spec:control.ptz.click_offset_algorithm default_step].
PTZ_STEP = 10
#: Single-axis hold-to-move uses ``singalMove`` [spec:control.ptz.move_step], the request the app
#: sends for press-and-hold: direction angle 0 = right, 90 = up, 180 = left, 270 = down.
_STEP_DIRECTIONS: dict[tuple[int, int], int] = {(1, 0): 0, (0, 1): 90, (-1, 0): 180, (0, -1): 270}


def _sign(v: int) -> int:
    return (v > 0) - (v < 0)


def _local_tz():
    return datetime.now().astimezone().tzinfo


class TapoCameraService(CameraService):
    """Real wiring: TapoControlClient (HTTPS :443, cloud password, user "admin"), StreamSupervisor
    (RTSP :554, camera account), OnvifClient/PullPoint (:2020, camera account) and
    RecordingDownloader (:8800, cloud password).

    Credentials are read from ``vault.get(config.id, CredentialKind.CLOUD | CAMERA_ACCOUNT)``.
    Construction performs no network I/O. Core modules are imported lazily so that this module
    (and the UI) import even when a core package is unavailable.

    Thread-safety: the controller serialises calls per camera except downloads, which may run
    concurrently with control calls; shared state is guarded by an internal lock and no network
    call is made while holding it.
    """

    def __init__(self, config: CameraConfig, vault: CredentialVault, *,
                 limiter: StreamLimiter | None = None, pin_store: PinStore | None = None):
        self.config = config
        self._vault = vault
        self._limiter = limiter
        self._pin_store = pin_store
        self._lock = threading.RLock()
        self._client: Any = None
        self._device_info: DeviceInfo | None = None
        self._supervisor: Any = None
        self._stream_gen = 0  # bumped whenever a supervisor is replaced: mutes stale callbacks
        self._on_frame: Callable[[Frame], None] | None = None
        self._on_state: Callable[[StreamState, str | None], None] | None = None
        self._subscriber: Any = None
        self._on_event: Callable[[MotionEvent], None] | None = None
        self._alarm = False  # the API has no alarm getter; last value we set
        self._cancel = threading.Event()
        self._closed = False

    def __repr__(self) -> str:
        return f"TapoCameraService(id={self.config.id!r}, host={self.config.host!r})"

    # ------------------------------------------------------------------ helpers
    def _creds(self, kind: CredentialKind) -> Credentials:
        creds = self._vault.get(self.config.id, kind)
        if creds is None:
            raise TapoAuthError(f"no {kind.value} credentials stored for camera {self.config.name!r}")
        return creds

    def _check_open(self) -> None:
        if self._closed:
            raise TapoError("camera service is closed")

    def _control(self) -> Any:
        """The (lazily created) TapoControlClient; it logs in on first use."""
        with self._lock:
            self._check_open()
            if self._client is None:
                from tapo_desktop.core.control_client.client import TapoControlClient

                cloud = self._creds(CredentialKind.CLOUD)
                self._client = TapoControlClient(self.config.host, cloud.password,
                                                 username=cloud.username or "admin",
                                                 port=self.config.https_port, pin_store=self._pin_store)
            return self._client

    # ------------------------------------------------------------------ control
    def connect(self) -> DeviceInfo:
        client = self._control()
        if not client.logged_in:
            client.login()
        if self._device_info is None:
            self._device_info = client.get_device_info()
        return self._device_info

    def ptz_move(self, dx: int, dy: int) -> None:
        if dx == 0 and dy == 0:
            raise ValueError("ptz_move needs a non-zero direction; use ptz_stop to stop")
        client = self._control()
        direction = _STEP_DIRECTIONS.get((_sign(dx), _sign(dy)))  # single-axis keys only
        if direction is not None:
            client.step(direction)
        else:  # diagonal: one relative move, +y = up (see module docstring)
            client.move(int(dx) * PTZ_STEP, int(dy) * PTZ_STEP)

    def ptz_stop(self) -> None:
        self._control().stop_move()

    def get_presets(self) -> list[Preset]:
        return list(self._control().get_presets())

    def goto_preset(self, preset_id: str) -> None:
        self._control().goto_preset(str(preset_id))

    def get_status(self) -> dict:
        client = self._control()

        def read(name: str, fn: Callable[[], Any], default: Any) -> Any:
            # A model without e.g. a status LED answers with a device error for that one getter;
            # report the default for it instead of failing the whole status.
            try:
                return fn()
            except TapoDeviceError as exc:
                log.debug("status %s unavailable on camera %s: %s", name, self.config.id, exc)
                return default

        return {
            "privacy": bool(read("privacy", client.get_privacy, False)),
            "day_night": read("day_night", client.get_day_night, DayNightMode.AUTO),
            "led": bool(read("led", client.get_led, False)),
            "motion_detection": bool(read("motion_detection", lambda: client.get_motion_detection()[0], False)),
            "alarm": self._alarm,
        }

    def set_privacy(self, on: bool) -> None:
        self._control().set_privacy(bool(on))

    def set_day_night(self, mode: DayNightMode) -> None:
        self._control().set_day_night(DayNightMode(mode))

    def set_led(self, on: bool) -> None:
        self._control().set_led(bool(on))

    def set_alarm(self, on: bool) -> None:
        self._control().set_alarm(bool(on))
        self._alarm = bool(on)

    def set_motion_detection(self, on: bool) -> None:
        self._control().set_motion_detection(bool(on))

    # ------------------------------------------------------------------ recordings
    def days_with_recordings(self, start: date, end: date) -> list[date]:
        return list(self._control().days_with_recordings(start, end))

    def recordings_for_day(self, day: date) -> list[RecordingClip]:
        """Clips of the LOCAL calendar day ``day``."""
        return list(self._control().recordings_for_day(day, tz=_local_tz()))

    def download_recording(self, clip: RecordingClip, out_path: pathlib.Path,
                           progress: Callable[[float], None] | None = None) -> pathlib.Path:
        from tapo_desktop.core.vod.downloader import RecordingDownloader

        self._check_open()
        cloud = self._creds(CredentialKind.CLOUD)
        downloader = RecordingDownloader(self.config.host, cloud.password, port=self.config.media_port)
        return downloader.download(clip, pathlib.Path(out_path), progress=progress, cancel=self._cancel)

    # ------------------------------------------------------------------ live stream
    def start_stream(self, quality: StreamQuality, width: int, height: int,
                     on_frame: Callable[[Frame], None],
                     on_state: Callable[[StreamState, str | None], None]) -> None:
        from tapo_desktop.core.media_engine.profiles import build_rtsp_url
        from tapo_desktop.core.media_engine.supervisor import StreamState, StreamSupervisor

        account = self._creds(CredentialKind.CAMERA_ACCOUNT)
        url = build_rtsp_url(self.config.host, account.username, account.password,
                             StreamQuality(quality), port=self.config.rtsp_port)
        with self._lock:
            self._check_open()
            self._on_frame, self._on_state = on_frame, on_state
            current = self._supervisor
            if current is not None and current.state not in (StreamState.FAILED, StreamState.STOPPED):
                current.switch(url, int(width), int(height))
                return
            self._supervisor = None
            self._stream_gen += 1  # the old supervisor's STOPPED must not reach the UI
        if current is not None:
            current.stop()
        with self._lock:
            self._check_open()
            gen = self._stream_gen
            sup = StreamSupervisor(url, int(width), int(height),
                                   on_frame=lambda f: self._forward_frame(gen, f),
                                   on_state=lambda s, r: self._forward_state(gen, s, r),
                                   limiter=self._limiter, limiter_key=self.config.host)
            self._supervisor = sup
        sup.start()

    def stop_stream(self) -> None:
        with self._lock:
            sup, self._supervisor = self._supervisor, None
        if sup is not None:
            sup.stop()  # its STOPPED transition is still forwarded
        with self._lock:
            self._stream_gen += 1

    def _forward_frame(self, gen: int, frame: Frame) -> None:
        cb = self._on_frame
        if gen == self._stream_gen and cb is not None:
            cb(frame)

    def _forward_state(self, gen: int, state: StreamState, reason: str | None) -> None:
        cb = self._on_state
        if gen == self._stream_gen and cb is not None:
            cb(state, reason)

    # ------------------------------------------------------------------ ONVIF events
    def start_events(self, on_event: Callable[[MotionEvent], None]) -> None:
        from tapo_desktop.core.onvif_service.events import PullPointSubscriber
        from tapo_desktop.core.onvif_service.soap import OnvifClient

        with self._lock:
            self._check_open()
            self._on_event = on_event
            if self._subscriber is not None:
                return
        account = self._creds(CredentialKind.CAMERA_ACCOUNT)
        client = OnvifClient(self.config.host, account.username, account.password, port=self.config.onvif_port)
        sub = PullPointSubscriber(client, self._forward_event)
        sub.start()
        with self._lock:
            self._subscriber = sub

    def stop_events(self) -> None:
        with self._lock:
            sub, self._subscriber = self._subscriber, None
        if sub is not None:
            sub.stop()

    def _forward_event(self, event: MotionEvent) -> None:
        cb = self._on_event
        if cb is not None:
            cb(event)

    # ------------------------------------------------------------------ lifecycle
    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            client, self._client = self._client, None
        self._cancel.set()
        for name, fn in (("stop_events", self.stop_events), ("stop_stream", self.stop_stream)):
            try:
                fn()
            except Exception as exc:  # close must release everything it can
                log.warning("%s failed while closing camera %s: %s", name, self.config.id, exc)
        if client is not None:
            try:
                client.close()
            except Exception as exc:
                log.warning("closing the control client of camera %s failed: %s", self.config.id, exc)
