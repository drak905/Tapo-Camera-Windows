"""Session-managing control client for the camera's HTTPS API (TPAP, secure v3 and legacy login).

TPAP (SPAKE2+ + AES-128-CCM ``/ds``, see :mod:`.tpap`) is used when the TDP hints ask for it or when
the v3 step 1 answers -40211 [spec:tpap.selection].

``local_token`` (optional) is the camera's cloud-issued local access token (LAT) [spec:lat.*]: TPAP
offers SHA256_HEX_UPPER(LAT) first, and the v3 secure login uses the raw LAT when the camera's
device_confirm proves it holds the LAT. The legacy MD5 login always uses the password.

Credentials never appear in logs, exception messages or ``repr``.
"""
from __future__ import annotations

import datetime
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

from tapo_desktop.core.auth import crypto
from tapo_desktop.core.control_client import dtos
from tapo_desktop.core.control_client import tpap as tpap_mod
from tapo_desktop.core.control_client.transport import HttpsTransport, PinStore
from tapo_desktop.core.errors import (
    TapoAuthError, TapoDeviceError, TapoError, TapoLockedError, TapoProtocolError, TapoSessionExpired,
)
from tapo_desktop.models import DayNightMode, DeviceInfo, Preset, RecordingClip

log = logging.getLogger(__name__)

_OK = 0
_SESSION_EXPIRED = -40401        # [spec:auth.errors.table]
_PROTOCOL_FORMAT_ERROR = -40210  # on securePassthrough: invalid secure session -> re-login
_INVALID_NONCE = -40413          # normal step-1 challenge of encrypt_type "3"
_TPAP_REQUIRED = -40211          # step 1 on TPAP-only firmware (encrypt_type ["4"]) [spec:tpap.selection]
_LOCK_CODES = frozenset({-40404, -40408})  # DEVICE_BLOCKED, SYSTEM_BLOCKED
# plaintext /ds refusals that mean "log in again" [spec:tpap.ds.errors]
_TPAP_SESSION_ERRORS = frozenset({-40401, -40418, -40420, -40421, -2402})
_PAGE_SIZE = 100
_MAX_PAGES = 100                 # safety cap: 10 000 clips per day


@dataclass
class _Session:
    """Live login state. Never rendered (repr disabled) because it holds the stok and keys."""

    stok: str
    secure: bool
    cnonce: str = ""
    cipher: crypto.SecureSessionCipher | None = None
    seq: int = 0
    tpap: "tpap_mod.TpapSession | None" = None
    expires_at: float | None = None
    secret: str = ""  # secure v3: the password or LAT the session keys came from (tapo_tag)

    def __repr__(self) -> str:
        return f"_Session(secure={self.secure})"


class _SessionInvalid(Exception):
    """Internal: the session is no longer usable. ``error`` is raised if a fresh login does not help."""

    def __init__(self, error: TapoError):
        super().__init__(type(error).__name__)
        self.error = error


def _as_int(value: Any, what: str) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            pass
    raise TapoProtocolError(f"response {what} is not an integer")


def _error_code(obj: dict) -> int:
    return _as_int(obj.get("error_code", _OK), "error_code")


def _result(obj: dict) -> dict:
    res = obj.get("result")
    return res if isinstance(res, dict) else {}


def _parse_json(text: str) -> dict:
    try:
        obj = json.loads(text)
    except ValueError as exc:
        raise TapoProtocolError("camera response is not JSON") from exc
    if not isinstance(obj, dict):
        raise TapoProtocolError("camera response is not a JSON object")
    return obj


def _tpap_refusal(data: bytes) -> int | None:
    """error_code of a plaintext JSON refusal on TPAP ``/ds``, else ``None`` (binary reply).

    Like the app, the whole body is first tried as JSON, because an encrypted reply whose seq starts
    with 0x7B would also begin with ``{``.
    """
    if not data.startswith(b"{"):
        return None
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(obj, dict) or "error_code" not in obj:
        return None
    try:
        return _error_code(obj)
    except TapoProtocolError:
        return None


def _normalize_item(item: Any) -> dict:
    if not isinstance(item, dict):
        raise TapoProtocolError("multipleRequest response item is not an object")
    method = item.get("method")
    return {"method": method if isinstance(method, str) else None, "result": _result(item),
            "error_code": _error_code(item)}


#: Error of ``searchVideoWithUTC`` on firmware that wants ``player_id`` instead of ``"id": 1`` (real C510W).
ERR_CLIP_SEARCH_FORM = -71103
#: Stable (per process) player id for the ``player_id`` clip-search form when none is configured.
DEFAULT_PLAYER_ID = uuid.uuid4().hex
_CLIP_SEARCH_FORMS = ("id", "player_id")


class ControlApi:
    """High-level camera API shared by the LAN :class:`TapoControlClient` and the cloud
    :class:`~tapo_desktop.core.cloud.passthrough.RemoteControlClient`.

    Subclasses provide ``request(req) -> result`` (raises TapoDeviceError for a non-zero item
    error_code) and ``multiple_request(reqs) -> items``; every wrapper below is built on ``request``.

    ``recordings_for_day`` sends ``searchVideoWithUTC`` in the form that worked last on this client
    (``"id": 1`` first); on -71103 it retries that page once with ``player_id`` (``_player_id``) or
    back with ``id`` and remembers the form that answered.
    """

    _player_id: str = DEFAULT_PLAYER_ID
    _clip_search_form: str = "id"

    def request(self, req: dict) -> dict:  # pragma: no cover - interface
        raise NotImplementedError

    def multiple_request(self, reqs: list[dict]) -> list[dict]:  # pragma: no cover - interface
        raise NotImplementedError

    def get_device_info(self) -> DeviceInfo:
        """Model, alias, firmware etc. (getDeviceInfo)."""
        return dtos.parse_device_info(self.request(dtos.get_device_info_req()))

    def get_components(self) -> dict[str, int]:
        """Supported components and their versions (getAppComponentList)."""
        return dtos.parse_component_list(self.request(dtos.get_component_list_req()))

    def move(self, x: int, y: int) -> None:
        """Relative pan/tilt move (motorMove)."""
        self.request(dtos.motor_move_req(x, y))

    def step(self, direction: int) -> None:
        """One step in ``direction`` (0, 90, 180 or 270 degrees)."""
        self.request(dtos.motor_step_req(direction))

    def stop_move(self) -> None:
        """Stop any PTZ motion."""
        self.request(dtos.motor_stop_req())

    def get_presets(self) -> list[Preset]:
        """Saved PTZ presets."""
        return dtos.parse_presets(self.request(dtos.get_presets_req()))

    def goto_preset(self, preset_id: str) -> None:
        """Move to a saved preset."""
        self.request(dtos.goto_preset_req(preset_id))

    def add_preset(self, name: str) -> None:
        """Save the current position as a new preset."""
        self.request(dtos.add_preset_req(name))

    def delete_preset(self, preset_id: str) -> None:
        """Delete a preset."""
        self.request(dtos.delete_preset_req(preset_id))

    def get_privacy(self) -> bool:
        """True if privacy mode (lens mask) is on."""
        return dtos.parse_privacy(self.request(dtos.get_privacy_req()))

    def set_privacy(self, enabled: bool) -> None:
        """Turn privacy mode (lens mask) on or off."""
        self.request(dtos.set_privacy_req(enabled))

    def get_day_night(self) -> DayNightMode:
        """Current day/night (IR) mode."""
        return dtos.parse_day_night(self.request(dtos.get_day_night_req()))

    def set_day_night(self, mode: DayNightMode) -> None:
        """Set the day/night (IR) mode."""
        self.request(dtos.set_day_night_req(mode))

    def get_led(self) -> bool:
        """True if the status LED is enabled."""
        return dtos.parse_led(self.request(dtos.get_led_req()))

    def set_led(self, enabled: bool) -> None:
        """Enable or disable the status LED."""
        self.request(dtos.set_led_req(enabled))

    def set_alarm(self, active: bool) -> None:
        """Start or stop the manual siren/light alarm."""
        self.request(dtos.manual_alarm_req(active))

    def get_motion_detection(self) -> tuple[bool, int | None]:
        """``(enabled, digital_sensitivity or None)``."""
        return dtos.parse_motion_detection(self.request(dtos.get_motion_detection_req()))

    def set_motion_detection(self, enabled: bool, sensitivity: int | None = None) -> None:
        """Enable/disable motion detection; ``sensitivity`` in {20, 60, 80} if given."""
        self.request(dtos.set_motion_detection_req(enabled, sensitivity))

    def get_sd_status(self) -> list[dict]:
        """One dict per storage device (empty list if no card)."""
        return dtos.parse_sd_status(self.request(dtos.get_sd_status_req()))

    def days_with_recordings(self, start: date, end: date) -> list[date]:
        """Days in ``[start, end]`` that have recordings, sorted."""
        return dtos.parse_days(self.request(dtos.search_days_req(start, end)))

    def _clip_search_req(self, form: str, start: int, end: int, first: int, last: int) -> dict:
        player_id = self._player_id if form == "player_id" else None
        return dtos.search_day_clips_req(start, end, first, last, player_id=player_id)

    def _search_clips(self, start: int, end: int, first: int, last: int) -> dict:
        """One searchVideoWithUTC page; -71103 -> one retry in the other form, which is then kept."""
        form = self._clip_search_form
        try:
            return self.request(self._clip_search_req(form, start, end, first, last))
        except TapoDeviceError as exc:
            if exc.code != ERR_CLIP_SEARCH_FORM:
                raise
        other = _CLIP_SEARCH_FORMS[1 - _CLIP_SEARCH_FORMS.index(form)]
        log.info("clip search: the camera refused the %r form (-71103); retrying with %r", form, other)
        result = self.request(self._clip_search_req(other, start, end, first, last))
        self._clip_search_form = other
        return result

    def recordings_for_day(self, day: date, tz: datetime.tzinfo | None = None) -> list[RecordingClip]:
        """Clips of local day ``[00:00, 24:00)`` in ``tz`` (None = local timezone); pages until
        ``to_be_continued`` is 0."""
        midnight = datetime.datetime.combine(day, datetime.time(0))
        midnight = midnight.replace(tzinfo=tz) if tz is not None else midnight.astimezone()
        start = int(midnight.timestamp())
        end = start + 86399
        clips: list[RecordingClip] = []
        for page in range(_MAX_PAGES):
            first = page * _PAGE_SIZE
            result = self._search_clips(start, end, first, first + _PAGE_SIZE - 1)
            page_clips = dtos.parse_day_clips(result)
            clips.extend(page_clips)
            if not page_clips or not dtos.has_more_clips(result):
                break
        else:
            log.warning("recordings_for_day(%s) stopped after %d pages", day, _MAX_PAGES)
        return sorted(clips, key=lambda c: c.start)

    def reboot(self) -> None:
        """Reboot the camera."""
        self.request(dtos.reboot_req())

    def rename(self, name: str) -> None:
        """Set the camera's name (setDeviceAlias) [spec:rename.request]."""
        raise NotImplementedError


class TapoControlClient(ControlApi):
    """Thread-safe client. Auto-logs-in on first use; on -40401 (top-level or per-item) or HTTP 401
    it re-logs-in once and retries once. ``seq`` increments per secure request.

    One re-entrant lock serialises login and the whole seq/tag/POST critical section, so concurrent
    callers can never desynchronise the secure session's ``seq``.

    ``player_id`` (32 hex chars, default :data:`DEFAULT_PLAYER_ID`) is used by the ``player_id`` form
    of the clip search (see :class:`ControlApi`).
    """

    def __init__(self, host: str, password: str, *, username: str = "admin", port: int = 443,
                 transport: HttpsTransport | None = None, pin_store: PinStore | None = None,
                 timeout: float = 10.0, tpap: dict | None = None, encrypt_type: Any = None,
                 auth_retry_after: float = 120.0, local_token: str | None = None, player_id: str | None = None):
        self._host = host
        self._port = int(port)
        self._username = username
        self._password = password
        self._local_token = local_token or None
        if player_id:
            self._player_id = str(player_id)
        self._tpap_info = dict(tpap) if isinstance(tpap, dict) else None
        self._use_tpap = tpap_mod.tpap_requested(self._tpap_info, encrypt_type)
        self._transport = transport if transport is not None else HttpsTransport(
            host, self._port, pin_store=pin_store, timeout=timeout)
        self._headers = {"Accept": "application/json", "User-Agent": "Tapo CameraClient Android",
                         "requestByApp": "true", "Referer": f"https://{host}:{self._port}"}
        self._lock = threading.RLock()
        self._session: _Session | None = None
        self._secure = False
        self._tpap_auth: tpap_mod.TpapAuthenticator | None = None
        self._tpap_confirmed = False     # the camera itself demanded TPAP (-40211)
        self._auth_retry_after = float(auth_retry_after)
        self._auth_error: TapoAuthError | None = None
        self._auth_blocked_until: float | None = None

    def __repr__(self) -> str:
        return (f"TapoControlClient(host={self._host!r}, port={self._port}, username={self._username!r}, "
                f"secure={self._secure}, logged_in={self.logged_in})")

    # ================================================================== login
    def login(self) -> None:
        """TPAP login if hinted or if step 1 returns -40211; secure login (encrypt_type "3") if step 1
        returns -40413 with a nonce; else legacy MD5 login.

        Bad device_confirm / bad password -> TapoAuthError; -40404 / lock codes -> TapoLockedError.
        TPAP makes at most ``tpap.MAX_ATTEMPTS`` password attempts per call.
        """
        with self._lock:
            self._login_locked()

    @property
    def is_secure(self) -> bool:
        """True if the last successful login used an encrypted flow (secure v3 or TPAP)."""
        return self._secure

    @property
    def protocol(self) -> str | None:
        """``"tpap"``, ``"secure"`` or ``"legacy"`` for the current session, ``None`` if logged out."""
        sess = self._session
        if sess is None:
            return None
        if sess.tpap is not None:
            return "tpap"
        return "secure" if sess.secure else "legacy"

    def _tpap_login(self) -> _Session:
        """SPAKE2+ login [spec:tpap.pake_register.request]; the authenticator remembers what worked."""
        if self._tpap_auth is None:
            self._tpap_auth = tpap_mod.TpapAuthenticator(self._post_login, self._password, tpap=self._tpap_info,
                                                         local_token=self._local_token)
        tsess = self._tpap_auth.login()
        expires_at = (time.monotonic() + tsess.expires_in
                      if isinstance(tsess.expires_in, int) and tsess.expires_in > 0 else None)
        log.info("logged in to %s:%s (tpap)", self._host, self._port)
        return _Session(stok=tsess.stok, secure=True, seq=tsess.start_seq, tpap=tsess, expires_at=expires_at)

    @property
    def uses_local_token(self) -> bool:
        """True if a cloud-issued local access token was given (the token itself is never exposed)."""
        return self._local_token is not None

    @property
    def logged_in(self) -> bool:
        """True while a session (stok) is held."""
        return self._session is not None

    def _login_locked(self) -> None:
        """Login guarded by an auth circuit breaker.

        A wrong password must not be retried automatically: every failed login counts towards the
        camera's lockout, and background callers (status refresh, event polling) would otherwise
        hammer it. After a ``TapoAuthError`` (not a lockout) further logins on this client fail
        fast with the same error for ``auth_retry_after`` seconds, or until ``reset_auth_backoff()``.
        """
        blocked = self._auth_blocked_until
        if blocked is not None and time.monotonic() < blocked and self._auth_error is not None:
            raise TapoAuthError(str(self._auth_error))
        try:
            self._login_impl()
        except TapoLockedError:
            raise
        except TapoAuthError as exc:
            self._auth_error = exc
            self._auth_blocked_until = time.monotonic() + self._auth_retry_after
            raise
        self._auth_error = None
        self._auth_blocked_until = None

    def reset_auth_backoff(self) -> None:
        """Allow the next login attempt immediately (e.g. after the user re-entered the password)."""
        with self._lock:
            self._auth_error = None
            self._auth_blocked_until = None

    def _login_impl(self) -> None:
        self._session = None
        if self._use_tpap:
            try:
                self._session = self._tpap_login()
                self._secure = True
                return
            except tpap_mod.TpapNotSupported:
                if self._tpap_confirmed:
                    raise
                log.info("%s declined TPAP and asked for the v3 login", self._host)
                self._use_tpap = False
        cnonce = crypto.generate_cnonce()
        resp = self._post_login({"cnonce": cnonce, "encrypt_type": "3", "username": self._username})
        code = _error_code(resp)
        self._raise_if_locked(code, resp)
        if code == _TPAP_REQUIRED:
            self._use_tpap = True
            self._tpap_confirmed = True
            self._session = self._tpap_login()
            self._secure = True
            return
        data = _result(resp).get("data")
        if (code == _INVALID_NONCE and isinstance(data, dict) and isinstance(data.get("nonce"), str)
                and isinstance(data.get("device_confirm"), str)):
            self._session = self._secure_login(cnonce, data["nonce"], data["device_confirm"])
        else:
            log.debug("%s did not offer a secure challenge (error %s); using legacy login", self._host, code)
            self._session = self._legacy_login()
        self._secure = self._session.secure
        log.info("logged in to %s:%s (%s)", self._host, self._port, "secure" if self._secure else "legacy")

    def _secret_for(self, cnonce: str, nonce: str, device_confirm: str) -> str | None:
        """The secret the camera proves it knows via ``device_confirm`` [spec:lat.legacy_secure.login].

        The check is local (no login attempt is spent), so both the raw local access token (LAT
        mode) and the TP-Link ID password can be tested; ``None`` if neither matches.
        """
        candidates = [self._local_token] if self._local_token else []
        candidates.append(self._password)
        for secret in candidates:
            if secret and crypto.verify_device_confirm(secret, cnonce, nonce, device_confirm):
                return secret
        return None

    def _secure_login(self, cnonce: str, nonce: str, device_confirm: str) -> _Session:
        secret = self._secret_for(cnonce, nonce, device_confirm)
        if secret is None:
            if self._local_token:
                raise TapoAuthError("camera did not confirm the password: neither the local access token "
                                    "nor the TP-Link ID password matches")
            raise TapoAuthError("camera did not confirm the password: wrong password, or the camera "
                                "uses a cloud-synced local access token")
        keys = crypto.derive_session_keys(secret, cnonce, nonce)
        resp = self._post_login({"cnonce": cnonce, "encrypt_type": "3", "username": self._username,
                                 "digest_passwd": crypto.digest_passwd(secret, cnonce, nonce)})
        code = _error_code(resp)
        self._raise_if_locked(code, resp)
        if code != _OK:
            raise TapoAuthError(f"secure login rejected by the camera (error {code})")
        res = _result(resp)
        return _Session(stok=self._stok(res), secure=True, cnonce=cnonce,
                        cipher=crypto.SecureSessionCipher(keys), seq=_as_int(res.get("start_seq"), "start_seq"),
                        secret=secret)

    def _legacy_login(self) -> _Session:
        resp = self._post_login({"hashed": True, "password": crypto.md5_upper(self._password),
                                 "username": self._username})
        code = _error_code(resp)
        self._raise_if_locked(code, resp)
        if code != _OK:
            raise TapoAuthError(f"login rejected by the camera (error {code})")
        return _Session(stok=self._stok(_result(resp)), secure=False)

    @staticmethod
    def _stok(res: dict) -> str:
        stok = res.get("stok")
        if not isinstance(stok, str) or not stok:
            raise TapoProtocolError("login response has no stok")
        return stok

    @staticmethod
    def _raise_if_locked(code: int, resp: dict) -> None:
        if code in _LOCK_CODES:
            data = _result(resp).get("data")
            left = data.get("sec_left") if isinstance(data, dict) else None
            suffix = f"; retry in {left} s" if isinstance(left, int) and not isinstance(left, bool) else ""
            raise TapoLockedError(f"camera is blocking logins after too many failures (error {code}){suffix}")

    def _post_login(self, params: dict) -> dict:
        status, _hdrs, text = self._transport.post(
            "/", crypto.dumps_compact({"method": "login", "params": params}), dict(self._headers))
        if status == 401:
            raise TapoAuthError("login rejected (HTTP 401)")
        if status != 200:
            raise TapoProtocolError(f"login answered with HTTP {status}")
        return _parse_json(text)

    # ================================================================== requests
    def request(self, req: dict) -> dict:
        """Send one request via multipleRequest and return its ``result``.

        Non-zero item error_code -> TapoDeviceError(code, method).
        """
        items = self.multiple_request([req])
        if not items:
            raise TapoProtocolError(f"no response item for {req.get('method')}")
        item = items[0]
        if item["error_code"] != _OK:
            raise TapoDeviceError(item["error_code"], item["method"] or req.get("method"))
        return item["result"]

    def multiple_request(self, reqs: list[dict]) -> list[dict]:
        """Send a batch; return raw items ``[{"method", "result", "error_code"}]``."""
        with self._lock:
            for attempt in (1, 2):
                sess = self._session
                if sess is not None and sess.expires_at is not None and time.monotonic() >= sess.expires_at:
                    log.debug("TPAP session on %s:%s reached its expiry; logging in again", self._host, self._port)
                    self._session = None
                if self._session is None:
                    self._login_locked()
                try:
                    return self._send_batch(reqs)
                except _SessionInvalid as invalid:
                    self._session = None
                    if attempt == 2:
                        raise invalid.error from None
                    log.info("session on %s:%s is no longer valid (%s); logging in again",
                             self._host, self._port, invalid)
        raise AssertionError("unreachable")  # pragma: no cover

    def _send_batch(self, reqs: list[dict]) -> list[dict]:
        reply = self._post_ds(dtos.multiple_request(reqs))
        code = _error_code(reply)
        if code == _SESSION_EXPIRED:
            raise _SessionInvalid(TapoSessionExpired("multipleRequest"))
        if code != _OK:
            raise TapoDeviceError(code, "multipleRequest")
        responses = _result(reply).get("responses")
        if not isinstance(responses, list):
            raise TapoProtocolError("multipleRequest response has no responses list")
        items = [_normalize_item(r) for r in responses]
        for item in items:
            if item["error_code"] == _SESSION_EXPIRED:
                raise _SessionInvalid(TapoSessionExpired(item["method"]))
        return items

    def _post_ds(self, inner: dict) -> dict:
        """POST ``inner`` to ``/stok=<stok>/ds`` (secure-wrapped if needed); return the inner reply."""
        sess = self._session
        assert sess is not None
        path = f"/stok={sess.stok}/ds"
        headers = dict(self._headers)
        if sess.tpap is not None:
            return self._post_tpap(sess, path, inner, headers)
        if not sess.secure:
            return self._post_checked(path, crypto.dumps_compact(inner), headers)
        assert sess.cipher is not None
        seq = sess.seq
        sess.seq += 1  # consumed whether or not the request succeeds [spec:auth.secure.seq]
        body = crypto.wrap_secure_passthrough(sess.cipher, inner)
        headers["seq"] = str(seq)
        headers["tapo_tag"] = crypto.compute_tag(sess.secret or self._password, sess.cnonce, body, seq)
        outer = self._post_checked(path, body, headers)
        code = _error_code(outer)
        if code in (_SESSION_EXPIRED, _PROTOCOL_FORMAT_ERROR):
            err = TapoSessionExpired("securePassthrough") if code == _SESSION_EXPIRED \
                else TapoDeviceError(code, "securePassthrough")
            raise _SessionInvalid(err)
        if code != _OK:
            raise TapoDeviceError(code, "securePassthrough")
        encrypted = _result(outer).get("response")
        if not isinstance(encrypted, str):
            raise TapoProtocolError("securePassthrough response has no encrypted payload")
        try:
            return _parse_json(sess.cipher.decrypt(encrypted))
        except TapoProtocolError as exc:
            raise _SessionInvalid(exc) from exc

    def _post_tpap(self, sess: _Session, path: str, inner: dict, headers: dict[str, str]) -> dict:
        """One AES-128-CCM ``/ds`` exchange [spec:tpap.ds.framing]; any refusal ends the session."""
        assert sess.tpap is not None
        seq = sess.seq
        sess.seq += 1  # every attempt burns a seq [spec:tpap.ds.framing]
        body = sess.tpap.cipher.seal(crypto.dumps_compact(inner).encode("utf-8"), seq)
        headers["Content-Type"] = "application/octet-stream"
        headers["Accept"] = "application/octet-stream"
        status, _hdrs, data = self._transport.post_bytes(path, body, headers)
        if status == 401:
            raise _SessionInvalid(TapoSessionExpired(None))
        if status != 200:
            raise _SessionInvalid(TapoProtocolError(f"camera answered with HTTP {status}"))
        refusal = _tpap_refusal(data)
        if refusal is not None:  # plaintext JSON: the camera refused the request and dropped the session
            if refusal in _TPAP_SESSION_ERRORS:
                raise _SessionInvalid(TapoSessionExpired("multipleRequest"))
            self._session = None
            raise TapoDeviceError(refusal, "multipleRequest")
        try:
            _seq, plain = sess.tpap.cipher.open(data)
            return _parse_json(plain.decode("utf-8"))
        except (TapoProtocolError, UnicodeDecodeError) as exc:
            raise _SessionInvalid(TapoProtocolError("TPAP reply could not be decrypted")) from exc

    def _post_checked(self, path: str, body: str, headers: dict[str, str]) -> dict:
        status, _hdrs, text = self._transport.post(path, body, headers)
        if status == 401:
            raise _SessionInvalid(TapoSessionExpired(None))
        if status != 200:
            raise TapoProtocolError(f"camera answered with HTTP {status}")
        return _parse_json(text)

    # ================================================================== lifecycle
    def reboot(self) -> None:
        """Reboot the camera (the session ends; the next request logs in again)."""
        self.request(dtos.reboot_req())
        with self._lock:
            self._session = None

    def close(self) -> None:
        """Forget the session and close the HTTPS connection (idempotent)."""
        self._session = None
        self._transport.close()
