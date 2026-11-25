"""Kami Home account for Tepo Desktop (lane A): sign-in, camera list and online state. No UI, no live view.

Sits next to :class:`~tapo_desktop.services.account.AccountManager` but shares nothing with it. It only touches
cameras with ``source == "kami"`` (id ``kami:<uid>``, ``host=""``) and the vault entry ``KAMI_ACCOUNT_VAULT_ID`` /
``CredentialKind.KAMI_ACCOUNT``, so a Tapo sign-in, refresh or sign-out can not affect it and the reverse.

Safety rules (plan review finding 10; the first real login is the owner's, so a bug here could lock the account or log
the phone out):

* ``sign_in`` sends exactly ONE login request per call: no retry, no second variant. While one runs, a second click
  is refused.
* A refused login (anything but a connection problem) sets ``kami_last_sign_in_failed``; a wrong password also
  starts a 120 s block (``kami_blocked_until``, an epoch time, so a restart does not reset it).
* ``restore_session`` signs in silently ONLY with the stored token and only while ``kami_last_sign_in_failed`` is
  False. It never sends a password. A dead token ends the session and asks for a manual sign-in.
* The password is never stored. The session token and secret live in the DPAPI vault (a ``Credentials`` whose
  username is the e-mail and whose password is a small JSON record). ``config.json`` keeps only a masked label,
  the country and the region.
* Nothing logs URLs, passwords, tokens or camera data: log lines carry exception class names and result codes only.

Threading: network work runs on a private worker; results are applied on the GUI thread, where every signal is
emitted. ``sign_out`` invalidates work in flight (a generation counter), so a late result cannot sign back in.
"""
from __future__ import annotations

import dataclasses
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QCoreApplication, QObject, QThread, Qt, pyqtSignal, pyqtSlot

from tapo_desktop.core.kami.app_key import key_file_path, load_app_key, ENV_VAR as KEY_ENV_VAR
from tapo_desktop.core.kami.client import KamiClient
from tapo_desktop.core.kami.dtos import (
    KamiAuthError, KamiConnectionError, KamiExtraStepError, KamiKeyMissingError, KamiProtocolError, KamiServerError,
    KamiSession, KamiSessionExpired,
)
from tapo_desktop.core.kami.models import KamiDevice
from tapo_desktop.core.kami.regions import region_for
from tapo_desktop.models import KAMI_ACCOUNT_VAULT_ID, CameraConfig, CredentialKind, Credentials
from tapo_desktop.services._workers import WorkerPool
from tapo_desktop.services.config_store import SETTING_KAMI_ACCOUNT, SETTING_KAMI_LAST_SIGN_IN_FAILED

log = logging.getLogger(__name__)

#: Epoch seconds until which no new sign-in is sent (set after a refused login, persisted).
SETTING_KAMI_BLOCKED_UNTIL = "kami_blocked_until"
BLOCK_SECONDS = 120.0

MSG_BAD_CREDENTIALS = ("Kami did not accept this sign-in. Check the e-mail, the password and the country, "
                       "then try again in 2 minutes.")
MSG_BLOCKED = "Please wait {seconds} more seconds before signing in to Kami again."
MSG_EXTRA_STEP = ("Kami asks for an extra step (code {code}) that this app cannot do. If you sign in to Kami with "
                  "Google or Apple, set a password in the Kami app first and use that password here.")
MSG_OFFLINE = "Cannot reach the Kami servers. Check the internet connection and try again."
MSG_SERVER = "The Kami servers sent an unexpected reply. Try again later."
MSG_UNEXPECTED = "Kami sign-in failed: unexpected error (see the log for details)."
MSG_KEY_MISSING = ("The Kami password key is missing. Put it in the environment variable " + KEY_ENV_VAR +
                   " or in the file kami_app_key.txt in {folder}.")
MSG_SESSION_ENDED = "Your Kami session has ended. Sign in again."
MSG_BUSY = "A Kami sign-in is already running."
MSG_EMPTY = "Enter the e-mail and the password of your Kami account."
MSG_COUNTRY = "Choose the country of your Kami account."


def mask_email(email: str) -> str:
    """``owner@example.com`` -> ``o***@example.com`` (what the UI shows and ``signed_in`` carries)."""
    local, at, domain = email.strip().partition("@")
    return f"{local[:1]}***{at}{domain}"


def _default_client_factory(region: str, country_code: str) -> KamiClient:
    return KamiClient(region, country_code, app_key=load_app_key())


class KamiAccountManager(QObject):
    """Signed-in Kami account and its cameras; see the module docstring."""

    signed_in = pyqtSignal(str)        # masked e-mail
    sign_in_failed = pyqtSignal(str)   # user-readable message (also: restore found a dead session)
    signed_out = pyqtSignal()
    cameras_changed = pyqtSignal()     # the Kami camera list was updated

    _invoke = pyqtSignal(object)       # private: run a callable on the GUI thread

    def __init__(self, controller, vault, config_store,
                 client_factory: Callable[[str, str], Any] | None = None, *,
                 clock: Callable[[], float] | None = None, parent: QObject | None = None):
        super().__init__(parent)
        self._ctrl = controller
        self._vault = vault
        self._store = config_store
        self._factory = client_factory or _default_client_factory
        self._clock = clock or time.time
        self._pool = WorkerPool(max_workers=1, name="kami-account")
        self._posts_lock = threading.Lock()
        self._posts = 0
        self._gen = 0
        self._busy = False
        self._session: KamiSession | None = None
        self._client: Any = None
        self._label = ""
        self._invoke.connect(self._run_posted, Qt.ConnectionType.QueuedConnection)

    def __repr__(self) -> str:
        return f"KamiAccountManager(signed_in={self._session is not None})"

    # ================================================================== public API
    def is_signed_in(self) -> bool:
        return self._session is not None

    def account_label(self) -> str:
        """Masked e-mail of the signed-in account, or ``""``."""
        return self._label if self._session is not None else ""

    def blocked_until(self) -> float | None:
        """Epoch time until which a new sign-in is refused, or ``None`` when sign-in is allowed."""
        until = self._store.get_setting(SETTING_KAMI_BLOCKED_UNTIL, None)
        if isinstance(until, (int, float)) and not isinstance(until, bool) and until > self._clock():
            return float(until)
        return None

    def sign_in(self, email: str, password: str, country_code: str) -> None:
        """One sign-in attempt, off the UI thread. Ends with ``signed_in`` or ``sign_in_failed``."""
        email, country = (email or "").strip(), (country_code or "").strip().upper()
        if self._busy:
            return self._refuse(MSG_BUSY)
        if not email or not password:
            return self._refuse(MSG_EMPTY)
        try:
            region = region_for(country)
        except ValueError:
            return self._refuse(MSG_COUNTRY)
        until = self.blocked_until()
        if until is not None:
            return self._refuse(MSG_BLOCKED.format(seconds=max(1, round(until - self._clock()))))
        self._busy = True
        gen = self._gen
        previous_failed = bool(self._store.get_setting(SETTING_KAMI_LAST_SIGN_IN_FAILED, False))
        # Conservative: if the app dies while the request is in flight, no silent sign-in happens at the next start.
        self._store.set_setting(SETTING_KAMI_LAST_SIGN_IN_FAILED, True)

        def job() -> None:
            try:
                client = self._factory(region, country)
                session = client.login(email, password)  # the one and only attempt
            except Exception as exc:
                self._post(lambda exc=exc: self._login_failed(gen, exc, previous_failed))
                return
            devices = None
            try:
                devices = client.list_devices(session)
            except Exception as exc:  # signed in, but the list failed: keep the cached cameras
                log.warning("kami: signed in, camera list unavailable (%s)", type(exc).__name__)
            self._post(lambda: self._login_ok(gen, client, session, devices))

        self._submit(job)

    def sign_out(self) -> None:
        """Forget the session and remove the Kami cameras. Tapo cameras and the Tapo vault entry stay."""
        self._gen += 1
        self._session = self._client = None
        self._label = ""
        ids = [c.id for c in self._ctrl.cameras() if c.source == "kami"]
        if ids:
            self._ctrl.remove_cameras(ids)
            self.cameras_changed.emit()
        self._vault.delete(KAMI_ACCOUNT_VAULT_ID)
        self._store.set_setting(SETTING_KAMI_ACCOUNT, None)
        self._store.set_setting(SETTING_KAMI_LAST_SIGN_IN_FAILED, False)  # the 120 s block is kept on purpose
        self.signed_out.emit()

    def restore_session(self) -> None:
        """At start: use the stored token (never a password) when the last sign-in did not fail."""
        if self._session is not None or self._busy:
            return
        if self._store.get_setting(SETTING_KAMI_LAST_SIGN_IN_FAILED, False):
            return
        entry = self._vault.get(KAMI_ACCOUNT_VAULT_ID, CredentialKind.KAMI_ACCOUNT)
        if entry is None:
            return
        try:
            session = KamiSession.from_json(entry.password, email=entry.username)
            region = region_for(session.country)
        except (KamiProtocolError, ValueError):
            log.warning("kami: the stored session is unreadable; ignoring it")
            self._vault.delete(KAMI_ACCOUNT_VAULT_ID)
            return
        self._busy = True
        gen = self._gen

        def job() -> None:
            client: Any = None
            devices = None
            try:
                client = self._factory(region, session.country)
                devices = client.list_devices(session)
            except KamiSessionExpired:
                self._post(lambda: self._session_ended(gen))
                return
            except Exception as exc:  # offline or server trouble: keep the cached cameras
                log.info("kami: restore without a fresh list (%s)", type(exc).__name__)
            self._post(lambda: self._restored(gen, client, session, devices))

        self._submit(job)

    def refresh(self) -> None:
        """Reload the camera list and the online state (a no-op when signed out)."""
        session, client, gen = self._session, self._client, self._gen
        if session is None:
            return

        def job() -> None:
            try:
                api = client or self._factory(region_for(session.country), session.country)
                devices = api.list_devices(session)
            except KamiSessionExpired:
                self._post(lambda: self._session_ended(gen))
                return
            except Exception as exc:
                log.info("kami: refresh failed (%s); keeping the cached list", type(exc).__name__)
                return
            self._post(lambda: self._apply_devices(gen, devices))

        self._submit(job)

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
                log.error("kami background job failed (%s)", type(exc).__name__)

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
            log.error("kami: error while applying a result (%s)", type(exc).__name__)
        finally:
            with self._posts_lock:
                self._posts -= 1

    def _refuse(self, message: str) -> None:
        self._post(lambda: self.sign_in_failed.emit(message))

    # ================================================================== GUI side
    def _login_failed(self, gen: int, exc: BaseException, previous_failed: bool) -> None:
        self._busy = False
        if isinstance(exc, KamiKeyMissingError):  # nothing was sent: put the flag back
            self._store.set_setting(SETTING_KAMI_LAST_SIGN_IN_FAILED, previous_failed)
            message = MSG_KEY_MISSING.format(folder=key_file_path().parent)
        elif isinstance(exc, KamiConnectionError):
            message = MSG_OFFLINE
        elif isinstance(exc, KamiExtraStepError):
            message = MSG_EXTRA_STEP.format(code=exc.code)
        elif isinstance(exc, KamiAuthError):
            self._store.set_setting(SETTING_KAMI_BLOCKED_UNTIL, self._clock() + BLOCK_SECONDS)
            message = MSG_BAD_CREDENTIALS
        elif isinstance(exc, (KamiProtocolError, KamiServerError, KamiSessionExpired)):
            message = MSG_SERVER
        else:
            message = MSG_UNEXPECTED
        log.info("kami sign-in failed (%s, code %s)", type(exc).__name__, getattr(exc, "code", ""))
        if gen == self._gen:
            self.sign_in_failed.emit(message)

    def _login_ok(self, gen: int, client: Any, session: KamiSession, devices: list[KamiDevice] | None) -> None:
        self._busy = False
        if gen != self._gen:
            return
        label = mask_email(session.email)
        self._vault.set(KAMI_ACCOUNT_VAULT_ID, CredentialKind.KAMI_ACCOUNT, Credentials(session.email, session.to_json()))
        self._store.set_setting(SETTING_KAMI_ACCOUNT, {"label": label, "country": session.country,
                                                       "region": session.region})
        self._store.set_setting(SETTING_KAMI_LAST_SIGN_IN_FAILED, False)
        self._store.set_setting(SETTING_KAMI_BLOCKED_UNTIL, None)
        self._client, self._session, self._label = client, session, label
        if devices is not None:
            self._apply_devices(gen, devices)
        self.signed_in.emit(label)

    def _restored(self, gen: int, client: Any, session: KamiSession, devices: list[KamiDevice] | None) -> None:
        self._busy = False
        if gen != self._gen:
            return
        account = self._store.get_setting(SETTING_KAMI_ACCOUNT, None)
        label = account.get("label") if isinstance(account, dict) and account.get("label") else mask_email(session.email)
        self._client, self._session, self._label = client, session, label
        if devices is not None:
            self._apply_devices(gen, devices)
        self.signed_in.emit(label)

    def _session_ended(self, gen: int) -> None:
        """The stored token is dead. No silent re-login: the user must sign in by hand."""
        self._busy = False
        if gen != self._gen:
            return
        self._session = self._client = None
        self._label = ""
        self._vault.delete(KAMI_ACCOUNT_VAULT_ID)
        self._store.set_setting(SETTING_KAMI_ACCOUNT, None)
        self._store.set_setting(SETTING_KAMI_LAST_SIGN_IN_FAILED, True)
        self.sign_in_failed.emit(MSG_SESSION_ENDED)

    def _apply_devices(self, gen: int, devices: list[KamiDevice]) -> None:
        """Upsert the listed cameras and drop Kami cameras that are no longer listed."""
        if gen != self._gen:
            return
        existing = {c.id: c for c in self._ctrl.cameras() if c.source == "kami"}
        changed: list[CameraConfig] = []
        for dev in devices:
            old = existing.get(dev.camera_id)
            data = {"uid": dev.uid, "region": dev.region, "model_code": dev.model, "shared": dev.shared,
                    "cloud_name": dev.name}
            if old is None:
                cfg = CameraConfig(name=dev.name, host="", id=dev.camera_id, source="kami", device_id=dev.uid,
                                   model=dev.model, online=dev.online, lan_found=False, vendor_data=data)
            else:
                # a name changed locally (``controller.rename_camera``) differs from the last cloud name: keep it
                renamed = old.name != old.vendor_data.get("cloud_name", old.name)
                cfg = dataclasses.replace(old, name=old.name if renamed else dev.name, model=dev.model,
                                          online=dev.online, vendor_data={**old.vendor_data, **data})
            if cfg != old:
                changed.append(cfg)
        wanted = {dev.camera_id for dev in devices}
        gone = [cid for cid in existing if cid not in wanted]
        if changed:
            self._ctrl.upsert_cameras(changed, cloud=None)  # cloud=None: no TP-Link credential for Kami cameras
        if gone:
            self._ctrl.remove_cameras(gone)
        if changed or gone:
            self.cameras_changed.emit()
