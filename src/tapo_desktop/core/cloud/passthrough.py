"""Remote camera control through the TP-Link IoT cloud passthrough, like the Tapo app away from home and
for cameras shared to the account [spec:remote.control.*].

Every camera command is the LAN request wrapped in ``multipleRequest`` and sent as

``POST {thing.appServerUrlV2 || appServerUrl}/v1/things/{thingName}/services-sync``

with the IoT headers (``Authorization: ut|<account token>``, ``app-cid`` ..., not HMAC-signed) and the body
``{"serviceId":"passthrough","inputParams":{"requestData":{"method":"multipleRequest","params":{"requests":[..]}}}}``
[spec:remote.control.iot_passthrough.request]. The reply is
``{"outputParams":{"responseData":{"error_code":0,"result":{"responses":[..]}}}}``; some paths return the
responseData directly as ``outputParams``, so both are accepted [spec:remote.control.iot_passthrough.response].
Proven on real owned (role 0) and shared (role 1) C510W cameras with tools/diag_remote_ctl.py.

Error mapping (cloud codes are those of the legacy passthrough, the IoT ones are not named in the APK):
token expired (HTTP 401 / -20651 / -20652 / -20675) -> one silent re-login, then once more;
-20571 -> ``TapoConnectionError("Camera is offline")``; -20002 / HTTP 408 -> ``"Camera did not respond"``;
HTTP 429 -> "busy"; other cloud codes -> TapoDeviceError; a non-zero ``responseData.error_code`` ->
TapoDeviceError(code, "multipleRequest"); per-command codes stay in the items (as on the LAN).

:class:`RemoteControlClient` offers the :class:`~tapo_desktop.core.control_client.client.TapoControlClient`
API (it shares :class:`~tapo_desktop.core.control_client.client.ControlApi`) without any camera login, so
it never spends one of the camera's login attempts. The account token never appears in logs or reprs.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any
from urllib.parse import quote, urlsplit

from tapo_desktop.core.cloud.client import CloudError
from tapo_desktop.core.control_client.client import ControlApi, _normalize_item
from tapo_desktop.core.errors import TapoConnectionError, TapoDeviceError, TapoProtocolError

__all__ = ["CloudPassthrough", "MSG_CAMERA_OFFLINE", "MSG_CLOUD_BUSY", "MSG_NO_RESPONSE", "MSG_SESSION_EXPIRED",
           "PASSTHROUGH_TIMEOUT_S", "RemoteControlClient", "services_sync_url"]

log = logging.getLogger(__name__)

#: OkHttp connect/read/write timeout of the app for services-sync [spec:remote.control.iot_passthrough.request].
PASSTHROUGH_TIMEOUT_S = 30.0
ERR_DEVICE_OFFLINE = -20571
ERR_DEVICE_TIMEOUT = -20002
ERR_NOT_BOUND = -20580
_TOKEN_CODES = frozenset({-20651, -20652, -20675})
MSG_CAMERA_OFFLINE = "Camera is offline"
MSG_NO_RESPONSE = "Camera did not respond"
MSG_CLOUD_BUSY = "The TP-Link cloud is busy (too many requests); try again in a moment."
MSG_SESSION_EXPIRED = "The TP-Link cloud session expired; sign in again."
MSG_NOT_BOUND = "This camera is not linked to the signed-in TP-Link ID."


class _TokenRejected(Exception):
    """Internal: the cloud refused the account token."""


def services_sync_url(app_server_url: str, thing_name: str) -> str:
    """``{app_server_url}/v1/things/{thing_name}/services-sync`` (thing name quoted as one path segment);
    TapoProtocolError for anything that is not an http(s) URL."""
    base = str(app_server_url or "").strip().rstrip("/")
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.query or parts.fragment:
        raise TapoProtocolError("The TP-Link cloud returned no usable address for this camera.")
    if not thing_name:
        raise ValueError("thing_name is required")
    return f"{base}/v1/things/{quote(str(thing_name), safe='')}/services-sync"


def _code(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(str(value).strip() or 0)
    except (TypeError, ValueError):
        return 0


def _cloud_code(body: Any) -> int:
    """First non-zero error code of an IoT (``code``) or account-API style (``errorCode`` / ``error_code``) body."""
    if not isinstance(body, dict):
        return 0
    for key in ("code", "errorCode", "error_code"):
        code = _code(body.get(key))
        if code:
            return code
    return 0


def _cloud_error(code: int, status: int) -> Exception:
    """User-facing error for a refused passthrough call (no secrets: only codes)."""
    if code == ERR_DEVICE_OFFLINE:
        return TapoConnectionError(MSG_CAMERA_OFFLINE)
    if code in (ERR_DEVICE_TIMEOUT, 12202) or status == 408:
        return TapoConnectionError(MSG_NO_RESPONSE)
    if status == 429:
        return TapoConnectionError(MSG_CLOUD_BUSY)
    if code == ERR_NOT_BOUND:
        return TapoDeviceError(code, "passthrough", MSG_NOT_BOUND)
    value = code or status
    return TapoDeviceError(value, "passthrough", f"TP-Link cloud error {value} for this camera")


class CloudPassthrough:
    """``services-sync`` passthrough of one signed-in account (thread-safe, stateless per call).

    ``cloud_client`` is a signed-in :class:`~tapo_desktop.core.cloud.client.TapoCloudClient` (duck-typed:
    ``iot_request``, ``refresh_session``, ``token``). ``timeout`` bounds every HTTP call.
    """

    def __init__(self, cloud_client: Any, *, timeout: float = PASSTHROUGH_TIMEOUT_S):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._cloud = cloud_client
        self.timeout = float(timeout)
        self._relogin_lock = threading.Lock()

    def __repr__(self) -> str:
        return f"CloudPassthrough(timeout={self.timeout:g})"

    __str__ = __repr__

    def multiple_request(self, thing_name: str, app_server_url: str, reqs: list[dict]) -> list[dict]:
        """Send ``reqs`` to the camera ``thing_name`` in one ``multipleRequest``; return the items
        ``[{"method", "result", "error_code"}]`` exactly as the LAN client does. An empty batch sends
        nothing. Raises TapoConnectionError / TapoDeviceError / TapoProtocolError (see the module docstring)."""
        url = services_sync_url(app_server_url, thing_name)
        requests = list(reqs)
        if not requests:
            return []
        body = {"serviceId": "passthrough", "inputParams": {"requestData": {
            "method": "multipleRequest", "params": {"requests": requests}}}}
        for attempt in (1, 2):
            used = getattr(self._cloud, "token", None)
            try:
                return self._call(url, body)
            except _TokenRejected:
                if attempt == 2:
                    break
                self._refresh(used)
        raise TapoConnectionError(MSG_SESSION_EXPIRED)

    def _refresh(self, used_token: str | None) -> None:
        """Silent re-login, once for concurrent callers that saw the same rejected token."""
        with self._relogin_lock:
            if getattr(self._cloud, "token", None) != used_token:
                return  # another thread already signed in again
            refresh = getattr(self._cloud, "refresh_session", None)
            if refresh is None:
                raise TapoConnectionError(MSG_SESSION_EXPIRED)
            log.info("cloud passthrough: account token rejected; signing in again silently")
            refresh()

    def _call(self, url: str, body: dict) -> list[dict]:
        try:
            status, raw = self._cloud.iot_request("POST", url, body, timeout=self.timeout)
        except CloudError as exc:
            if exc.code == "bad_url":
                raise TapoProtocolError(str(exc)) from None
            if exc.code == "not_signed_in":
                raise TapoConnectionError("Not signed in to a TP-Link ID.") from None
            raise
        try:
            reply = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, ValueError):
            reply = None
        code = _cloud_code(reply)
        if status == 401 or code in _TOKEN_CODES:
            raise _TokenRejected()
        if status >= 400 or code:
            log.info("cloud passthrough: HTTP %s, code %s", status, code)
            raise _cloud_error(code, status)
        if not isinstance(reply, dict):
            raise TapoProtocolError("The TP-Link cloud sent an unreadable passthrough answer.")
        output = reply.get("outputParams")
        if output is None and isinstance(reply.get("result"), dict):
            output = reply["result"].get("outputParams")
        if not isinstance(output, dict):
            raise TapoProtocolError("The TP-Link cloud passthrough answer has no outputParams.")
        data = output.get("responseData", output)
        if not isinstance(data, dict):
            raise TapoProtocolError("The TP-Link cloud passthrough answer has no responseData.")
        inner = _code(data.get("error_code"))
        if inner:
            if inner in (ERR_DEVICE_OFFLINE, ERR_DEVICE_TIMEOUT):
                raise _cloud_error(inner, status)
            raise TapoDeviceError(inner, "multipleRequest")
        result = data.get("result")
        responses = result.get("responses") if isinstance(result, dict) else None
        if not isinstance(responses, list):
            raise TapoProtocolError("The camera's passthrough answer has no responses list.")
        return [_normalize_item(item) for item in responses]


class RemoteControlClient(ControlApi):
    """The :class:`~tapo_desktop.core.control_client.client.TapoControlClient` API for one camera through
    the cloud passthrough (owned cameras away from home, and shared cameras anywhere).

    No camera login is involved: ``login()`` is a no-op, ``logged_in`` is always True, ``close()`` only
    forgets nothing (the client is stateless apart from the remembered clip-search form).
    ``player_id`` (default: the per-process id) is used for the ``player_id`` clip-search form.
    """

    def __init__(self, passthrough: CloudPassthrough | Any, thing_name: str, app_server_url: str, *,
                 player_id: str | None = None):
        if not thing_name:
            raise ValueError("thing_name is required")
        self._passthrough = passthrough
        self._thing = str(thing_name)
        self._url = str(app_server_url or "")
        if player_id:
            self._player_id = str(player_id)

    def __repr__(self) -> str:
        return f"RemoteControlClient(thing={self._thing!r})"

    __str__ = __repr__

    @property
    def thing_name(self) -> str:
        return self._thing

    # ------------------------------------------------------------------ TapoControlClient surface
    def login(self) -> None:
        """No-op: commands are authorised by the account token, not by a camera login."""

    @property
    def logged_in(self) -> bool:
        return True

    @property
    def is_secure(self) -> bool:
        """Always True: the cloud path is HTTPS end to end (no plaintext LAN session)."""
        return True

    @property
    def protocol(self) -> str:
        return "cloud"

    @property
    def uses_local_token(self) -> bool:
        return False

    def reset_auth_backoff(self) -> None:
        """No-op (there is no camera login to back off from)."""

    def request(self, req: dict) -> dict:
        """Send one request and return its ``result``; non-zero item error_code -> TapoDeviceError."""
        items = self.multiple_request([req])
        if not items:
            raise TapoProtocolError(f"no response item for {req.get('method')}")
        item = items[0]
        if item["error_code"] != 0:
            raise TapoDeviceError(item["error_code"], item["method"] or req.get("method"))
        return item["result"]

    def multiple_request(self, reqs: list[dict]) -> list[dict]:
        """One cloud call for the whole batch; returns ``[{"method", "result", "error_code"}]``."""
        return self._passthrough.multiple_request(self._thing, self._url, reqs)

    def close(self) -> None:
        """Nothing to release (idempotent)."""
