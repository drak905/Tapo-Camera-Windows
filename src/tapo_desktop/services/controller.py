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

Phase 2 additions (lane P2-app, docs/CONTRACT_PHASE2.md): ``vault`` property,
``upsert_cameras(configs, cloud)`` / ``remove_cameras(ids)`` (batch add-or-replace / remove with a
single save and ``cameras_changed``; used by AccountManager for cloud cameras) and
``latest_image(id)`` (newest delivered frame, for snapshots). ``lan_found`` and ``live_transport``
count as connection fields: changing them restarts the camera's service and visible stream.

Implementation notes (Wave 3):

* Threading: a private :class:`~tapo_desktop.services._workers.WorkerPool` of daemon threads. Each
  camera owns two serial strands: ``control`` (connect, stream, PTZ, status, presets, recordings)
  and ``io`` (downloads, which can take minutes and must not block PTZ). Both share one lock that
  guards creating/connecting the service.
* Marshalling: workers call :meth:`AppController._post` which emits a private queued signal; the
  callable then runs on the GUI thread, where every public signal is emitted.
* Frames: the newest frame per camera is kept in a slot; at most one delivery per camera is queued
  to the GUI thread at any time, so a slow GUI drops stale frames instead of queueing them.
* Unknown camera ids in operational methods are logged and ignored (UI races after a removal must
  not raise inside Qt slots); ``update_camera`` / ``remove_camera`` raise ``KeyError``.
"""
from __future__ import annotations

import dataclasses
import logging
import pathlib
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import date
from functools import partial
from typing import TYPE_CHECKING, Any

from PyQt6.QtCore import QObject, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QImage

from tapo_desktop.models import (
    CameraConfig,
    CredentialKind,
    Credentials,
    DayNightMode,
    MotionEvent,
    RecordingClip,
    StreamQuality,
)
from tapo_desktop.services._workers import Strand, WorkerPool
from tapo_desktop.services.camera_service import CameraService
from tapo_desktop.services.config_store import ConfigStore

if TYPE_CHECKING:  # pragma: no cover
    from tapo_desktop.core.auth.vault import CredentialVault

log = logging.getLogger(__name__)

#: Fields whose change makes the existing service (sessions, sockets, URLs) obsolete.
_CONNECTION_FIELDS = ("host", "https_port", "rtsp_port", "onvif_port", "media_port", "enable_onvif_events",
                      "lan_found", "live_transport", "control_protocol")
#: Upper bound for the synchronous :meth:`AppController.stop_all`.
STOP_ALL_TIMEOUT_S = 10.0
#: Minimum progress step forwarded to the GUI (the last value 1.0 is always forwarded).
_PROGRESS_STEP = 0.01


def _message(exc: BaseException) -> str:
    """Non-empty, user-presentable text for ``exc`` (core errors never contain secrets)."""
    text = str(exc).strip()
    return text or type(exc).__name__


def frame_to_qimage(frame: Any) -> QImage | None:
    """Deep-copy an rgb24 ``Frame`` into a ``QImage`` (Format_RGB888); None if the data is short."""
    width, height = int(frame.width), int(frame.height)
    data = frame.data
    stride = width * 3
    if width <= 0 or height <= 0 or len(data) < stride * height:
        log.debug("dropping malformed frame %sx%s (%d bytes)", width, height, len(data))
        return None
    if not isinstance(data, bytes):
        data = bytes(data)
    # .copy() detaches the image from ``data`` before it can be freed.
    return QImage(data, width, height, stride, QImage.Format.Format_RGB888).copy()


@dataclasses.dataclass(eq=False)
class _Camera:
    """Per-camera runtime state. ``status``/``view``/``removed`` are touched on the GUI thread only;
    ``service``/``connected``/``events_started`` are guarded by ``lock``; the frame slot by
    ``AppController._frame_lock``."""

    config: CameraConfig
    control: Strand
    io: Strand
    lock: threading.Lock = dataclasses.field(default_factory=threading.Lock)
    service: CameraService | None = None
    connected: bool = False
    events_started: bool = False
    status: dict | None = None
    view: tuple[StreamQuality, int, int] | None = None
    removed: bool = False
    latest_frame: Any = None
    frame_pending: bool = False
    last_image: QImage | None = None  # newest delivered frame (GUI thread; snapshots)

    @property
    def id(self) -> str:
        return self.config.id


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

    _invoke = pyqtSignal(object)                      # private: run a callable on the GUI thread

    def __init__(self, config_store: ConfigStore, vault: CredentialVault,
                 service_factory: Callable[[CameraConfig], CameraService] | None = None, parent=None):
        super().__init__(parent)
        self._store = config_store
        self._vault = vault
        self._factory = service_factory or self._default_factory
        self._factory_lock = threading.Lock()
        self._limiter: Any = None
        self._pin_store: Any = None
        self._pool = WorkerPool(name="tapo-ctrl")
        self._frame_lock = threading.Lock()
        self._cams: dict[str, _Camera] = {}
        for cfg in config_store.load():
            if cfg.id in self._cams:
                log.warning("duplicate camera id in config, ignoring the second entry")
                continue
            self._cams[cfg.id] = self._new_camera(cfg)
        self._invoke.connect(self._run_posted, Qt.ConnectionType.QueuedConnection)

    # ================================================================== configuration
    @property
    def config_store(self) -> ConfigStore:
        """The ConfigStore given to ``__init__`` (settings access for the UI)."""
        return self._store

    def cameras(self) -> list[CameraConfig]:
        """Configured cameras in display order."""
        return [c.config for c in self._cams.values()]

    def camera(self, camera_id: str) -> CameraConfig | None:
        """Config of ``camera_id`` or None."""
        cam = self._cams.get(camera_id)
        return cam.config if cam is not None else None

    def add_camera(self, config: CameraConfig, cloud: Credentials, camera_account: Credentials) -> None:
        """Store credentials, persist the config and emit ``cameras_changed``."""
        if config.id in self._cams:
            raise ValueError(f"camera id {config.id!r} already exists")
        self._vault.set(config.id, CredentialKind.CLOUD, cloud)
        self._vault.set(config.id, CredentialKind.CAMERA_ACCOUNT, camera_account)
        self._cams[config.id] = self._new_camera(config)
        try:
            self._save()
        except Exception:
            del self._cams[config.id]
            self._vault.delete(config.id)
            raise
        self.cameras_changed.emit()

    def update_camera(self, config: CameraConfig, cloud: Credentials | None = None,
                      camera_account: Credentials | None = None) -> None:
        """Replace the config with the same id; ``None`` credentials stay unchanged."""
        cam = self._cams.get(config.id)
        if cam is None:
            raise KeyError(config.id)
        old = cam.config
        cam.config = config
        self._save()
        if cloud is not None:
            self._vault.set(config.id, CredentialKind.CLOUD, cloud)
        if camera_account is not None:
            self._vault.set(config.id, CredentialKind.CAMERA_ACCOUNT, camera_account)
        changed = any(getattr(old, f) != getattr(config, f) for f in _CONNECTION_FIELDS)
        if changed or cloud is not None or camera_account is not None:
            cam.control.submit(self._guarded(cam, "close", partial(self._close_service, cam)))
            if cam.view is not None:  # keep the tile streaming with the new settings
                self._submit_start(cam, *cam.view)
        self.cameras_changed.emit()

    def remove_camera(self, camera_id: str) -> None:
        """Stop and close the camera's service, forget its credentials and config."""
        if camera_id not in self._cams:
            raise KeyError(camera_id)
        self._detach(camera_id)
        self._save()
        self.cameras_changed.emit()

    # ---------------------------------------------------------- Phase 2 (lane P2-app) additions
    @property
    def vault(self) -> CredentialVault:
        """The credential vault given to ``__init__`` (AccountManager stores the account secret)."""
        return self._vault

    def upsert_cameras(self, configs: list[CameraConfig], cloud: Credentials | None = None,
                       local_tokens: dict[str, str | None] | None = None) -> None:
        """Add or replace several cameras at once (cloud camera sync); one save, one
        ``cameras_changed``.

        New cameras are appended in the given order. ``cloud`` (if given) becomes every camera's
        ``CredentialKind.CLOUD`` entry. An existing camera whose connection fields or credentials
        change gets a fresh service and, if it is on screen, its stream restarted.
        """
        restart: list[_Camera] = []
        added: list[str] = []
        previous: dict[str, CameraConfig] = {}
        for config in configs:
            creds_changed = False
            if cloud is not None and self._vault.get(config.id, CredentialKind.CLOUD) != cloud:
                self._vault.set(config.id, CredentialKind.CLOUD, cloud)
                creds_changed = True
            cam = self._cams.get(config.id)
            if cam is None:
                self._cams[config.id] = self._new_camera(config)
                added.append(config.id)
                continue
            previous.setdefault(config.id, cam.config)
            old, cam.config = cam.config, config
            if creds_changed or any(getattr(old, f) != getattr(config, f) for f in _CONNECTION_FIELDS):
                restart.append(cam)
        try:
            self._save()
        except Exception:
            for cid in added:
                del self._cams[cid]
            for cid, old in previous.items():
                self._cams[cid].config = old
            raise
        for cam in restart:
            cam.control.submit(self._guarded(cam, "close", partial(self._close_service, cam)))
            if cam.view is not None:
                self._submit_start(cam, *cam.view)
        self.cameras_changed.emit()

    def remove_cameras(self, camera_ids: list[str]) -> None:
        """Remove several cameras (streams stopped, credentials forgotten); unknown ids are ignored.
        One save and one ``cameras_changed`` (only if something was removed)."""
        removed = [cid for cid in camera_ids if cid in self._cams]
        if not removed:
            return
        for cid in removed:
            self._detach(cid)
        self._save()
        self.cameras_changed.emit()

    def latest_image(self, camera_id: str) -> QImage | None:
        """The newest frame delivered for ``camera_id`` (for snapshots), or None."""
        cam = self._cams.get(camera_id)
        return cam.last_image if cam is not None else None

    def uses_local_token(self, camera_id: str) -> bool:
        """True if a local access token is stored for ``camera_id`` (the token itself is never exposed)."""
        raise NotImplementedError

    def _detach(self, camera_id: str) -> None:
        cam = self._cams.pop(camera_id)
        cam.removed = True
        cam.view = None
        cam.last_image = None
        cam.control.submit(self._guarded(cam, "remove", partial(self._close_service, cam, stop_first=True)))
        self._vault.delete(camera_id)

    # ================================================================== streaming
    def start_view(self, camera_id: str, quality: StreamQuality, width: int, height: int) -> None:
        """Start (or switch) the live stream of ``camera_id``; non-blocking."""
        cam = self._cam(camera_id)
        if cam is None:
            return
        cam.view = (quality, int(width), int(height))
        self._submit_start(cam, *cam.view)

    def stop_view(self, camera_id: str) -> None:
        """Stop the live stream of ``camera_id``; non-blocking."""
        cam = self._cam(camera_id)
        if cam is None:
            return
        cam.view = None
        self._submit(cam, "stop_view", lambda svc: svc.stop_stream(), connect=False,
                     on_error=lambda _msg: None)  # already logged; nothing for the UI to roll back

    def stop_all(self) -> None:
        """Stops streams + events of every camera; synchronous; used on quit."""
        futures: list[Future] = []
        for cam in list(self._cams.values()):
            cam.view = None
            futures.append(cam.control.submit(self._guarded(cam, "stop_all", partial(self._stop_service, cam))))
        deadline = time.monotonic() + STOP_ALL_TIMEOUT_S
        for fut in futures:
            try:
                fut.result(timeout=max(0.0, deadline - time.monotonic()))
            except FutureTimeout:
                log.warning("stop_all: a camera did not stop within %.0f s", STOP_ALL_TIMEOUT_S)
                break

    # ================================================================== status / toggles
    def refresh_status(self, camera_id: str) -> None:
        """Fetch the status off-thread and emit ``status_changed``."""
        cam = self._cam(camera_id)
        if cam is None:
            return
        self._submit(cam, "refresh_status", lambda svc: svc.get_status(),
                     on_ok=partial(self._status_ready, cam))

    def set_privacy(self, camera_id: str, on: bool) -> None:
        self._set_status(camera_id, "set_privacy", "privacy", bool(on))

    def set_day_night(self, camera_id: str, mode: DayNightMode) -> None:
        self._set_status(camera_id, "set_day_night", "day_night", DayNightMode(mode))

    def set_led(self, camera_id: str, on: bool) -> None:
        self._set_status(camera_id, "set_led", "led", bool(on))

    def set_alarm(self, camera_id: str, on: bool) -> None:
        self._set_status(camera_id, "set_alarm", "alarm", bool(on))

    def set_motion_detection(self, camera_id: str, on: bool) -> None:
        self._set_status(camera_id, "set_motion_detection", "motion_detection", bool(on))

    # ================================================================== PTZ / presets
    def ptz_move(self, camera_id: str, dx: int, dy: int) -> None:
        """Continuous move (dx > 0 right, dy > 0 up) until :meth:`ptz_stop`."""
        cam = self._cam(camera_id)
        if cam is not None:
            self._submit(cam, "ptz_move", lambda svc: svc.ptz_move(int(dx), int(dy)))

    def ptz_stop(self, camera_id: str) -> None:
        cam = self._cam(camera_id)
        if cam is not None:
            self._submit(cam, "ptz_stop", lambda svc: svc.ptz_stop())

    def load_presets(self, camera_id: str) -> None:
        """Fetch presets off-thread and emit ``presets_ready`` (lane addition)."""
        cam = self._cam(camera_id)
        if cam is not None:
            self._submit(cam, "load_presets", lambda svc: list(svc.get_presets()),
                         on_ok=lambda presets: self.presets_ready.emit(cam.id, presets))

    def goto_preset(self, camera_id: str, preset_id: str) -> None:
        cam = self._cam(camera_id)
        if cam is not None:
            self._submit(cam, "goto_preset", lambda svc: svc.goto_preset(str(preset_id)))

    # ================================================================== recordings
    def load_recordings(self, camera_id: str, day: date) -> None:
        """Fetch the clips of ``day`` off-thread and emit ``recordings_ready``."""
        cam = self._cam(camera_id)
        if cam is not None:
            self._submit(cam, "load_recordings", lambda svc: list(svc.recordings_for_day(day)),
                         on_ok=lambda clips: self.recordings_ready.emit(cam.id, day, clips))

    def download_recording(self, camera_id: str, clip: RecordingClip, out_path: pathlib.Path) -> None:
        """Download ``clip`` on the camera's I/O strand; reports via download_progress/finished."""
        cam = self._cam(camera_id)
        if cam is None:
            return
        cam.io.submit(self._guarded(cam, "download_recording",
                                    partial(self._download_job, cam, clip, pathlib.Path(out_path))))

    def wait_idle(self, timeout_ms: int = 5000) -> bool:
        """Test helper: block until no background job is queued/running (signals already queued to
        the GUI thread may still need ``processEvents``). Returns False on timeout."""
        return self._pool.wait_idle(max(0, timeout_ms) / 1000.0)

    # ================================================================== internals: plumbing
    def _new_camera(self, config: CameraConfig) -> _Camera:
        return _Camera(config=config, control=Strand(self._pool, f"{config.id}/control"),
                       io=Strand(self._pool, f"{config.id}/io"))

    def _cam(self, camera_id: str) -> _Camera | None:
        cam = self._cams.get(camera_id)
        if cam is None:
            log.warning("ignoring request for unknown camera id %r", camera_id)
        return cam

    def _save(self) -> None:
        self._store.save(self.cameras())

    def _default_factory(self, config: CameraConfig) -> CameraService:
        """Real wiring; core modules are imported lazily so the UI imports with core stubbed."""
        from tapo_desktop.services.camera_service import TapoCameraService

        with self._factory_lock:
            if self._limiter is None:
                from tapo_desktop.core.media_engine.limiter import StreamLimiter

                self._limiter = StreamLimiter()
            if self._pin_store is None:
                from tapo_desktop.core.control_client.transport import PinStore

                self._pin_store = PinStore(self._store.path.parent / "pins.json")
        return TapoCameraService(config, self._vault, limiter=self._limiter, pin_store=self._pin_store)

    def _post(self, fn: Callable[[], None]) -> None:
        """Run ``fn`` on the GUI thread (callable from any thread)."""
        try:
            self._invoke.emit(fn)
        except RuntimeError:  # the controller's C++ object is gone (application shutting down)
            log.debug("controller deleted; dropping a GUI callback")

    @pyqtSlot(object)
    def _run_posted(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:
            log.exception("error in a controller GUI callback")

    @staticmethod
    def _guarded(cam: _Camera, what: str, fn: Callable[[], None]) -> Callable[[], None]:
        """Wrap a job whose failure has no dedicated signal: log it instead of losing it."""
        def job() -> None:
            try:
                fn()
            except Exception as exc:
                log.warning("%s failed for camera %s: %s", what, cam.id, _message(exc))
        return job

    def _service(self, cam: _Camera, *, connect: bool = True) -> CameraService | None:
        """Return the camera's service, creating and connecting it on first use (worker thread).

        With ``connect=False`` an existing service is returned as is and none is created.
        """
        with cam.lock:
            if cam.service is None:
                if not connect:
                    return None
                cam.service = self._factory(cam.config)
            svc = cam.service
            if connect and not cam.connected:
                svc.connect()
                cam.connected = True
                if cam.config.enable_onvif_events and not cam.events_started:
                    try:
                        svc.start_events(partial(self._on_event, cam))
                        cam.events_started = True
                    except Exception as exc:  # events are optional; streaming/control keep working
                        log.warning("motion events unavailable for camera %s: %s", cam.id, _message(exc))
            return svc

    def _submit(self, cam: _Camera, action: str, call: Callable[[CameraService], Any], *,
                on_ok: Callable[[Any], None] | None = None,
                on_error: Callable[[str], None] | None = None, connect: bool = True) -> Future:
        """Run ``call(service)`` on the camera's control strand.

        ``on_ok(result)`` / ``on_error(message)`` run on the GUI thread; without ``on_error`` a
        failure emits ``command_failed(id, action, message)``.
        """
        def job() -> None:
            try:
                svc = self._service(cam, connect=connect)
                if svc is None:
                    return
                result = call(svc)
            except Exception as exc:
                msg = _message(exc)
                log.warning("%s failed for camera %s: %s", action, cam.id, msg)
                if on_error is not None:
                    self._post(lambda: on_error(msg))
                else:
                    self._post(lambda: self.command_failed.emit(cam.id, action, msg))
                return
            if on_ok is not None:
                self._post(lambda: on_ok(result))
        return cam.control.submit(job)

    def _stop_service(self, cam: _Camera) -> None:
        """Stop stream + events of an existing service (worker thread). Errors are logged."""
        with cam.lock:
            svc, events = cam.service, cam.events_started
            cam.events_started = False
        if svc is None:
            return
        calls = [("stop_stream", svc.stop_stream)] + ([("stop_events", svc.stop_events)] if events else [])
        for name, fn in calls:
            try:
                fn()
            except Exception as exc:
                log.warning("%s failed for camera %s: %s", name, cam.id, _message(exc))

    def _close_service(self, cam: _Camera, *, stop_first: bool = False) -> None:
        """Detach and close the camera's service (worker thread); the next job creates a new one."""
        if stop_first:
            self._stop_service(cam)
        with cam.lock:
            svc, cam.service = cam.service, None
            cam.connected = cam.events_started = False
        if svc is not None:
            svc.close()

    # ================================================================== internals: streaming
    def _submit_start(self, cam: _Camera, quality: StreamQuality, width: int, height: int) -> None:
        def fail(msg: str) -> None:
            self.stream_state.emit(cam.id, "FAILED", msg)

        self._submit(cam, "start_view",
                     lambda svc: svc.start_stream(quality, width, height, partial(self._on_frame, cam),
                                                  partial(self._on_state, cam)),
                     on_error=fail)

    def _on_frame(self, cam: _Camera, frame: Any) -> None:
        """Stream worker thread: keep only the newest frame; queue one delivery at most."""
        with self._frame_lock:
            cam.latest_frame = frame
            if cam.frame_pending:
                return
            cam.frame_pending = True
        self._post(partial(self._deliver_frame, cam))

    def _deliver_frame(self, cam: _Camera) -> None:
        with self._frame_lock:
            frame, cam.latest_frame = cam.latest_frame, None
            cam.frame_pending = False
        if frame is None or cam.removed:
            return
        image = frame_to_qimage(frame)
        if image is not None:
            cam.last_image = image
            self.frame_ready.emit(cam.id, image)

    def _on_state(self, cam: _Camera, state: Any, reason: str | None) -> None:
        name = getattr(state, "name", str(state))
        text = reason or ""
        self._post(lambda: self.stream_state.emit(cam.id, name, text))

    def _on_event(self, cam: _Camera, event: MotionEvent) -> None:
        self._post(lambda: self.motion_event.emit(cam.id, event))

    # ================================================================== internals: status
    def _status_ready(self, cam: _Camera, status: dict) -> None:
        cam.status = dict(status)
        self.status_changed.emit(cam.id, dict(cam.status))

    def _set_status(self, camera_id: str, action: str, key: str, value: Any) -> None:
        """Optimistic toggle: emit the new status now, roll back on failure (GUI thread)."""
        cam = self._cam(camera_id)
        if cam is None:
            return
        primed = cam.status is not None
        old = cam.status.get(key) if cam.status is not None else None
        if cam.status is not None:
            cam.status = {**cam.status, key: value}
            self.status_changed.emit(cam.id, dict(cam.status))

        def rollback(msg: str) -> None:
            self.command_failed.emit(cam.id, action, msg)
            if primed and cam.status is not None:
                if cam.status.get(key) == value:  # a newer change wins over this rollback
                    cam.status = {**cam.status, key: old}
                self.status_changed.emit(cam.id, dict(cam.status))
            else:
                self.refresh_status(cam.id)

        self._submit(cam, action, lambda svc: getattr(svc, action)(value), on_error=rollback)

    # ================================================================== internals: downloads
    def _download_job(self, cam: _Camera, clip: RecordingClip, out_path: pathlib.Path) -> None:
        """I/O strand: download with throttled, non-decreasing progress."""
        lock = threading.Lock()
        emitted = [-1.0]

        def report(value: float) -> None:
            v = min(1.0, max(0.0, float(value)))
            with lock:
                if v <= emitted[0] or (v < 1.0 and v - emitted[0] < _PROGRESS_STEP):
                    return
                emitted[0] = v
            self._post(lambda: self.download_progress.emit(cam.id, v))

        try:
            svc = self._service(cam, connect=True)
            if svc is None:  # pragma: no cover - connect=True always yields a service
                raise RuntimeError("camera service unavailable")
            result = svc.download_recording(clip, out_path, report)
        except Exception as exc:
            msg = _message(exc)
            log.warning("download failed for camera %s: %s", cam.id, msg)
            self._post(lambda: self.download_finished.emit(cam.id, "", msg))
            return
        report(1.0)
        path = str(result if result is not None else out_path)
        self._post(lambda: self.download_finished.emit(cam.id, path, ""))
