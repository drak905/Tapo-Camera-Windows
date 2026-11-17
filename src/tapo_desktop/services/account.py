"""TP-Link ID account flow, like the Tapo phone app (contract: docs/CONTRACT_PHASE2.md, lane P2-app).

Sign in with email + password (+ MFA code when the account asks for one) -> the cloud device list
gives the cameras (name = alias, model, online state) -> TDP discovery finds them on the LAN
(matched by md5(deviceId), MAC as fallback) -> each one becomes a ``source="cloud"``
:class:`CameraConfig` with ``id = deviceId`` whose ``CredentialKind.CLOUD`` vault entry is
``("admin", <TP-Link ID password>)`` [spec:lan.local_connection_params]. No camera account needed.

Persistence:

* vault ``ACCOUNT_VAULT_ID`` / ``CLOUD`` = ``(email, password)`` - only with "Keep me signed in";
* setting ``"account"`` = non-secret :class:`AccountInfo` fields (cached for offline starts);
* setting ``"terminal_uuid"`` = 32 upper-case hex chars, generated once [spec:cloud.terminal_uuid];
* vault ``<deviceId>`` / ``LOCAL_TOKEN`` = ``("admin", <local access token>)`` for cameras the IoT
  cloud lists with a ``localAccessToken`` [spec:lat.source.thing_list] (fetched after sign-in,
  restore and refresh; removed when the cloud returns none; kept when the fetch fails).

Phase 3 (remote live view): after sign-in a real cloud client is attached to one
:class:`~tapo_desktop.services.remote.RelayProvider` (``relay_provider()``; limit from setting
``max_remote_streams``, default 4) which is handed to the controller (``set_remote_provider``); sign-out
detaches it. ``relay_factory(cloud_client) -> RelayClient`` replaces the default relay client (tests).

The password is never logged and never written outside the DPAPI vault; the cloud token stays in
the cloud client's memory.

Threading: network work (cloud calls, discovery) runs on a private serial worker; results are
applied on the GUI thread, where every signal is emitted. ``sign_out`` invalidates work that is
still in flight (a generation counter), so a late result cannot sign the user back in.

Cloud errors are recognised by class name (``CloudMfaRequired``, ``CloudAuthError``,
``CloudError`` from ``tapo_desktop.core.cloud.client``) so this module imports without the cloud
lane and tests can use stand-ins; network failures are :class:`TapoConnectionError`.
"""
from __future__ import annotations

import dataclasses
import hashlib
import logging
import re
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QCoreApplication, QObject, QThread, Qt, pyqtSignal, pyqtSlot

from tapo_desktop.core.control_client.tpap import tpap_requested
from tapo_desktop.core.errors import TapoConnectionError
from tapo_desktop.models import (
    ACCOUNT_VAULT_ID,
    AccountInfo,
    CameraConfig,
    CloudDevice,
    CredentialKind,
    Credentials,
    LanDevice,
)
from tapo_desktop.services._workers import Strand, WorkerPool

log = logging.getLogger(__name__)

#: Username for local camera login with the TP-Link ID password [spec:lan.local_connection_params].
CAMERA_USER = "admin"
SETTING_ACCOUNT = "account"
SETTING_TERMINAL_UUID = "terminal_uuid"
#: True after the user chose "Use without account" at startup (app.main then stops prompting).
SETTING_SIGN_IN_SKIPPED = "sign_in_skipped"
_UUID_RE = re.compile(r"[0-9A-F]{32}")
_LOCKED_CODES = {"-20661"}

MSG_BAD_CREDENTIALS = "Incorrect email or password."
MSG_LOCKED = "This TP-Link ID is temporarily locked after too many sign-in attempts. Try again later."
MSG_OFFLINE = "Cannot reach the TP-Link cloud. Check the internet connection and try again."
MSG_BAD_CODE = "The verification code is wrong or has expired."
MSG_NO_MFA = "No sign-in is waiting for a verification code. Sign in again."


# ====================================================================== core-lane hooks (lazy)
def _default_cloud_factory(terminal_uuid: str) -> Any:
    from tapo_desktop.core.cloud.client import TapoCloudClient

    return TapoCloudClient(terminal_uuid=terminal_uuid)


def _default_discover() -> list[LanDevice]:
    from tapo_desktop.core.discovery.tdp import discover

    return discover()


def _normalise_mac(mac: str) -> str:
    try:
        from tapo_desktop.core.discovery.matcher import normalise_mac
    except ImportError:  # discovery lane not merged yet: same rule, "AA-BB-CC-DD-EE-FF"
        digits = re.sub(r"[^0-9A-Fa-f]", "", mac or "").upper()
        return "-".join(digits[i:i + 2] for i in range(0, 12, 2)) if len(digits) == 12 else (mac or "").upper()
    return normalise_mac(mac) if mac else ""


def _control_protocol(hit: LanDevice) -> str:
    """"tpap" if the TDP reply advertises the TPAP login (encrypt_type "4" / tpap.pake) [spec:tpap.selection]."""
    raw = hit.raw if isinstance(hit.raw, dict) else {}
    tpap = raw.get("tpap")
    return "tpap" if tpap_requested(tpap if isinstance(tpap, dict) else None, raw.get("encrypt_type")) else ""


def _match(cloud: list[CloudDevice], lan: list[LanDevice]) -> dict[str, LanDevice]:
    """deviceId -> LanDevice [spec:lan.match_rule]: md5(deviceId) first, MAC as fallback."""
    try:
        from tapo_desktop.core.discovery.matcher import match_devices
    except ImportError:
        match_devices = None
    if match_devices is not None:
        return match_devices(cloud, lan)
    by_md5 = {d.device_id_md5.lower(): d for d in lan if d.device_id_md5}
    by_mac = {_normalise_mac(d.mac): d for d in lan if d.mac}
    out: dict[str, LanDevice] = {}
    for dev in cloud:
        hit = by_md5.get(hashlib.md5(dev.device_id.encode("utf-8")).hexdigest())
        if hit is None and dev.mac:
            hit = by_mac.get(_normalise_mac(dev.mac))
        if hit is not None:
            out[dev.device_id] = hit
    return out


def _is_error(exc: BaseException, name: str) -> bool:
    return any(cls.__name__ == name for cls in type(exc).__mro__)


def _user_message(exc: BaseException, *, mfa: bool = False) -> str:
    """Short, user-facing text for a sign-in failure (never contains the password)."""
    if isinstance(exc, TapoConnectionError):
        return MSG_OFFLINE
    if _is_error(exc, "CloudAuthError"):
        if str(getattr(exc, "code", "")) in _LOCKED_CODES:
            return MSG_LOCKED
        return MSG_BAD_CODE if mfa else MSG_BAD_CREDENTIALS
    if _is_error(exc, "CloudError"):
        text = str(exc).strip() or type(exc).__name__
        return f"Sign-in failed: {text}"
    return "Sign-in failed: unexpected error (see the log for details)."


@dataclasses.dataclass
class _Pending:
    """A login waiting for its MFA code (the same client keeps the MFA process id)."""

    client: Any
    email: str
    password: str
    remember: bool

    def __repr__(self) -> str:
        return f"_Pending(email={self.email!r}, password='***', remember={self.remember})"


class AccountManager(QObject):
    """Signed-in TP-Link ID and its cameras; see the module docstring."""

    signed_in = pyqtSignal(object)            # AccountInfo
    signed_out = pyqtSignal()
    mfa_required = pyqtSignal(object)         # list[int] MFA types (1 push, 2 email, 3 TOTP, 4 SMS)
    sign_in_failed = pyqtSignal(str)          # user-facing message
    cameras_refreshed = pyqtSignal(int, int)  # cloud cameras, found on the LAN

    _invoke = pyqtSignal(object)              # private: run a callable on the GUI thread

    def __init__(self, controller, *, cloud_factory: Callable[[str], Any] | None = None,
                 discover: Callable[[], list[LanDevice]] | None = None, parent: QObject | None = None,
                 relay_factory: Callable[[Any], Any] | None = None):
        super().__init__(parent)
        self._ctrl = controller
        self._store = controller.config_store
        self._vault = controller.vault
        self._cloud_factory = cloud_factory or (lambda terminal_uuid: _default_cloud_factory(terminal_uuid))
        self._discover = discover or (lambda: _default_discover())
        self._relay_factory = relay_factory
        self._relay_provider: Any = None
        self._pool = WorkerPool(max_workers=2, name="tapo-account")
        self._strand = Strand(self._pool, "account")
        self._posts_lock = threading.Lock()
        self._posts = 0
        self._gen = 0
        self._terminal_uuid: str | None = None
        self._client: Any = None
        self._account: AccountInfo | None = None
        self._email: str | None = None
        self._password: str | None = None
        self._pending: _Pending | None = None
        self._invoke.connect(self._run_posted, Qt.ConnectionType.QueuedConnection)

    def __repr__(self) -> str:
        email = self._account.email if self._account is not None else None
        return f"AccountManager(account={email!r})"

    # ================================================================== public API
    def account(self) -> AccountInfo | None:
        """The signed-in account, or None."""
        return self._account

    def relay_provider(self) -> Any:
        """The :class:`~tapo_desktop.services.remote.RelayProvider` for remote live view of the signed-in
        account (also handed to the controller), or None (signed out / the cloud client has no relay API)."""
        provider = self._relay_provider
        return provider if provider is not None and provider.available else None

    @property
    def config_store(self):
        """The controller's ConfigStore (settings ``account``, ``terminal_uuid``, ``sign_in_skipped``)."""
        return self._store

    @property
    def terminal_uuid(self) -> str:
        """This installation's terminal id (generated once, then persisted)."""
        if self._terminal_uuid is None:
            value = self._store.get_setting(SETTING_TERMINAL_UUID, None)
            if not (isinstance(value, str) and _UUID_RE.fullmatch(value)):
                value = uuid.uuid4().hex.upper()
                self._store.set_setting(SETTING_TERMINAL_UUID, value)
            self._terminal_uuid = value
        return self._terminal_uuid

    def sign_in(self, email: str, password: str, remember: bool = True) -> None:
        """Sign in asynchronously; ends with ``signed_in``, ``mfa_required`` or ``sign_in_failed``."""
        email = (email or "").strip()
        remember = bool(remember)
        gen, terminal = self._gen, self.terminal_uuid
        self._pending = None

        def job() -> None:
            client = self._cloud_factory(terminal)
            try:
                account = client.login(email, password)
            except Exception as exc:
                self._log_failure("sign-in", exc)
                self._post(lambda exc=exc: self._login_failed(gen, client, email, password, remember, exc))
                return
            self._finish_login(gen, client, account, email, password, remember)

        self._submit(job)

    def submit_mfa(self, code: str, mfa_type: int) -> None:
        """Send the verification code of the pending login (asynchronous)."""
        pending, gen = self._pending, self._gen
        if pending is None:
            self._post(lambda: self.sign_in_failed.emit(MSG_NO_MFA))
            return
        code = (code or "").strip()

        def job() -> None:
            try:
                account = pending.client.complete_mfa(code, int(mfa_type))
            except Exception as exc:
                self._log_failure("verification", exc)
                message = _user_message(exc, mfa=True)
                self._post(lambda: self._emit_if_current(gen, message))
                return
            self._finish_login(gen, pending.client, account, pending.email, pending.password, pending.remember)

        self._submit(job)

    def request_mfa_code(self, mfa_type: int) -> None:
        """Ask the cloud to send a code (2 email, 4 SMS) for the pending login (asynchronous)."""
        pending, gen = self._pending, self._gen
        if pending is None:
            self._post(lambda: self.sign_in_failed.emit(MSG_NO_MFA))
            return

        def job() -> None:
            try:
                pending.client.request_mfa_code(int(mfa_type))
            except Exception as exc:
                self._log_failure("sending the verification code", exc)
                message = _user_message(exc, mfa=True)
                self._post(lambda: self._emit_if_current(gen, message))

        self._submit(job)

    def restore_session(self) -> bool:
        """On startup: sign in silently with the stored account (asynchronous).

        Returns False when no account is stored (then cloud cameras left over from a session that
        was not kept are removed). Offline, the cached account and camera list are used and LAN
        discovery still runs.
        """
        creds = self._vault.get(ACCOUNT_VAULT_ID, CredentialKind.CLOUD)
        if creds is None:
            self._drop_cloud_cameras()
            return False
        email, password = creds.username, creds.password
        gen, terminal = self._gen, self.terminal_uuid
        cached = self._cached_account(email)

        def job() -> None:
            client = self._cloud_factory(terminal)
            try:
                account = client.login(email, password)
            except TapoConnectionError as exc:
                log.info("TP-Link cloud unreachable (%s); using the cached camera list", exc)
                lan = self._run_discover()
                self._post(lambda: self._apply_signed_in(gen, client, cached, email, password, True, None, lan))
                return
            except Exception as exc:
                self._log_failure("silent sign-in", exc)
                self._post(lambda exc=exc: self._login_failed(gen, client, email, password, True, exc))
                return
            self._finish_login(gen, client, account, email, password, True)

        self._submit(job)
        return True

    def refresh_cameras(self) -> None:
        """Reload the cloud camera list and rediscover the cameras on the LAN (asynchronous)."""
        if self._password is None or self._email is None:
            log.info("refresh_cameras ignored: not signed in")
            return
        gen, client, email, password = self._gen, self._client, self._email, self._password

        def job() -> None:
            devices = None
            tokens = None
            online_states: dict[str, bool] = {}
            try:
                if client is not None:
                    if getattr(client, "token", None) is None:  # started offline: log in now
                        client.login(email, password)
                    devices = list(client.list_cameras())
                    tokens = self._fetch_local_tokens(client)
                    online_states = self._online_states(client)
            except Exception as exc:
                log.warning("cloud camera list unavailable, keeping the cached list: %s", exc)
            lan = self._run_discover()
            self._post(lambda: self._set_online(online_states) or
                       self._apply_cameras(gen, devices, lan, password, tokens=tokens))

        self._submit(job)

    def sign_out(self) -> None:
        """Forget the account secret and the cloud cameras (their streams stop); manual cameras stay."""
        self._gen += 1
        self._client = self._account = self._email = self._password = self._pending = None
        self._drop_cloud_cameras()
        self._detach_relay()
        self._vault.delete(ACCOUNT_VAULT_ID)
        if self._store.get_setting(SETTING_ACCOUNT, None) is not None:
            self._store.set_setting(SETTING_ACCOUNT, None)
        self.signed_out.emit()

    def wait_idle(self, timeout_ms: int = 5000) -> bool:
        """Block until no account job runs and every result has been applied (when called on the
        GUI thread, posted results are processed while waiting). False on timeout."""
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

    # ================================================================== worker side
    def _submit(self, job: Callable[[], None]) -> None:
        """Queue an account job; an unexpected exception is logged (it would otherwise vanish in
        the job's Future and leave the UI waiting for a result that never comes)."""

        def guarded() -> None:
            try:
                job()
            except Exception:
                log.exception("account background job failed")

        self._strand.submit(guarded)

    def _finish_login(self, gen: int, client: Any, account: AccountInfo, email: str, password: str,
                      remember: bool) -> None:
        devices = None
        tokens = None
        online_states: dict[str, bool] = {}
        try:
            devices = list(client.list_cameras())
        except Exception as exc:
            log.warning("signed in, but the cloud camera list is unavailable: %s", exc)
        else:
            tokens = self._fetch_local_tokens(client)
            online_states = self._online_states(client)
        lan = self._run_discover()
        self._post(lambda: self._set_online(online_states) or
                   self._apply_signed_in(gen, client, account, email, password, remember, devices, lan, tokens))

    @staticmethod
    def _fetch_local_tokens(client: Any) -> dict[str, str] | None:
        """deviceId -> local access token [spec:lat.source.thing_list]; None when unknown (the fetch
        failed or the client has no such API), so cached tokens are kept. Never raises."""
        fetch = getattr(client, "local_access_tokens", None)
        if fetch is None:
            return None
        try:
            tokens = fetch()
        except Exception as exc:  # sign-in must not depend on the IoT cloud
            log.warning("local access tokens unavailable, keeping the stored ones: %s (%s)",
                        type(exc).__name__, getattr(exc, "code", ""))
            return None
        if not isinstance(tokens, dict):
            return None
        return {str(k): v for k, v in tokens.items() if isinstance(v, str) and v}

    def _set_online(self, states: dict[str, bool]) -> None:
        if states:
            self._thing_online = states

    @staticmethod
    def _online_states(client: Any) -> dict[str, bool]:
        """deviceId -> online from the client's last IoT thing list (empty when unknown)."""
        states = getattr(client, "thing_online", None)
        if not isinstance(states, dict):
            return {}
        return {str(k): bool(v) for k, v in states.items()}

    def _run_discover(self) -> list[LanDevice]:
        try:
            return list(self._discover())
        except Exception as exc:
            log.warning("LAN discovery failed: %s", exc)
            return []

    @staticmethod
    def _log_failure(what: str, exc: BaseException) -> None:
        log.info("%s failed: %s (%s)", what, type(exc).__name__, getattr(exc, "code", ""))

    # ================================================================== GUI side
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
        except Exception:
            log.exception("error while applying an account result")
        finally:
            with self._posts_lock:
                self._posts -= 1

    def _emit_if_current(self, gen: int, message: str) -> None:
        if gen == self._gen:
            self.sign_in_failed.emit(message)

    def _login_failed(self, gen: int, client: Any, email: str, password: str, remember: bool,
                      exc: BaseException) -> None:
        if gen != self._gen:
            return
        if _is_error(exc, "CloudMfaRequired"):
            self._pending = _Pending(client, email, password, remember)
            self.mfa_required.emit([int(t) for t in getattr(exc, "mfa_types", [])])
            return
        self.sign_in_failed.emit(_user_message(exc))

    def _apply_signed_in(self, gen: int, client: Any, account: AccountInfo, email: str, password: str,
                         remember: bool, devices: list[CloudDevice] | None, lan: list[LanDevice],
                         tokens: dict[str, str] | None = None) -> None:
        if gen != self._gen:
            return
        self._client, self._account, self._email, self._password = client, account, email, password
        self._pending = None
        if self._store.get_setting(SETTING_SIGN_IN_SKIPPED, False):
            self._store.set_setting(SETTING_SIGN_IN_SKIPPED, False)
        if remember:
            self._vault.set(ACCOUNT_VAULT_ID, CredentialKind.CLOUD, Credentials(email, password))
            self._store.set_setting(SETTING_ACCOUNT, dataclasses.asdict(account))
        self._attach_relay(client)
        self._apply_cameras(gen, devices, lan, password, notify=False, tokens=tokens)
        self.signed_in.emit(account)
        self._emit_counts()

    def _apply_cameras(self, gen: int, devices: list[CloudDevice] | None, lan: list[LanDevice],
                       password: str, *, notify: bool = True, tokens: dict[str, str] | None = None) -> None:
        """Upsert cloud cameras from ``devices`` (None = cloud unavailable: refresh the cached
        cloud cameras' LAN state only, remove nothing).

        ``tokens`` (deviceId -> local access token) replaces every listed camera's vault
        ``LOCAL_TOKEN`` (a camera without a token loses it); ``None`` = unknown, keep the stored ones."""
        if gen != self._gen:
            return
        cached = [c for c in self._ctrl.cameras() if c.source == "cloud"]
        prune = devices is not None
        provider = self.__dict__.get("_relay_provider")  # (tests may build the manager without __init__)
        if devices is not None and provider is not None:
            provider.update_devices(devices)  # device type / IoT-cloud flag for relay slots
        if devices is None:
            devices = [CloudDevice(device_id=c.device_id or c.id, mac=c.mac, model=c.model, alias=c.name,
                                   online=c.online) for c in cached]
        online = self._thing_online if isinstance(getattr(self, "_thing_online", None), dict) else {}
        if online:
            devices = [dataclasses.replace(d, online=online.get(d.device_id, d.online)) for d in devices]
        matches = _match(devices, lan)
        configs: list[CameraConfig] = []
        for dev in devices:
            old = self._ctrl.camera(dev.device_id)
            if old is not None and old.source != "cloud":
                continue  # an unrelated manual camera owns this id
            configs.append(self._cloud_config(old, dev, matches.get(dev.device_id)))
        if prune:
            keep = {c.id for c in configs}
            self._ctrl.remove_cameras([c.id for c in cached if c.id not in keep])
        if tokens is None:
            self._ctrl.upsert_cameras(configs, Credentials(CAMERA_USER, password))
        else:
            lats = {c.id: tokens.get(c.device_id or c.id) for c in configs}
            log.info("local access tokens: %d of %d cloud camera(s)", sum(1 for v in lats.values() if v), len(lats))
            self._ctrl.upsert_cameras(configs, Credentials(CAMERA_USER, password), local_tokens=lats)
        if notify:
            self._emit_counts()

    @staticmethod
    def _cloud_config(old: CameraConfig | None, dev: CloudDevice, hit: LanDevice | None) -> CameraConfig:
        base = old or CameraConfig(name=dev.alias or dev.model or dev.device_id, host="", id=dev.device_id,
                                   source="cloud")
        return dataclasses.replace(
            base, name=dev.alias or base.name, source="cloud", device_id=dev.device_id,
            mac=_normalise_mac(dev.mac) if dev.mac else base.mac, model=dev.model or base.model,
            online=bool(dev.online) or hit is not None,  # answering on the LAN means online
            host=hit.ip if hit is not None else "", lan_found=hit is not None,
            https_port=(hit.http_port or 443) if hit is not None else base.https_port,
            control_protocol=_control_protocol(hit) if hit is not None else base.control_protocol)

    def _emit_counts(self) -> None:
        cams = [c for c in self._ctrl.cameras() if c.source == "cloud"]
        self.cameras_refreshed.emit(len(cams), sum(1 for c in cams if c.lan_found and c.host))

    def _drop_cloud_cameras(self) -> None:
        ids = [c.id for c in self._ctrl.cameras() if c.source == "cloud"]
        if ids:
            self._ctrl.remove_cameras(ids)

    # ================================================================== remote live view (Phase 3)
    @staticmethod
    def _supports_relay(client: Any) -> bool:
        """Only a real cloud client (signed-call ``getAppServiceUrl`` + silent re-login) can request relay
        slots; stand-ins without that API get no remote live view."""
        return callable(getattr(client, "get_service_urls", None)) and callable(getattr(client, "refresh_session", None))

    def _attach_relay(self, client: Any) -> None:
        """GUI thread: hand the signed-in client to the (single) RelayProvider and the controller."""
        if not self._supports_relay(client):
            self._detach_relay()
            return
        from tapo_desktop.services.remote import SETTING_MAX_REMOTE_STREAMS, RelayProvider, max_remote_streams

        limit = max_remote_streams(self._store.get_setting(SETTING_MAX_REMOTE_STREAMS, None))
        if self._relay_provider is None:
            self._relay_provider = RelayProvider(terminal_uuid=self.terminal_uuid, max_streams=limit,
                                                 relay_factory=self._relay_factory)
        else:
            self._relay_provider.gate.set_capacity(limit)
        self._relay_provider.attach(client)
        setter = getattr(self._ctrl, "set_remote_provider", None)
        if callable(setter):
            setter(self._relay_provider)

    def _detach_relay(self) -> None:
        if self._relay_provider is not None:
            self._relay_provider.detach()
        setter = getattr(self._ctrl, "set_remote_provider", None)
        if callable(setter):
            setter(None)

    def _cached_account(self, email: str) -> AccountInfo:
        raw = self._store.get_setting(SETTING_ACCOUNT, None)
        if isinstance(raw, dict) and raw.get("email") == email:
            fields = {f.name for f in dataclasses.fields(AccountInfo)}
            try:
                return AccountInfo(**{k: str(v) for k, v in raw.items() if k in fields})
            except TypeError:
                pass
        return AccountInfo(email=email)
