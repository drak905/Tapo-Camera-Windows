"""iMou (Open Platform) connection of Tapo Desktop: connect (Test), camera list and online state, disconnect. No UI.

Sits next to the Tapo account manager and shares nothing with it. It only touches cameras with ``source == "imou"``
(id ``imou:<serial>``, ``host=""``) and the vault entry ``IMOU_APP_VAULT_ID`` / ``CredentialKind.IMOU_APP``, so a Tapo
or other-vendor sign-in, refresh or sign-out can not affect it, and the reverse.

Safety and cost rules (the iMou free tier is small, and a wrong key can only be fixed by the owner):

* ``test`` makes ONE ``accessToken`` call and ONE device-list call and nothing else. The App ID / App secret are
  saved (DPAPI vault only) only after that succeeded. A failed Test stores nothing.
* No polling, no timer, no call at start-up: the list is read on ``test`` and on a manual ``refresh``. ``restore``
  only rebuilds the in-memory state from the vault (no network); the first click on a tile does the token call.
* After a signature error (SN1001...) nothing is sent for 120 s and there is no silent retry: ``restore`` does not
  reconnect until a Test succeeds again. After a quota error nothing is sent until the next UTC day.
  Both live in one :class:`~tapo_desktop.core.imou.guard.ImouGuard` whose state is saved in ``config.json``.
* ``config.json`` keeps only non-secret state: the data center, a masked label, the limits, the guard state and the
  local minute meter. Never the App ID, the App secret, a token or a live address. Logs carry class names only.

Threading: network work runs on a private one-worker pool; results are applied on the GUI thread, where every signal
is emitted. ``disconnect`` invalidates work in flight (a generation counter).
"""
from __future__ import annotations

import dataclasses
import logging
import math
import threading
import time
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QCoreApplication, QObject, QThread, Qt, pyqtSignal, pyqtSlot

from tapo_desktop.core.imou import regions
from tapo_desktop.core.imou.client import ImouClient
from tapo_desktop.core.imou.errors import ImouAuthError, ImouBlockedError, ImouError
from tapo_desktop.core.imou.guard import ImouGuard
from tapo_desktop.core.imou.models import ImouDevice
from tapo_desktop.core.imou.runtime import ImouLimits, ImouRuntime, UsageMeter
from tapo_desktop.models import IMOU_APP_VAULT_ID, CameraConfig, CredentialKind, Credentials
from tapo_desktop.services import imou_camera_service
from tapo_desktop.services._workers import WorkerPool

log = logging.getLogger(__name__)

SETTING_ACCOUNT = "imou_account"            # {"label": masked App ID, "data_center": id} while connected
SETTING_DATA_CENTER = "imou_data_center"    # last data center chosen in the dialog (not secret)
SETTING_LAST_FAILED = "imou_last_failed"    # True after a refused key: no reconnect at start-up until a Test works
SETTING_GUARD = "imou_guard"                # saved ImouGuard state
SETTING_USAGE = "imou_usage"                # saved UsageMeter state
SETTING_DAILY_MINUTES = "imou_daily_minutes"
SETTING_MAX_LIVE = "imou_max_live"
DEFAULT_DAILY_MINUTES = 20
DEFAULT_MAX_LIVE = 2
MAX_DAILY_MINUTES = 600
MAX_LIVE_CAP = 4

MSG_BLOCKED = "iMou: {text} Try again in {wait}."
MSG_BUSY = "An iMou test is already running."
MSG_EMPTY = "Enter the App ID and the App secret of your iMou Open Platform app."
MSG_DATA_CENTER = "Choose the data center of your iMou app from the list."
MSG_UNEXPECTED = "iMou: unexpected error (see the log for the error class)."
MSG_NOT_CONNECTED = "iMou: not connected. Use the account button, iMou connection."

ClientFactory = Callable[[str, str, str, ImouGuard, Callable[[], float]], Any]


def mask_app_id(app_id: str) -> str:
    """``lcdabcdef123`` -> ``lcd***`` (what the UI shows and the settings keep)."""
    return f"{app_id.strip()[:3]}***"


def _default_client_factory(app_id: str, secret: str, data_center: str, guard: ImouGuard,
                            clock: Callable[[], float]) -> ImouClient:
    return ImouClient(app_id, secret, data_center, guard=guard, clock=clock)


def _wait_text(seconds: float) -> str:
    seconds = max(1, round(seconds))
    if seconds < 120:
        return f"{seconds} s"
    return f"{round(seconds / 60)} min" if seconds < 7200 else f"{round(seconds / 3600)} h"


def _clamped(value: object, default: int, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return default
    return max(low, min(high, int(value)))


class ImouAccountManager(QObject):
    """Connected iMou app and its cameras; see the module docstring."""

    connected = pyqtSignal(str)         # masked App ID
    connect_failed = pyqtSignal(str)    # user-readable message (Test, Refresh)
    disconnected = pyqtSignal()
    cameras_changed = pyqtSignal()      # the iMou camera list was updated

    _invoke = pyqtSignal(object)        # private: run a callable on the GUI thread

    def __init__(self, controller, vault, config_store, client_factory: ClientFactory | None = None, *,
                 clock: Callable[[], float] | None = None, supervisor_factory: Callable[..., Any] | None = None,
                 parent: QObject | None = None):
        super().__init__(parent)
        self._ctrl = controller
        self._vault = vault
        self._store = config_store
        self._factory = client_factory or _default_client_factory
        self._clock = clock or time.time
        self._sup_factory = supervisor_factory
        self._pool = WorkerPool(max_workers=1, name="imou-account")
        self._posts_lock = threading.Lock()
        self._posts = 0
        self._gen = 0
        self._busy = False
        self._runtime: ImouRuntime | None = None
        self._label = ""
        self._guard = ImouGuard(self._clock, load=lambda: self._store.get_setting(SETTING_GUARD, None),
                                save=lambda state: self._store.set_setting(SETTING_GUARD, state))
        self._meter = UsageMeter(self._clock, load=lambda: self._store.get_setting(SETTING_USAGE, None),
                                 save=lambda state: self._store.set_setting(SETTING_USAGE, state))
        self._invoke.connect(self._run_posted, Qt.ConnectionType.QueuedConnection)

    def __repr__(self) -> str:
        return f"ImouAccountManager(connected={self._runtime is not None})"

    # ================================================================== public API
    def is_connected(self) -> bool:
        return self._runtime is not None

    def account_label(self) -> str:
        return self._label if self._runtime is not None else ""

    def data_center(self) -> str | None:
        value = self._store.get_setting(SETTING_DATA_CENTER, None)
        return value if isinstance(value, str) and value in regions.DATA_CENTERS else None

    def blocked_until(self) -> float | None:
        """Epoch time until which no iMou call is sent (quota used, or a refused key), else ``None``."""
        state = self._guard.blocked()
        return state[0] if state is not None else None

    def daily_minutes(self) -> int:
        return _clamped(self._store.get_setting(SETTING_DAILY_MINUTES, None), DEFAULT_DAILY_MINUTES, 1,
                        MAX_DAILY_MINUTES)

    def max_live(self) -> int:
        return _clamped(self._store.get_setting(SETTING_MAX_LIVE, None), DEFAULT_MAX_LIVE, 1, MAX_LIVE_CAP)

    def set_limits(self, daily_minutes: int, max_live: int) -> None:
        """Save the daily minutes per camera (1..600) and the live streams at once (1..4); read at the next click."""
        self._store.set_setting(SETTING_DAILY_MINUTES, _clamped(daily_minutes, 1, 1, MAX_DAILY_MINUTES))
        self._store.set_setting(SETTING_MAX_LIVE, _clamped(max_live, 1, 1, MAX_LIVE_CAP))

    def usage_minutes_today(self) -> float:
        """Live minutes of all iMou cameras today (UTC day): a local estimate, not the vendor's own meter."""
        return round(self._meter.total() / 60.0, 1)

    def limits(self) -> ImouLimits:
        return dataclasses.replace(ImouLimits(), daily_seconds=float(self.daily_minutes() * 60),
                                   max_live=self.max_live())

    def test(self, app_id: str, secret: str, data_center: str) -> None:
        """One token call + one device-list call, off the UI thread. Ends with ``connected`` or ``connect_failed``."""
        app_id, secret = (app_id or "").strip(), secret or ""
        if self._busy:
            return self._refuse(MSG_BUSY)
        if not app_id or not secret:
            return self._refuse(MSG_EMPTY)
        if data_center not in regions.DATA_CENTERS:
            return self._refuse(MSG_DATA_CENTER)
        state = self._guard.blocked()
        if state is not None:
            return self._refuse(MSG_BLOCKED.format(text=state[1], wait=_wait_text(state[0] - self._clock())))
        self._busy = True
        gen = self._gen
        self._store.set_setting(SETTING_DATA_CENTER, data_center)

        def job() -> None:
            try:
                client = self._factory(app_id, secret, data_center, self._guard, self._clock)
                devices = client.list_devices()  # token call + list call, nothing else
            except Exception as exc:
                self._post(lambda exc=exc: self._test_failed(gen, exc))
                return
            self._post(lambda: self._test_ok(gen, client, app_id, secret, data_center, devices))

        self._submit(job)

    def refresh(self) -> None:
        """Reload the camera list and the online state: one list call (plus a token call when none is held)."""
        runtime, gen = self._runtime, self._gen
        if runtime is None or self._busy:
            return
        self._busy = True

        def job() -> None:
            try:
                devices = runtime.client.list_devices()
            except Exception as exc:
                self._post(lambda exc=exc: self._refresh_failed(gen, exc))
                return
            self._post(lambda: self._refreshed(gen, devices))

        self._submit(job)

    def disconnect(self) -> None:
        """Forget the key and remove the iMou cameras (streams stop). Tapo and other cameras and vaults stay."""
        self._gen += 1
        self._busy = False
        self._runtime = None
        self._label = ""
        imou_camera_service.set_runtime(None)
        ids = [c.id for c in self._ctrl.cameras() if c.source == "imou"]
        if ids:
            self._ctrl.remove_cameras(ids)
            self.cameras_changed.emit()
        self._vault.delete(IMOU_APP_VAULT_ID)
        self._store.set_setting(SETTING_ACCOUNT, None)
        self._store.set_setting(SETTING_LAST_FAILED, False)
        self.disconnected.emit()

    def restore(self) -> None:
        """At start: rebuild the in-memory connection from the vault. No network call, ever."""
        if self._runtime is not None:
            return
        if self._store.get_setting(SETTING_LAST_FAILED, False):
            return  # a refused key: no silent reconnect until a Test succeeds
        entry = self._vault.get(IMOU_APP_VAULT_ID, CredentialKind.IMOU_APP)
        data_center = self.data_center()
        if entry is None or data_center is None:
            return
        try:
            client = self._factory(entry.username, entry.password, data_center, self._guard, self._clock)
        except ValueError:
            log.warning("imou: the saved data center is not valid; ignoring the saved connection")
            return
        self._set_runtime(client, mask_app_id(entry.username))
        self.connected.emit(self._label)

    def wait_idle(self, timeout_ms: int = 5000) -> bool:
        """Block until no job runs and every result has been applied (pumps events on the GUI thread)."""
        deadline = time.monotonic() + max(0, timeout_ms) / 1000.0
        on_gui = QThread.currentThread() is self.thread()
        while True:
            if not self._pool.wait_idle(max(0.0, deadline - time.monotonic())):
                return False
            with self._posts_lock:
                posts = self._posts
            if posts == 0 and self._pool.wait_idle(0):
                return True
            if time.monotonic() >= deadline:
                return False
            if on_gui:
                QCoreApplication.processEvents()
            else:
                time.sleep(0.005)

    # ================================================================== worker plumbing
    def _submit(self, job: Callable[[], None]) -> None:
        def guarded() -> None:
            try:
                job()
            except Exception as exc:  # class name only: a message could carry request data
                log.error("imou background job failed (%s)", type(exc).__name__)

        self._pool.submit(guarded)

    def _post(self, fn: Callable[[], None]) -> None:
        with self._posts_lock:
            self._posts += 1
        try:
            self._invoke.emit(fn)
        except RuntimeError:  # the QObject is gone (application shutting down)
            with self._posts_lock:
                self._posts -= 1

    @pyqtSlot(object)
    def _run_posted(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception as exc:
            log.error("imou: error while applying a result (%s)", type(exc).__name__)
        finally:
            with self._posts_lock:
                self._posts -= 1

    def _refuse(self, message: str) -> None:
        self._post(lambda: self.connect_failed.emit(message))

    # ================================================================== GUI side
    def _message(self, exc: BaseException) -> str:
        if isinstance(exc, ImouBlockedError):
            return f"{exc} Try again in {_wait_text(exc.until - self._clock())}." if exc.until else str(exc)
        if isinstance(exc, ImouError):
            return str(exc)
        return MSG_UNEXPECTED

    def _set_runtime(self, client: Any, label: str) -> None:
        kwargs = {"supervisor_factory": self._sup_factory} if self._sup_factory is not None else {}
        self._runtime = ImouRuntime(client=client, guard=self._guard, clock=self._clock, limits=self.limits,
                                    meter=self._meter, **kwargs)
        self._label = label
        imou_camera_service.set_runtime(self._runtime)

    def _test_failed(self, gen: int, exc: BaseException) -> None:
        self._busy = False
        log.info("imou test failed (%s)", type(exc).__name__)
        if isinstance(exc, ImouAuthError):
            self._store.set_setting(SETTING_LAST_FAILED, True)
        if gen == self._gen:
            self.connect_failed.emit(self._message(exc))

    def _test_ok(self, gen: int, client: Any, app_id: str, secret: str, data_center: str,
                 devices: list[ImouDevice]) -> None:
        self._busy = False
        if gen != self._gen:
            return
        label = mask_app_id(app_id)
        self._vault.set(IMOU_APP_VAULT_ID, CredentialKind.IMOU_APP, Credentials(app_id, secret))
        self._store.set_setting(SETTING_ACCOUNT, {"label": label, "data_center": data_center})
        self._store.set_setting(SETTING_DATA_CENTER, data_center)
        self._store.set_setting(SETTING_LAST_FAILED, False)
        self._set_runtime(client, label)
        self._apply_devices(gen, devices, data_center)
        self.connected.emit(label)

    def _refresh_failed(self, gen: int, exc: BaseException) -> None:
        self._busy = False
        log.info("imou refresh failed (%s)", type(exc).__name__)
        if gen == self._gen:
            self.connect_failed.emit(self._message(exc))

    def _refreshed(self, gen: int, devices: list[ImouDevice]) -> None:
        self._busy = False
        self._apply_devices(gen, devices, self.data_center() or "")

    def _apply_devices(self, gen: int, devices: list[ImouDevice], data_center: str) -> None:
        """Upsert the listed cameras and drop iMou cameras that are no longer listed. Other cameras are never touched."""
        if gen != self._gen:
            return
        existing = {c.id: c for c in self._ctrl.cameras() if c.source == "imou"}
        changed: list[CameraConfig] = []
        for dev in devices:
            cid = f"imou:{dev.serial}"
            old = existing.get(cid)
            data = {"serial": dev.serial, "channel": dev.channel, "cloud_name": dev.name, "data_center": data_center}
            if old is None:
                cfg = CameraConfig(name=dev.name, host="", id=cid, source="imou", device_id=dev.serial,
                                   model=dev.model, online=dev.online, lan_found=False, enable_onvif_events=False,
                                   vendor_data=data)
            else:
                # a name changed locally (``controller.rename_camera``) differs from the last cloud name: keep it
                renamed = old.name != old.vendor_data.get("cloud_name", old.name)
                cfg = dataclasses.replace(old, name=old.name if renamed else dev.name, model=dev.model,
                                          online=dev.online, vendor_data={**old.vendor_data, **data})
            if cfg != old:
                changed.append(cfg)
        wanted = {f"imou:{d.serial}" for d in devices}
        gone = [cid for cid in existing if cid not in wanted]
        if changed:
            self._ctrl.upsert_cameras(changed, cloud=None)
        if gone:
            self._ctrl.remove_cameras(gone)
        if changed or gone:
            self.cameras_changed.emit()
