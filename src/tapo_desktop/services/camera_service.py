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
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any

from tapo_desktop.core.errors import (
    TapoAuthError, TapoConnectionError, TapoDeviceError, TapoError, TapoLockedError,
)
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
#: Stream failure reason for cameras that were not found on the LAN.
NOT_ON_NETWORK = "Not on this network"
#: Motion polling period when no camera account exists (ONVIF needs one) [spec:events.last_alarm_info].
ALARM_POLL_INTERVAL_S = 5.0
LAST_ALARM_INFO_REQUEST: dict = {"method": "getLastAlarmInfo", "params": {"system": {"name": ["last_alarm_info"]}}}
_HD_QUALITIES = (StreamQuality.HD, StreamQuality.TELE_HD)
_TELE_QUALITIES = (StreamQuality.TELE_HD, StreamQuality.TELE_SD)


def local_resolution(quality: StreamQuality) -> str:
    """:8800 preview resolution for ``quality``: ``"HD"`` (main stream) or ``"VGA"`` (minor stream)
    [spec:stream.control.preview, spec:lan.live_view_selection]."""
    return "HD" if StreamQuality(quality) in _HD_QUALITIES else "VGA"


def local_channel(quality: StreamQuality) -> int:
    """:8800 preview channel: 1 for the second (tele) lens of dual-lens models, else 0."""
    return 1 if StreamQuality(quality) in _TELE_QUALITIES else 0


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
    """Real wiring: TapoControlClient (HTTPS :443, cloud password, user "admin"), live view over
    LocalStreamSupervisor (:8800, cloud password, like the Tapo app) or StreamSupervisor (RTSP :554,
    camera account; see :meth:`live_transport`), motion from OnvifClient/PullPoint (:2020, camera
    account) or ``getLastAlarmInfo`` polling (no camera account) and RecordingDownloader (:8800,
    cloud password). Cameras with ``lan_found=False`` or no host fail fast with
    :data:`NOT_ON_NETWORK` and do no network I/O.

    The ``*_factory`` arguments replace the core classes (tests); defaults import them lazily.

    Credentials are read from ``vault.get(config.id, CredentialKind.CLOUD | CAMERA_ACCOUNT)``; a
    ``CredentialKind.LOCAL_TOKEN`` entry (cloud-issued LAT, [spec:lat.*]) is passed as ``local_token``
    to the control client, the :8800 live view and the downloader, and a changed LAT rebuilds the
    control client (fresh auth breaker) and restarts the live view.
    Construction performs no network I/O. Core modules are imported lazily so that this module
    (and the UI) import even when a core package is unavailable.

    Thread-safety: the controller serialises calls per camera except downloads, which may run
    concurrently with control calls; shared state is guarded by an internal lock and no network
    call is made while holding it.
    """

    def __init__(self, config: CameraConfig, vault: CredentialVault, *,
                 limiter: StreamLimiter | None = None, pin_store: PinStore | None = None,
                 local_stream_factory: Callable[..., Any] | None = None,
                 stream_factory: Callable[..., Any] | None = None,
                 control_factory: Callable[..., Any] | None = None,
                 alarm_poll_interval: float = ALARM_POLL_INTERVAL_S):
        self.config = config
        self._vault = vault
        self._limiter = limiter
        self._pin_store = pin_store
        self._local_stream_factory = local_stream_factory
        self._stream_factory = stream_factory
        self._control_factory = control_factory
        self._alarm_poll_interval = float(alarm_poll_interval)
        self._transport: str | None = None  # "local" | "rtsp" of the running supervisor
        self._poller: _AlarmPoller | None = None
        self._lock = threading.RLock()
        self._client: Any = None
        self._client_token: str | None = None  # LAT the control client was built with
        self._stream_token: str | None = None  # LAT the running local supervisor was built with
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
        """The (lazily created) TapoControlClient; it logs in on first use.

        The client is rebuilt (fresh session and auth breaker) when the camera's vault local access
        token changed since it was created [spec:lat.client.recommended_flow].
        """
        token = self._local_token()
        stale = None
        with self._lock:
            self._check_open()
            if self._client is not None and token != self._client_token:
                log.info("camera %s: local access token changed; rebuilding the control client", self.config.id)
                stale, self._client = self._client, None
            if self._client is None:
                self._check_on_network()
                factory = self._control_factory
                if factory is None:
                    from tapo_desktop.core.control_client.client import TapoControlClient as factory
                cloud = self._creds(CredentialKind.CLOUD)
                hints: dict[str, Any] = {}
                if self.config.control_protocol == "tpap":  # TDP said encrypt_type "4": skip the v3 probe
                    hints["encrypt_type"] = ["4"]
                if token:  # only when present: Phase 1 factories do not know the keyword
                    hints["local_token"] = token
                self._client = factory(host=self.config.host, password=cloud.password,
                                       username=cloud.username or "admin",
                                       port=self.config.https_port, pin_store=self._pin_store, **hints)
                self._client_token = token
            client = self._client
        if stale is not None:
            try:
                stale.close()
            except Exception as exc:
                log.debug("closing the old control client of camera %s failed: %s", self.config.id, exc)
        return client

    def _local_token(self) -> str | None:
        """The camera's cloud-issued local access token from the vault, or None."""
        creds = self._vault.get(self.config.id, CredentialKind.LOCAL_TOKEN)
        return creds.password if creds is not None and creds.password else None

    def diagnostics(self) -> str:
        """One-line, secret-free diagnostic text, e.g.
        ``"transport: local; control: tpap; using local access token: yes"``."""
        parts = [f"transport: {self.live_transport()}"]
        protocol = getattr(self._client, "protocol", None) if self._client is not None else None
        if isinstance(protocol, str) and protocol:
            parts.append(f"control: {protocol}")
        elif self.config.control_protocol:
            parts.append(f"control: {self.config.control_protocol}")
        parts.append(f"using local access token: {'yes' if self._local_token() else 'no'}")
        return "; ".join(parts)

    def _on_network(self) -> bool:
        """False when the camera was not found on the LAN (cloud camera without a usable host)."""
        return bool(self.config.lan_found and self.config.host.strip())

    def _check_on_network(self) -> None:
        if not self._on_network():
            raise TapoConnectionError(NOT_ON_NETWORK)

    def _has_camera_account(self) -> bool:
        return self._vault.get(self.config.id, CredentialKind.CAMERA_ACCOUNT) is not None

    def live_transport(self) -> str:
        """Transport the next ``start_stream`` uses: ``"local"`` (:8800, like the Tapo app) or
        ``"rtsp"`` (:554, needs a camera account).

        ``"local"`` -> local; ``"rtsp"`` -> RTSP when a camera account exists (else local);
        ``"auto"`` -> RTSP when a camera account exists (Phase 1 manual cameras), else local.
        """
        mode = (self.config.live_transport or "auto").lower()
        if mode == "local":
            return "local"
        return "rtsp" if self._has_camera_account() else "local"

    # ------------------------------------------------------------------ control
    def connect(self) -> DeviceInfo:
        self._check_on_network()
        client = self._control()
        if not client.logged_in:
            try:
                client.login()
            except TapoAuthError as exc:  # say whether the LAT was tried; never the token itself
                yes = "yes" if self._local_token() else "no"
                cls = TapoLockedError if isinstance(exc, TapoLockedError) else TapoAuthError
                raise cls(f"{exc} (using local access token: {yes})") from exc
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
        self._check_on_network()
        cloud = self._creds(CredentialKind.CLOUD)
        token = self._local_token()
        extra = {"local_token": token} if token else {}
        downloader = RecordingDownloader(self.config.host, cloud.password, port=self.config.media_port, **extra)
        return downloader.download(clip, pathlib.Path(out_path), progress=progress, cancel=self._cancel)

    # ------------------------------------------------------------------ live stream
    def start_stream(self, quality: StreamQuality, width: int, height: int,
                     on_frame: Callable[[Frame], None],
                     on_state: Callable[[StreamState, str | None], None]) -> None:
        from tapo_desktop.core.media_engine.supervisor import StreamState

        quality, width, height = StreamQuality(quality), int(width), int(height)
        if not self._on_network():
            self.stop_stream()
            on_state(StreamState.FAILED, NOT_ON_NETWORK)
            return
        transport = self.live_transport()
        if transport == "rtsp":
            from tapo_desktop.core.media_engine.profiles import build_rtsp_url

            account = self._creds(CredentialKind.CAMERA_ACCOUNT)
            target: Any = build_rtsp_url(self.config.host, account.username, account.password,
                                         quality, port=self.config.rtsp_port)
        else:
            target = local_resolution(quality)
        token = self._local_token() if transport == "local" else None
        with self._lock:
            self._check_open()
            self._on_frame, self._on_state = on_frame, on_state
            current = self._supervisor
            if (current is not None and self._transport == transport
                    and current.state not in (StreamState.FAILED, StreamState.STOPPED)
                    and (transport != "local" or token == self._stream_token)):
                current.switch(target, width, height)
                return
            self._supervisor = None
            self._stream_gen += 1  # the old supervisor's STOPPED must not reach the UI
        if current is not None:
            current.stop()
        with self._lock:
            self._check_open()
            gen = self._stream_gen
            callbacks = dict(on_frame=lambda f: self._forward_frame(gen, f),
                             on_state=lambda s, r: self._forward_state(gen, s, r),
                             limiter=self._limiter, limiter_key=self.config.host)
            if transport == "rtsp":
                sup = self._new_rtsp_supervisor(target, width, height, callbacks)
            else:
                sup = self._new_local_supervisor(quality, width, height, callbacks, token)
            self._supervisor, self._transport, self._stream_token = sup, transport, token
        sup.start()

    def _new_rtsp_supervisor(self, url: str, width: int, height: int, callbacks: dict) -> Any:
        factory = self._stream_factory
        if factory is None:
            from tapo_desktop.core.media_engine.supervisor import StreamSupervisor as factory
        return factory(url, width, height, **callbacks)

    def _new_local_supervisor(self, quality: StreamQuality, width: int, height: int, callbacks: dict,
                              token: str | None = None) -> Any:
        """Local :8800 preview, user ``admin`` + the TP-Link ID password [spec:lan.live_view_selection],
        or the camera's local access token when one is stored [spec:lat.media.digest]."""
        factory = self._local_stream_factory
        if factory is None:
            from tapo_desktop.core.media_engine.local_stream import LocalStreamSupervisor as factory
        cloud = self._creds(CredentialKind.CLOUD)
        extra = {"local_token": token} if token else {}  # only when present: older factories lack it
        return factory(self.config.host, cloud.password, width, height,
                       resolution=local_resolution(quality), port=self.config.media_port,
                       channel=local_channel(quality), **callbacks, **extra)

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

    # ------------------------------------------------------------------ motion events
    def start_events(self, on_event: Callable[[MotionEvent], None]) -> None:
        """ONVIF pull-point events when a camera account exists; otherwise poll
        ``getLastAlarmInfo`` every ``alarm_poll_interval`` seconds [spec:events.last_alarm_info]
        (the Tapo app's LAN behaviour: it needs only the TP-Link ID password)."""
        with self._lock:
            self._check_open()
            self._on_event = on_event
            if self._subscriber is not None or self._poller is not None:
                return
        if not self._has_camera_account():
            self._check_on_network()
            poller = _AlarmPoller(self, self._alarm_poll_interval)
            with self._lock:
                self._check_open()
                self._poller = poller
            poller.start()
            return
        from tapo_desktop.core.onvif_service.events import PullPointSubscriber
        from tapo_desktop.core.onvif_service.soap import OnvifClient

        account = self._creds(CredentialKind.CAMERA_ACCOUNT)
        client = OnvifClient(self.config.host, account.username, account.password, port=self.config.onvif_port)
        sub = PullPointSubscriber(client, self._forward_event)
        sub.start()
        with self._lock:
            self._subscriber = sub

    def stop_events(self) -> None:
        with self._lock:
            sub, self._subscriber = self._subscriber, None
            poller, self._poller = self._poller, None
        if poller is not None:
            poller.stop()
        if sub is not None:
            sub.stop()

    def last_alarm_info(self) -> tuple[str, str]:
        """``(last_alarm_time, last_alarm_type)`` from ``getLastAlarmInfo`` (strings, may be empty)."""
        result = self._control().request(LAST_ALARM_INFO_REQUEST)
        info = result.get("system", {}).get("last_alarm_info", {}) if isinstance(result, dict) else {}
        if not isinstance(info, dict):
            info = {}
        return str(info.get("last_alarm_time") or ""), str(info.get("last_alarm_type") or "")

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


class _AlarmPoller:
    """Background ``getLastAlarmInfo`` poller: emits one active :class:`MotionEvent` each time
    ``last_alarm_time`` changes after the first (baseline) reading. Errors are logged and polling
    continues; :meth:`stop` joins the thread (bounded)."""

    def __init__(self, service: TapoCameraService, interval: float):
        self._service = service
        self._interval = max(0.01, float(interval))
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"tapo-alarm-{service.config.id}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout)

    def _run(self) -> None:
        svc = self._service
        baseline: str | None = None
        failures = 0
        while not self._stop.is_set():
            try:
                when, kind = svc.last_alarm_info()
                failures = 0
            except Exception as exc:  # the camera may be rebooting / briefly unreachable
                failures += 1
                if failures in (1, 10) or failures % 60 == 0:
                    log.info("getLastAlarmInfo failed for camera %s (%d in a row): %s",
                             svc.config.id, failures, exc)
            else:
                if baseline is None:
                    baseline = when
                elif when and when != baseline:
                    baseline = when
                    if not self._stop.is_set():
                        svc._forward_event(_alarm_event(svc.config.host, when, kind))
            self._stop.wait(self._interval)


def _alarm_event(host: str, when: str, kind: str) -> MotionEvent:
    try:
        stamp = datetime.fromtimestamp(int(when), tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        stamp = datetime.now(timezone.utc)
    return MotionEvent(camera_host=host, topic=f"tapo:LastAlarmInfo/{kind or 'motion'}", active=True,
                       timestamp=stamp, source="last_alarm_info",
                       data={"last_alarm_time": when, "last_alarm_type": kind})
