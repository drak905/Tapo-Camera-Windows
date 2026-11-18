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
from tapo_desktop.services.stream_policy import decoder_for

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
#: Minimum ``getLastAlarmInfo`` polling period for cameras reached through the cloud passthrough: every
#: poll is one TP-Link cloud call, so remote/shared cameras are polled at most every 30 s (the app has
#: no client-side passthrough rate limit and the server's is unknown [spec:remote.control.rate_limits]).
REMOTE_ALARM_POLL_INTERVAL_S = 30.0
LAST_ALARM_INFO_REQUEST: dict = {"method": "getLastAlarmInfo", "params": {"system": {"name": ["last_alarm_info"]}}}
#: Error of :meth:`CameraService.start_playback` for services without playback streaming.
PLAYBACK_UNSUPPORTED = "Playback is not available for this camera"
#: Playback FAILED reason when the camera refuses another media session (HTTP 503 / -52407 / limiter).
PLAYBACK_LIMIT_MESSAGE = ("The camera refused another video session (it allows only two at a time). "
                          "Close the live view of this camera or another app watching it, then try again.")
_PLAYBACK_SPEEDS = ("1/1", "2/1", "4/1", "8/1")
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

    # ------------------------------------------------------------------ Phase 5: SD-card playback
    # Not abstract: services written before Phase 5 (and test doubles) get these safe defaults.
    def start_playback(self, start_utc: float, end_utc: float, width: int, height: int,
                       on_frame: Callable[[Frame], None], on_state: Callable[[StreamState, str | None], None],
                       on_position: Callable[[float], None], *, segments: list | None = None) -> None:
        """Play the SD-card recordings from ``start_utc`` to ``end_utc`` (epoch s) as ``width`` x ``height``
        frames; ``on_position(utc)`` reports the recording time shown. ``segments`` = the day's clips as
        ``(start, end)`` pairs (continuous playback across recordings). Replaces a running playback.
        Non-blocking once started; callbacks fire on worker threads. Default: not supported."""
        raise TapoError(PLAYBACK_UNSUPPORTED)

    def pause_playback(self) -> None:
        """Pause the running playback (no-op when none)."""

    def resume_playback(self) -> None:
        """Resume a paused playback (no-op when none)."""

    def seek_playback(self, utc: float) -> None:
        """Continue the running playback at recording time ``utc`` (no-op when none)."""

    def set_playback_speed(self, scale: str) -> None:
        """Playback speed ``"1/1"``, ``"2/1"``, ``"4/1"`` or ``"8/1"`` (no-op when nothing plays)."""

    def stop_playback(self) -> None:
        """Stop the playback (idempotent)."""

    # ------------------------------------------------------------------ Phase 7: rename
    def rename(self, name: str) -> None:
        """Set the camera's own name (setDeviceAlias). Default: not supported."""
        raise TapoError("Renaming this camera is not supported")



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
    :data:`NOT_ON_NETWORK` and do no network I/O - unless they are cloud cameras that are online and
    ``remote()`` returns a signed-in :class:`~tapo_desktop.services.remote.RelayProvider`: then the live
    view goes through the TP-Link cloud relay (:class:`RelayStreamSupervisor`, VGA in the grid, HD
    fullscreen, at most ``provider.gate.capacity`` remote streams outside the view (the controller reserves a
    relay slot for every viewed camera, Phase 6); extra ones report FAILED
    :data:`~tapo_desktop.services.remote.REMOTE_CLICK_TO_VIEW` until ``start_stream(..., preempt=True)``),
    ``connect()`` succeeds without network I/O and control, PTZ, status, presets and recordings go through
    the cloud passthrough (``provider.control_client``, a RemoteControlClient; status is one batched call),
    downloads through the relay (``provider.recording_downloader``) and motion polling through the cloud at
    most every :data:`REMOTE_ALARM_POLL_INTERVAL_S` s. Providers without the passthrough API keep raising
    ``TapoConnectionError(NETWORK_ONLY)``. Offline cloud cameras report "Camera is offline".

    Shared cameras (``config.role != 0``) are always cloud-routed, even when found on the LAN: no local
    login is ever attempted with the user's own password (it would only spend the camera's lockout
    attempts; the app goes local only for the owner). Without a signed-in provider they report
    :data:`~tapo_desktop.services.remote.SHARED_CLOUD_ONLY`. The per-camera auth breaker applies to the
    LAN client only.

    Playback streaming (Phase 5, :meth:`start_playback` & co.) follows the same routing: owned LAN cameras
    over :8800 (``playback_factory``, default PlaybackStreamSupervisor, limiter key = host so live view +
    playback stay within the camera's two media sessions), cloud-routed cameras through
    ``provider.create_playback`` (relay playback slot, no live-view gate slot). A camera refusing another
    media session reports FAILED :data:`PLAYBACK_LIMIT_MESSAGE`.

    The ``*_factory`` arguments replace the core classes (tests); defaults import them lazily. Every live
    view supervisor gets ``hwaccel=``:func:`~tapo_desktop.services.stream_policy.decoder_for` ``(quality)``
    (Phase 6: software decoding for SD grid streams, hardware with software fallback for HD).

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
                 alarm_poll_interval: float = ALARM_POLL_INTERVAL_S,
                 remote: Callable[[], Any] | None = None,
                 playback_factory: Callable[..., Any] | None = None):
        self.config = config
        self._vault = vault
        self._limiter = limiter
        self._pin_store = pin_store
        self._local_stream_factory = local_stream_factory
        self._stream_factory = stream_factory
        self._control_factory = control_factory
        self._playback_factory = playback_factory
        self._playback: Any = None  # running playback supervisor (Phase 5)
        self._playback_gen = 0  # bumped whenever the playback is replaced/stopped: mutes stale callbacks
        self._playback_callbacks: tuple[Callable, Callable, Callable] | None = None
        self._remote = remote
        self._relay_gate: Any = None  # RemoteStreamGate holding this camera's relay slot
        self._alarm_poll_interval = float(alarm_poll_interval)
        self._transport: str | None = None  # "local" | "rtsp" | "relay" of the running supervisor
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
        """The control client: for cloud-routed cameras (remote, or shared to the account) the provider's
        :class:`~tapo_desktop.core.cloud.passthrough.RemoteControlClient`; otherwise the (lazily created)
        LAN TapoControlClient, which logs in on first use.

        The LAN client is rebuilt (fresh session and auth breaker) when the camera's vault local access
        token changed since it was created [spec:lat.client.recommended_flow].
        """
        if self._cloud_routed():
            self._check_open()
            remote = self._remote_control()
            if remote is not None:
                return remote
            self._check_on_network()  # always raises here: NETWORK_ONLY / offline / shared / not on network
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
        if self._cloud_routed():
            parts.append("control: cloud")
        elif isinstance(protocol, str) and protocol:
            parts.append(f"control: {protocol}")
        elif self.config.control_protocol:
            parts.append(f"control: {self.config.control_protocol}")
        parts.append(f"using local access token: {'yes' if self._local_token() else 'no'}")
        return "; ".join(parts)

    def _on_network(self) -> bool:
        """False when the camera was not found on the LAN (cloud camera without a usable host)."""
        return bool(self.config.lan_found and self.config.host.strip())

    def _cloud_routed(self) -> bool:
        """True for cloud cameras reached through the TP-Link cloud: not on this LAN, or shared to the
        account (``role != 0``: never logged in locally, see :func:`tapo_desktop.services.remote.cloud_routed`)."""
        return self.config.source == "cloud" and (self.config.role != 0 or not self._on_network())

    def _remote_provider(self) -> Any:
        """The signed-in account's relay provider when this camera is reached through the cloud, else
        None: a cloud-routed camera that is online, with a provider that is signed in."""
        if self._remote is None or not self._cloud_routed():
            return None
        if not self.config.online:
            return None
        provider = self._remote()
        return provider if provider is not None and getattr(provider, "available", False) else None

    def _remote_control(self) -> Any:
        """The provider's RemoteControlClient for this camera, or None (no provider / no passthrough API /
        no thing record). May refresh the account's thing list (network I/O, no lock held)."""
        provider = self._remote_provider()
        getter = getattr(provider, "control_client", None) if provider is not None else None
        return getter(self.config) if callable(getter) else None

    def _cloud_offline(self) -> bool:
        return self.config.source == "cloud" and not self.config.online

    def _check_on_network(self) -> None:
        """Raise for LAN-only features of a camera that is not usable on the LAN: remote cameras without
        a cloud control path get :data:`~tapo_desktop.services.remote.NETWORK_ONLY`, offline cloud cameras
        "Camera is offline", shared cameras without a signed-in cloud
        :data:`~tapo_desktop.services.remote.SHARED_CLOUD_ONLY`, the rest :data:`NOT_ON_NETWORK`."""
        if not self._cloud_routed() and self._on_network():
            return
        from tapo_desktop.services.remote import CAMERA_OFFLINE, NETWORK_ONLY, SHARED_CLOUD_ONLY

        if self._remote_provider() is not None:
            raise TapoConnectionError(NETWORK_ONLY)
        if self._cloud_offline():
            raise TapoConnectionError(CAMERA_OFFLINE)
        if self.config.source == "cloud" and self.config.role != 0:
            raise TapoConnectionError(SHARED_CLOUD_ONLY)
        raise TapoConnectionError(NOT_ON_NETWORK)

    def _has_camera_account(self) -> bool:
        return self._vault.get(self.config.id, CredentialKind.CAMERA_ACCOUNT) is not None

    def live_transport(self) -> str:
        """Transport the next ``start_stream`` uses: ``"local"`` (:8800, like the Tapo app) or
        ``"rtsp"`` (:554, needs a camera account).

        ``"local"`` -> local; ``"rtsp"`` -> RTSP when a camera account exists (else local);
        ``"auto"`` -> RTSP when a camera account exists (Phase 1 manual cameras), else local.
        A cloud camera that is not on this LAN but online, with a signed-in relay provider -> ``"relay"``.
        """
        if self._remote_provider() is not None:
            return "relay"
        mode = (self.config.live_transport or "auto").lower()
        if mode == "local":
            return "local"
        return "rtsp" if self._has_camera_account() else "local"

    # ------------------------------------------------------------------ control
    def connect(self) -> DeviceInfo:
        if self._remote_provider() is not None:  # live view only, through the relay: no LAN login
            if self._device_info is None:
                cfg = self.config
                self._device_info = DeviceInfo(model=cfg.model, alias=cfg.name, firmware="", mac=cfg.mac,
                                               device_id=cfg.device_id or cfg.id)
            return self._device_info
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
        if self._cloud_routed():
            return self._remote_status(client)

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

    def _remote_status(self, client: Any) -> dict:
        """The status dict in ONE cloud passthrough call (four getters batched); a getter the model does
        not support (non-zero item error_code) reports its default, like on the LAN."""
        from tapo_desktop.core.control_client import dtos

        getters: tuple[tuple[str, dict, Callable[[dict], Any], Any], ...] = (
            ("privacy", dtos.get_privacy_req(), dtos.parse_privacy, False),
            ("day_night", dtos.get_day_night_req(), dtos.parse_day_night, DayNightMode.AUTO),
            ("led", dtos.get_led_req(), dtos.parse_led, False),
            ("motion_detection", dtos.get_motion_detection_req(), lambda r: dtos.parse_motion_detection(r)[0], False),
        )
        items = client.multiple_request([req for _, req, _, _ in getters])
        status: dict[str, Any] = {}
        for i, (name, _req, parse, default) in enumerate(getters):
            item = items[i] if i < len(items) else None
            if item is None or item.get("error_code") != 0:
                log.debug("status %s unavailable on camera %s: %s", name, self.config.id,
                          None if item is None else item.get("error_code"))
                status[name] = default
            else:
                status[name] = parse(item.get("result") or {})
        return {"privacy": bool(status["privacy"]), "day_night": status["day_night"], "led": bool(status["led"]),
                "motion_detection": bool(status["motion_detection"]), "alarm": self._alarm}

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
        if self._cloud_routed():  # through the cloud relay (download slot, playback fallback)
            provider = self._remote_provider()
            make = getattr(provider, "recording_downloader", None) if provider is not None else None
            if not callable(make):
                self._check_on_network()  # raises the reason
            assert make is not None
            remote = make(self.config)
            return remote.download(clip, pathlib.Path(out_path), progress=progress, cancel=self._cancel)
        self._check_on_network()
        cloud = self._creds(CredentialKind.CLOUD)
        token = self._local_token()
        extra = {"local_token": token} if token else {}
        downloader = RecordingDownloader(self.config.host, cloud.password, port=self.config.media_port, **extra)
        return downloader.download(clip, pathlib.Path(out_path), progress=progress, cancel=self._cancel)

    # ------------------------------------------------------------------ live stream
    def start_stream(self, quality: StreamQuality, width: int, height: int,
                     on_frame: Callable[[Frame], None],
                     on_state: Callable[[StreamState, str | None], None], *, preempt: bool = False) -> None:
        """Start or switch the live stream. Not on the LAN: through the cloud relay when the camera is
        remote-viewable (``preempt=True`` = the user clicked the tile: take a relay slot even when the
        remote stream limit is reached), else FAILED "Camera is offline" / :data:`NOT_ON_NETWORK`."""
        from tapo_desktop.core.media_engine.supervisor import StreamState

        quality, width, height = StreamQuality(quality), int(width), int(height)
        if self._cloud_routed() or not self._on_network():
            provider = self._remote_provider()
            if provider is not None:
                self._start_relay(provider, quality, width, height, on_frame, on_state, preempt)
                return
            from tapo_desktop.services.remote import CAMERA_OFFLINE, SHARED_CLOUD_ONLY

            self.stop_stream()
            if self._cloud_offline():
                reason = CAMERA_OFFLINE
            elif self.config.source == "cloud" and self.config.role != 0:
                reason = SHARED_CLOUD_ONLY
            else:
                reason = NOT_ON_NETWORK
            on_state(StreamState.FAILED, reason)
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
        self._release_relay_slot()
        with self._lock:
            self._check_open()
            gen = self._stream_gen
            callbacks = dict(on_frame=lambda f: self._forward_frame(gen, f),
                             on_state=lambda s, r: self._forward_state(gen, s, r),
                             limiter=self._limiter, limiter_key=self.config.host, hwaccel=decoder_for(quality))
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

    def _start_relay(self, provider: Any, quality: StreamQuality, width: int, height: int,
                     on_frame: Callable[[Frame], None], on_state: Callable[[StreamState, str | None], None],
                     preempt: bool) -> None:
        """Remote live view through the relay [spec:remote.relay.*]: VGA for grid qualities, HD for
        fullscreen ones; at most ``provider.gate.capacity`` remote streams run at once."""
        from tapo_desktop.core.media_engine.supervisor import StreamState
        from tapo_desktop.services.remote import REMOTE_CLICK_TO_VIEW

        resolution, channel = local_resolution(quality), local_channel(quality)
        gate = provider.gate
        with self._lock:
            self._check_open()
            self._on_frame, self._on_state = on_frame, on_state
            current = self._supervisor
            if (current is not None and self._transport == "relay" and self._relay_gate is gate
                    and current.state not in (StreamState.FAILED, StreamState.STOPPED)):
                current.switch(resolution, width, height)
                return
            self._supervisor = None
            self._stream_gen += 1
        if current is not None:
            current.stop()
        if self._relay_gate is not gate:
            self._release_relay_slot()
        if not gate.acquire(self.config.id, self._evict_relay, preempt=preempt):
            log.info("camera %s: remote stream limit (%d) reached; waiting for a click", self.config.id,
                     gate.capacity)
            on_state(StreamState.FAILED, REMOTE_CLICK_TO_VIEW)
            return
        with self._lock:
            if self._closed:
                gate.release(self.config.id)
                raise TapoError("camera service is closed")
            gen = self._stream_gen
            callbacks = dict(on_frame=lambda f: self._forward_frame(gen, f),
                             on_state=lambda s, r: self._forward_state(gen, s, r),
                             limiter=self._limiter, limiter_key=self.config.device_id or self.config.id,
                             hwaccel=decoder_for(quality))
            try:
                sup = provider.create_stream(self.config, width, height, resolution=resolution, channel=channel,
                                             **callbacks)
            except Exception:
                gate.release(self.config.id)
                raise
            self._supervisor, self._transport, self._stream_token = sup, "relay", None
            self._relay_gate = gate
        sup.start()

    def _evict_relay(self) -> None:
        """The remote stream limit gave this camera's relay slot to a clicked tile: stop, then tell the
        UI it can be clicked again (runs on the other camera's worker thread)."""
        from tapo_desktop.core.media_engine.supervisor import StreamState
        from tapo_desktop.services.remote import REMOTE_CLICK_TO_VIEW

        with self._lock:
            sup = self._supervisor if self._transport == "relay" else None
            if sup is not None:
                self._supervisor = None
                self._stream_gen += 1
            self._relay_gate = None
            callback = self._on_state
        if sup is not None:
            sup.stop()
        if callback is not None:
            callback(StreamState.FAILED, REMOTE_CLICK_TO_VIEW)

    def _release_relay_slot(self) -> None:
        with self._lock:
            gate, self._relay_gate = self._relay_gate, None
        if gate is not None:
            gate.release(self.config.id)

    def stop_stream(self) -> None:
        with self._lock:
            sup, self._supervisor = self._supervisor, None
        if sup is not None:
            sup.stop()  # its STOPPED transition is still forwarded
        self._release_relay_slot()
        with self._lock:
            self._stream_gen += 1

    def _forward_frame(self, gen: int, frame: Frame) -> None:
        cb = self._on_frame
        if gen == self._stream_gen and cb is not None:
            cb(frame)

    def _forward_state(self, gen: int, state: StreamState, reason: str | None) -> None:
        cb = self._on_state
        if gen != self._stream_gen:
            return
        if self._transport == "relay" and getattr(state, "name", "") == "FAILED":
            self._release_relay_slot()  # e.g. camera offline: give the slot to another remote tile
        if cb is not None:
            cb(state, reason)

    # ------------------------------------------------------------------ SD-card playback (Phase 5)
    def playback_transport(self) -> str:
        """Transport the next ``start_playback`` uses: ``"relay"`` for cloud-routed cameras with a signed-in
        provider, else ``"local"`` (:8800)."""
        return "relay" if self._remote_provider() is not None else "local"

    def _unreachable_reason(self, provider: Any) -> str:
        from tapo_desktop.services.remote import CAMERA_OFFLINE, NETWORK_ONLY, SHARED_CLOUD_ONLY

        if provider is not None:
            return NETWORK_ONLY  # signed in, but the provider cannot play recordings
        if self._cloud_offline():
            return CAMERA_OFFLINE
        if self.config.source == "cloud" and self.config.role != 0:
            return SHARED_CLOUD_ONLY
        return NOT_ON_NETWORK

    def start_playback(self, start_utc: float, end_utc: float, width: int, height: int,
                       on_frame: Callable[[Frame], None], on_state: Callable[[StreamState, str | None], None],
                       on_position: Callable[[float], None], *, segments: list | None = None) -> None:
        """Play recordings like the app: owned cameras on this LAN over :8800
        (:class:`~tapo_desktop.core.media_engine.playback_stream.PlaybackStreamSupervisor`, cloud password or
        local access token, same limiter key as the live view: live view + playback = the camera's two media
        sessions); remote / shared cameras through the relay (``provider.create_playback``; no remote
        live-view gate slot is taken). Unreachable cameras report FAILED with the reason at once."""
        from tapo_desktop.core.media_engine.supervisor import StreamState

        self._check_open()
        self.stop_playback()
        kwargs: dict[str, Any] = {"start_utc": float(start_utc), "end_utc": float(end_utc),
                                  "segments": list(segments or [])}
        make: Callable[..., Any] | None = None
        if self._cloud_routed() or not self._on_network():
            provider = self._remote_provider()
            make = getattr(provider, "create_playback", None) if provider is not None else None
            if not callable(make):
                on_state(StreamState.FAILED, self._unreachable_reason(provider))
                return
        with self._lock:
            self._check_open()
            self._playback_gen += 1
            gen = self._playback_gen
            self._playback_callbacks = (on_frame, on_state, on_position)
            callbacks = dict(on_frame=lambda f: self._playback_frame(gen, f),
                             on_state=lambda s, r: self._playback_state(gen, s, r),
                             on_position=lambda u: self._playback_position(gen, u), limiter=self._limiter)
            if make is not None:
                sup = make(self.config, int(width), int(height), limiter_key=self.config.device_id or self.config.id,
                           **kwargs, **callbacks)
            else:
                factory = self._playback_factory
                if factory is None:
                    from tapo_desktop.core.media_engine.playback_stream import PlaybackStreamSupervisor as factory
                cloud = self._creds(CredentialKind.CLOUD)
                sup = factory(self.config.host, cloud.password, int(width), int(height), port=self.config.media_port,
                              limiter_key=self.config.host, local_token=self._local_token(), **kwargs, **callbacks)
            self._playback = sup
        sup.start()

    def _running_playback(self) -> Any:
        with self._lock:
            return self._playback

    def pause_playback(self) -> None:
        sup = self._running_playback()
        if sup is not None:
            sup.pause()

    def resume_playback(self) -> None:
        sup = self._running_playback()
        if sup is not None:
            sup.resume()

    def seek_playback(self, utc: float) -> None:
        sup = self._running_playback()
        if sup is not None:
            sup.seek(float(utc))

    def set_playback_speed(self, scale: str) -> None:
        if scale not in _PLAYBACK_SPEEDS:
            raise ValueError(f"playback speed must be one of {list(_PLAYBACK_SPEEDS)}, not {scale!r}")
        sup = self._running_playback()
        if sup is not None:
            sup.set_speed(scale)

    def stop_playback(self) -> None:
        with self._lock:
            sup, self._playback = self._playback, None
            self._playback_gen += 1  # the stopped session's callbacks are muted
            self._playback_callbacks = None
        if sup is not None:
            sup.stop()

    def _playback_frame(self, gen: int, frame: Frame) -> None:
        cbs = self._playback_callbacks
        if gen == self._playback_gen and cbs is not None:
            cbs[0](frame)

    def _playback_state(self, gen: int, state: StreamState, reason: str | None) -> None:
        cbs = self._playback_callbacks
        if gen != self._playback_gen or cbs is None:
            return
        if getattr(state, "name", "") == "FAILED" and reason and "limit" in reason.lower():
            reason = PLAYBACK_LIMIT_MESSAGE
        cbs[1](state, reason)

    def _playback_position(self, gen: int, utc: float) -> None:
        cbs = self._playback_callbacks
        if gen == self._playback_gen and cbs is not None:
            cbs[2](utc)

    # ------------------------------------------------------------------ motion events
    def start_events(self, on_event: Callable[[MotionEvent], None]) -> None:
        """ONVIF pull-point events when a camera account exists; otherwise poll
        ``getLastAlarmInfo`` every ``alarm_poll_interval`` seconds [spec:events.last_alarm_info]
        (the Tapo app's LAN behaviour: it needs only the TP-Link ID password). Cloud-routed (remote /
        shared) cameras always poll through the cloud passthrough, at most every
        :data:`REMOTE_ALARM_POLL_INTERVAL_S` seconds (one cloud call per poll)."""
        with self._lock:
            self._check_open()
            self._on_event = on_event
            if self._subscriber is not None or self._poller is not None:
                return
        if self._cloud_routed():  # ONVIF needs the LAN; poll through the cloud, but slowly
            if self._remote_control() is None:
                self._check_on_network()  # raises the reason
            self._start_poller(max(self._alarm_poll_interval, REMOTE_ALARM_POLL_INTERVAL_S))
            return
        if not self._has_camera_account():
            self._check_on_network()
            self._start_poller(self._alarm_poll_interval)
            return
        from tapo_desktop.core.onvif_service.events import PullPointSubscriber
        from tapo_desktop.core.onvif_service.soap import OnvifClient

        account = self._creds(CredentialKind.CAMERA_ACCOUNT)
        client = OnvifClient(self.config.host, account.username, account.password, port=self.config.onvif_port)
        sub = PullPointSubscriber(client, self._forward_event)
        sub.start()
        with self._lock:
            self._subscriber = sub

    def _start_poller(self, interval: float) -> None:
        poller = _AlarmPoller(self, interval)
        with self._lock:
            self._check_open()
            if self._poller is not None:
                return
            self._poller = poller
        poller.start()

    def alarm_poll_interval(self) -> float:
        """Seconds between two ``getLastAlarmInfo`` polls: the running poller's, else the one the next
        :meth:`start_events` would use (at least :data:`REMOTE_ALARM_POLL_INTERVAL_S` for cloud-routed cameras)."""
        poller = self._poller
        if poller is not None:
            return poller.interval
        if self._cloud_routed():
            return max(self._alarm_poll_interval, REMOTE_ALARM_POLL_INTERVAL_S)
        return self._alarm_poll_interval

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
        for name, fn in (("stop_events", self.stop_events), ("stop_stream", self.stop_stream),
                         ("stop_playback", self.stop_playback)):
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

    @property
    def interval(self) -> float:
        return self._interval

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
