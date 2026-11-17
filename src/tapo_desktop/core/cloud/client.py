"""TP-Link ID cloud client: sign-in, MFA, silent re-login and camera list [spec:cloud.*].

Flow (same as the Tapo Android app 3.22.806, ``TCAccountRepository`` / ``TCDeviceRepository``):

1. ``POST {bootstrap}/api/v2/account/getAccountStatusAndUrl`` -> ``appServerUrlV2`` (regional URL)
   [spec:cloud.account_status_and_url.request].
2. ``POST {regional}/api/v2/account/login`` with ``LoginV2Params`` [spec:cloud.login.request].
   ``-20212`` carries the right ``appServerUrlV2``: store it and log in there once more.
   ``-20677`` means MFA [spec:cloud.mfa]: ``request_mfa_code`` + ``complete_mfa``
   (``checkMFACodeAndLogin`` with ``terminalBindEnabled: true`` so the terminal is trusted).
3. ``POST {regional}/api/v2/common/getDeviceListByPage?token=..`` pages of 20 [spec:cloud.device_list].
   ``-20651`` (token expired) -> ``captchaLogin`` with the stored password once, then retry
   [spec:cloud.token_refresh]; ``-20652`` / ``-20675`` -> one full login.
4. Local access tokens [spec:lat.*]: ``POST {regional}/api/v2/common/getAppServiceUrl`` (signed)
   resolves the IoT app server (``nbu.iot-app-server.app-v2``); ``GET {iot}/v2/things`` (NOT signed,
   ``Authorization: ut|<token>`` + app headers) pages the thing list, whose ``localAccessToken`` is the
   per-camera secret of cameras in LAT mode (:meth:`TapoCloudClient.local_access_tokens`).

Every request is signed (:mod:`.signing`) and carries the app's common query parameters
[spec:cloud.common_query_params]. TLS is verified against the system store plus TP-Link's private
cloud roots shipped in the APK (:mod:`._trust`). The password and token are kept in memory only (the
caller persists the password in the DPAPI vault) and never appear in logs, reprs or exceptions.

Alias decoding: ``DeviceInfoResult.alias`` has no Base64 adapter in this APK, but older cloud
paths returned Base64 aliases. :func:`decode_alias` therefore accepts a value as Base64 only when it
is canonical padded Base64 that decodes to printable UTF-8; anything else is kept verbatim.
"""
from __future__ import annotations

import base64
import binascii
import http.client
import json
import locale as _locale
import logging
import platform as _platform
import socket
import ssl
import threading
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

from tapo_desktop.core.cloud._trust import TPLINK_CLOUD_CA_PEMS
from tapo_desktop.core.cloud.signing import sign_headers
from tapo_desktop.core.discovery.matcher import normalise_mac
from tapo_desktop.core.errors import TapoConnectionError, TapoError
from tapo_desktop.models import AccountInfo, CloudDevice

log = logging.getLogger(__name__)

APP_TYPE = "TP-Link_Tapo_Android"
DEFAULT_BASE_URL = "https://n-wap.i.tplinkcloud.com"
# Platform / terminal strings: the server's validation for non-Android clients is unknown
# [spec unresolved: terminalName on desktop]; "platform" mimics the app, "terminalName" is what the
# user sees in the TP-Link ID "trusted devices" list.
PLATFORM = "Android 14"
TERMINAL_NAME = "Tapo Desktop (Windows PC)"
PAGE_LIMIT = 20
_MAX_PAGES = 500
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
# Requested like the app (TCDeviceRepository); hubs are dropped client-side (not cameras).
REQUEST_DEVICE_TYPES = ("SMART.IPCAMERA", "SMART.TAPODOORBELL", "SMART.TAPOHUB")
CAMERA_DEVICE_TYPES = frozenset({"SMART.IPCAMERA", "SMART.TAPODOORBELL"})

ERR_REGION_REDIRECT = -20212
ERR_TOKEN_EXPIRED = -20651
ERR_TOKEN_MISMATCH = -20652
ERR_LOGGED_IN_ELSEWHERE = -20675
ERR_MFA_REQUIRED = -20677
_TOKEN_ERRORS = frozenset({ERR_TOKEN_EXPIRED, ERR_TOKEN_MISMATCH, ERR_LOGGED_IN_ELSEWHERE})
#: Service id of the IoT app server that serves the thing list with local access tokens
#: [spec:lat.cloud.base_url]; resolved through ``getAppServiceUrl``.
IOT_APP_SERVER_SERVICE_ID = "nbu.iot-app-server.app-v2"
THINGS_PAGE_SIZE = 100  # ThingCloudRepository: ThingListPageParams(0, 100)
_AUTH_MESSAGES = {
    -20004: "Sign-in failed. Check your email and password.",
    -20200: "That email address is not valid.",
    -20202: "That user name is not valid.",
    -20600: "No TP-Link ID exists for this email.",
    -20601: "Incorrect email or password.",
    -20602: "This TP-Link ID is not activated yet. Open the activation email first.",
    -20607: "Sign-in failed. Check your email and password.",
    -20615: "Incorrect email or password.",
    -20661: "Too many failed attempts. The account is locked.",
    -20676: "Verification failed. Check the code and try again.",
    -20681: "Sign-in failed. Try again later.",
    -20697: "Sign-in failed. Try again later.",
    -20708: "Sign-in failed. Try again later.",
}
_MFA_SEND_PATHS = {
    1: "/api/v2/account/getPushVC4TerminalMFA",
    2: "/api/v2/account/getEmailVC4TerminalMFA",
    4: "/api/v2/account/getSmsVC4TerminalMFA",
}
_MFA_TOTP = 3


class CloudError(TapoError):
    """The cloud answered with an error (``code``: cloud errorCode, HTTP status or a short tag)."""

    def __init__(self, message: str, code: int | str = "", result: dict | None = None):
        super().__init__(message)
        self.code = code
        self.result: dict = result or {}

    def __repr__(self) -> str:
        return f"{type(self).__name__}({str(self)!r}, code={self.code!r})"


class CloudAuthError(CloudError):
    """Wrong email/password (-20601/-20615), unknown or locked account (-20600/-20661), bad MFA code."""


class CloudMfaRequired(CloudError):
    """Login needs a verification code (-20677); ``mfa_types``: 1 push, 2 email, 3 TOTP, 4 SMS."""

    def __init__(self, process_id: str, mfa_types: list[int]):
        super().__init__("A verification code is required to sign in.", ERR_MFA_REQUIRED)
        self.process_id = process_id
        self.mfa_types = list(mfa_types)


def decode_alias(value: str) -> str:
    """Return the readable device alias: decoded when ``value`` is Base64 of printable UTF-8."""
    if not value or len(value) % 4 or len(value) < 4:
        return value or ""
    try:
        raw = base64.b64decode(value, validate=True)
        text = raw.decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return value
    if not text or not text.isprintable() or base64.b64encode(raw).decode("ascii") != value:
        return value
    return text


def make_ssl_context(verify_tls: bool = True) -> ssl.SSLContext:
    """TLS context trusting the system roots plus TP-Link's cloud roots (or no checks if disabled)."""
    if not verify_tls:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cadata="\n".join(TPLINK_CLOUD_CA_PEMS))
    return ctx


def _int_code(value: Any) -> int:
    """Cloud error codes arrive as int (envelope) or string (``result.errorCode``)."""
    try:
        return int(str(value).strip() or 0)
    except (TypeError, ValueError):
        return 0


def _error_for(code: int, msg: str, result: dict) -> CloudError:
    if code == ERR_MFA_REQUIRED:
        types = [t for t in (result.get("supportedMFATypes") or []) if isinstance(t, int)]
        return CloudMfaRequired(str(result.get("MFAProcessId") or ""), types)
    if code in _AUTH_MESSAGES:
        text = _AUTH_MESSAGES[code]
        if code == -20661 and result.get("lockedMinutes"):
            text = f"Too many failed attempts. The account is locked for {result['lockedMinutes']} minutes."
        elif code in (-20601, -20615) and isinstance(result.get("remainAttempts"), int):
            text += f" {result['remainAttempts']} attempts left."
        return CloudAuthError(text, code, result)
    if code == -23003:
        return CloudError("This app version is too old for the TP-Link cloud.", code, result)
    return CloudError(f"TP-Link cloud error {code}" + (f": {msg}" if msg else ""), code, result)


@dataclass
class _PendingMfa:
    email: str
    password: str
    region: str
    process_id: str
    mfa_types: list[int]


class TapoCloudClient:
    """Thread-safe TP-Link ID client. One instance per signed-in account."""

    def __init__(self, *, terminal_uuid: str, base_url: str = DEFAULT_BASE_URL, timeout: float = 15.0,
                 app_version: str = "3.22.806", verify_tls: bool = True):
        self.terminal_uuid = terminal_uuid
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self.app_version = app_version
        self.verify_tls = verify_tls
        self._ssl: ssl.SSLContext | None = None
        self._lock = threading.RLock()
        self._email: str | None = None
        self.__password: str | None = None
        self.__token: str | None = None
        self._region: str | None = None
        self._account: AccountInfo | None = None
        self.__mfa: _PendingMfa | None = None
        self._iot_url: str | None = None
        self._thing_online: dict[str, bool] = {}

    def __repr__(self) -> str:
        return (f"TapoCloudClient(base_url={self.base_url!r}, signed_in={self.__token is not None}, "
                f"mfa_pending={self.__mfa is not None})")

    __str__ = __repr__

    # ------------------------------------------------------------------ public API
    @property
    def token(self) -> str | None:
        """Current session token (``None`` until signed in)."""
        return self.__token

    @property
    def account(self) -> AccountInfo | None:
        """Signed-in account (``None`` until signed in)."""
        return self._account

    def login(self, email: str, password: str) -> AccountInfo:
        """Sign in with a TP-Link ID; raises CloudMfaRequired / CloudAuthError / CloudError /
        TapoConnectionError."""
        with self._lock:
            status = self._call(self.base_url, "/api/v2/account/getAccountStatusAndUrl",
                                {"appType": APP_TYPE, "cloudUserName": email})
            region = str(status.get("appServerUrlV2") or "").rstrip("/") or self.base_url
            try:
                result = self._password_login(region, email, password)
            except CloudError as exc:
                redirect = str(exc.result.get("appServerUrlV2") or "").rstrip("/")
                if exc.code != ERR_REGION_REDIRECT or not redirect or redirect == region:
                    self._remember_mfa(exc, email, password, region)
                    raise
                log.info("cloud login: region redirect (-20212)")
                region = redirect
                try:
                    result = self._password_login(region, email, password)
                except CloudError as exc2:
                    self._remember_mfa(exc2, email, password, region)
                    raise
            return self._accept_login(result, email, password, region)

    def request_mfa_code(self, mfa_type: int) -> None:
        """Ask the cloud to send a verification code (1 push, 2 email, 4 SMS; 3 TOTP sends nothing)."""
        with self._lock:
            pending = self._pending_mfa()
            if mfa_type == _MFA_TOTP:
                return
            path = _MFA_SEND_PATHS.get(mfa_type)
            if path is None:
                raise ValueError(f"unsupported MFA type {mfa_type}")
            body: dict[str, Any] = {"appType": APP_TYPE, "cloudUserName": pending.email,
                                    "cloudPassword": pending.password, "terminalUUID": self.terminal_uuid}
            if mfa_type in (2, 4):
                body["locale"] = self._locale()
            self._call(pending.region, path, body)

    def complete_mfa(self, code: str, mfa_type: int) -> AccountInfo:
        """Verify the code (``checkMFACodeAndLogin``) and finish signing in; trusts this terminal."""
        with self._lock:
            pending = self._pending_mfa()
            result = self._call(pending.region, "/api/v2/account/checkMFACodeAndLogin", {
                "appType": APP_TYPE, "cloudUserName": pending.email, "code": str(code).strip(),
                "MFAProcessId": pending.process_id, "MFAType": int(mfa_type), "terminalBindEnabled": True,
            })
            info = self._accept_login(result, pending.email, pending.password, pending.region)
            self.__mfa = None
            return info

    def list_cameras(self) -> list[CloudDevice]:
        """All camera-like devices of the account (every page), re-logging in once if the token expired."""
        with self._lock:
            if self.__token is None or self._region is None:
                raise CloudError("Not signed in to a TP-Link ID.", "not_signed_in")
            relogged = False
            devices: dict[str, CloudDevice] = {}
            index = 0
            for _ in range(_MAX_PAGES):
                body = {"deviceTypeList": list(REQUEST_DEVICE_TYPES), "index": index, "limit": PAGE_LIMIT}
                try:
                    page = self._call(self._region, "/api/v2/common/getDeviceListByPage", body, token=True)
                except CloudError as exc:
                    if relogged or exc.code not in (ERR_TOKEN_EXPIRED, ERR_TOKEN_MISMATCH, ERR_LOGGED_IN_ELSEWHERE):
                        raise
                    relogged = True
                    self._relogin(captcha=exc.code == ERR_TOKEN_EXPIRED)
                    continue
                items = page.get("deviceList") or []
                for item in items:
                    if isinstance(item, dict) and item.get("deviceType") in CAMERA_DEVICE_TYPES and item.get("deviceId"):
                        dev = self._parse_device(item)
                        devices.setdefault(dev.device_id, dev)
                total = _int_code(page.get("totalNum"))
                index += PAGE_LIMIT
                if not items or index >= total:
                    break
            log.info("cloud device list: %d cameras", len(devices))
            return list(devices.values())

    def get_service_urls(self, service_ids: list[str] | tuple[str, ...]) -> dict[str, str]:
        """``getAppServiceUrl``: service id -> regional URL [spec:lat.cloud.base_url].

        Signed POST to the account's app server with the token in the query; re-logs in once on
        token expiry. Non-string entries are dropped.
        """
        ids = [str(s) for s in service_ids if s]
        if not ids:
            return {}
        with self._lock:
            result = self._signed_call_with_relogin("/api/v2/common/getAppServiceUrl", {"serviceIds": ids})
            urls = result.get("serviceUrls")
            if not isinstance(urls, dict):
                return {}
            return {str(k): v.rstrip("/") for k, v in urls.items() if isinstance(v, str) and v}

    def local_access_tokens(self) -> dict[str, str]:
        """deviceId (thingName) -> local access token from the IoT thing list [spec:lat.source.thing_list].

        ``GET {iot-app-server}/v2/things`` with ``Authorization: ut|<token>`` and the app headers
        [spec:lat.cloud.headers], every page. Devices without a (non-empty string) token are left
        out; malformed entries are skipped. Token expiry -> one silent re-login, like
        :meth:`list_cameras`. Raises CloudError / TapoConnectionError.
        """
        with self._lock:
            self._require_signed_in()
            relogged = False
            while True:
                try:
                    return self._fetch_local_tokens()
                except CloudError as exc:
                    if relogged or exc.code not in _TOKEN_ERRORS:
                        raise
                    relogged = True
                    self._iot_url = None  # resolve again with the new session
                    self._relogin(captcha=exc.code == ERR_TOKEN_EXPIRED)

    # ------------------------------------------------------------------ IoT app server (LAT)
    def _require_signed_in(self) -> None:
        if self.__token is None or self._region is None:
            raise CloudError("Not signed in to a TP-Link ID.", "not_signed_in")

    def _signed_call_with_relogin(self, path: str, body: dict) -> dict:
        self._require_signed_in()
        assert self._region is not None
        try:
            return self._call(self._region, path, body, token=True)
        except CloudError as exc:
            if exc.code not in _TOKEN_ERRORS:
                raise
            self._relogin(captcha=exc.code == ERR_TOKEN_EXPIRED)
            return self._call(self._region, path, body, token=True)

    def _iot_server(self) -> str:
        """The IoT app server URL (``nbu.iot-app-server.app-v2``), cached per session."""
        if self._iot_url is None:
            url = self.get_service_urls([IOT_APP_SERVER_SERVICE_ID]).get(IOT_APP_SERVER_SERVICE_ID, "")
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise CloudError("The TP-Link cloud returned no IoT server address.", "no_iot_url")
            if urlsplit(self.base_url).scheme == "https" and parts.scheme != "https":
                raise CloudError("The TP-Link cloud returned an insecure IoT server address.", "bad_url")
            self._iot_url = url.rstrip("/")
        return self._iot_url

    def _iot_headers(self) -> dict[str, str]:
        """IoT cloud headers [spec:lat.cloud.headers] (never logged: carries the session token)."""
        return {"Authorization": f"ut|{self.__token}", "app-cid": f"app:{APP_TYPE}:{self.terminal_uuid}",
                "x-app-name": APP_TYPE, "x-app-version": self.app_version, "x-term-id": self.terminal_uuid,
                "x-ospf": PLATFORM, "x-net-type": "wifi", "x-strict": "0", "x-locale": self._locale(),
                "User-Agent": f"{APP_TYPE}/{self.app_version}(Windows PC/;wifi)",
                "Accept": "application/json", "Connection": "close"}

    @property
    def thing_online(self) -> dict[str, bool]:
        """deviceId -> online, from the last IoT thing list fetch (``status == 1``). The v2 device list's
        ``status`` is not a reliable online flag (it reads 0 for cameras that are streaming)."""
        return dict(self._thing_online)

    def _fetch_local_tokens(self) -> dict[str, str]:
        self._thing_online = {}
        base = self._iot_server()
        parts = urlsplit(base)
        tokens: dict[str, str] = {}
        seen = 0
        for page in range(_MAX_PAGES):
            query = urlencode([("page", page), ("pageSize", THINGS_PAGE_SIZE),
                               ("deviceTypes", ",".join(REQUEST_DEVICE_TYPES)), ("includePcDevice", "false"),
                               ("includeKasaShareDevices", "true"), ("includeMatterDevice", "false"),
                               ("includeExternalVendorDeviceInfo", "true")])
            target = f"{parts.path.rstrip('/')}/v2/things?{query}"
            status, raw = self._http_request("GET", parts.scheme, parts.hostname or "", parts.port, target, None,
                                             self._iot_headers(), "/v2/things")
            body = self._iot_json(status, raw)
            items = body.get("data")
            items = items if isinstance(items, list) else []
            for item in items:
                if not isinstance(item, dict):
                    continue
                status = item.get("status")
                name0 = item.get("thingName") or item.get("deviceId")
                if isinstance(name0, str) and name0 and isinstance(status, int) and not isinstance(status, bool):
                    self._thing_online[name0] = status == 1
                lat = item.get("localAccessToken")
                if not isinstance(lat, str) or not lat:
                    continue
                for key in ("thingName", "deviceId"):
                    name = item.get(key)
                    if isinstance(name, str) and name:
                        tokens[name] = lat
            seen += len(items)
            total = body.get("total")
            if not items:
                break
            if isinstance(total, int) and not isinstance(total, bool):
                if seen >= total:
                    break
            elif len(items) < THINGS_PAGE_SIZE:
                break
        log.info("IoT thing list: local access tokens for %d device(s)", len(tokens))
        return tokens

    @staticmethod
    def _iot_json(status: int, raw: bytes) -> dict:
        """Parse an IoT cloud reply; HTTP 401 / token codes -> CloudError(code=-20651 ...)."""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = None
        if status == 401:
            code = _int_code(body.get("code")) if isinstance(body, dict) else 0
            log.info("IoT cloud /v2/things: HTTP 401 (code %s)", code)
            raise CloudError("The TP-Link cloud session expired.", code if code in _TOKEN_ERRORS else ERR_TOKEN_EXPIRED)
        if not isinstance(body, dict):
            log.warning("IoT cloud /v2/things: HTTP %s with non-JSON body", status)
            raise CloudError(f"Unexpected answer from the TP-Link IoT cloud (HTTP {status}).",
                             status if status >= 400 else "malformed")
        code = _int_code(body.get("code")) if "code" in body else 0
        if status >= 400 or code != 0:
            log.info("IoT cloud /v2/things: HTTP %s, code %s", status, code)
            raise CloudError(f"TP-Link IoT cloud error {code or status}.", code or status)
        return body

    # ------------------------------------------------------------------ internals
    def _pending_mfa(self) -> _PendingMfa:
        if self.__mfa is None:
            raise CloudError("No verification is in progress; sign in first.", "no_mfa")
        return self.__mfa

    def _remember_mfa(self, exc: CloudError, email: str, password: str, region: str) -> None:
        if isinstance(exc, CloudMfaRequired):
            self.__mfa = _PendingMfa(email, password, region, exc.process_id, exc.mfa_types)
            log.info("cloud login: verification code required (types %s)", exc.mfa_types)

    def _login_body(self, email: str, password: str) -> dict[str, Any]:
        return {"appType": APP_TYPE, "appVersion": self.app_version, "cloudUserName": email,
                "cloudPassword": password, "platform": PLATFORM, "refreshTokenNeeded": False,
                "terminalUUID": self.terminal_uuid, "terminalName": TERMINAL_NAME, "terminalMeta": "1"}

    def _password_login(self, region: str, email: str, password: str, *, captcha: bool = False) -> dict:
        path = "/api/v2/account/captchaLogin" if captcha else "/api/v2/account/login"
        return self._call(region, path, self._login_body(email, password))

    def _accept_login(self, result: dict, email: str, password: str, region: str) -> AccountInfo:
        token = result.get("token")
        if not isinstance(token, str) or not token:
            raise CloudError("The TP-Link cloud returned no session token.", "malformed")
        self._email, self.__password, self.__token, self._region = email, password, token, region
        self._account = AccountInfo(email=str(result.get("email") or email),
                                    nickname=str(result.get("nickname") or ""),
                                    account_id=str(result.get("accountId") or ""), region_url=region)
        self.__mfa = None
        log.info("cloud login ok (region %s)", urlsplit(region).hostname)
        return self._account

    def _relogin(self, *, captcha: bool) -> None:
        """Silent re-login with the stored credentials [spec:cloud.token_refresh]."""
        if self._email is None or self.__password is None or self._region is None:
            raise CloudError("Session expired; sign in again.", ERR_TOKEN_EXPIRED)
        log.info("cloud token expired; signing in again silently")
        self.__token = None
        result = self._password_login(self._region, self._email, self.__password, captcha=captcha)
        self._accept_login(result, self._email, self.__password, self._region)

    def _parse_device(self, d: dict) -> CloudDevice:
        model = str(d.get("deviceModel") or d.get("deviceName") or "")
        raw_mac = str(d.get("deviceMac") or "")
        return CloudDevice(
            device_id=str(d["deviceId"]), mac=normalise_mac(raw_mac) or raw_mac.upper(), model=model,
            alias=decode_alias(str(d.get("alias") or "")) or str(d.get("deviceName") or "") or model,
            device_type=str(d.get("deviceType") or ""), online=_int_code(d.get("status")) != 0,
            fw_version=str(d.get("fwVer") or ""), hw_version=str(d.get("deviceHwVer") or ""),
            role=_int_code(d.get("role")), raw=dict(d))

    @staticmethod
    def _locale() -> str:
        try:
            loc = _locale.getlocale()[0] or ""
        except ValueError:
            loc = ""
        return loc if "_" in loc and len(loc) <= 8 else "en_US"

    def _query(self, *, token: bool) -> str:
        params = [("appName", APP_TYPE), ("appVer", self.app_version), ("netType", "wifi"),
                  ("termID", self.terminal_uuid), ("ospf", f"{_platform.system()} {_platform.release()}".strip()),
                  ("brand", "TPLINK"), ("locale", self._locale())]
        if token and self.__token:
            params.append(("token", self.__token))
        return urlencode(params)

    def _call(self, base: str, path: str, body: dict, *, token: bool = False) -> dict:
        """POST a signed JSON request; return ``result`` or raise CloudError / TapoConnectionError."""
        parts = urlsplit(base)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise CloudError(f"invalid cloud URL scheme {parts.scheme!r}", "bad_url")
        full_path = parts.path.rstrip("/") + path
        data = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json;charset=UTF-8", "Accept": "application/json",
                   "Connection": "close", **sign_headers(data, full_path)}
        status, raw = self._http_post(parts.scheme, parts.hostname, parts.port, f"{full_path}?{self._query(token=token)}",
                                      data, headers, path)
        try:
            env = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            env = None
        if not isinstance(env, dict):
            log.warning("cloud %s: HTTP %s with non-JSON body", path, status)
            raise CloudError(f"Unexpected answer from the TP-Link cloud (HTTP {status}).",
                             status if status >= 400 else "malformed")
        code = _int_code(env.get("errorCode"))
        result = env.get("result") if isinstance(env.get("result"), dict) else {}
        if code == 0:
            code = _int_code(result.get("errorCode"))
        if code != 0:
            msg = str(env.get("msg") or result.get("errorMsg") or "")
            log.info("cloud %s: errorCode %s", path, code)
            raise _error_for(code, msg, result)
        if status >= 400:
            log.info("cloud %s: HTTP %s", path, status)
            raise CloudError(f"TP-Link cloud HTTP error {status}.", status, result)
        log.debug("cloud %s: ok", path)
        return result

    def _http_post(self, scheme: str, host: str, port: int | None, target: str, data: bytes,
                   headers: dict[str, str], label: str) -> tuple[int, bytes]:
        return self._http_request("POST", scheme, host, port, target, data, headers, label)

    def _http_request(self, method: str, scheme: str, host: str, port: int | None, target: str,
                      data: bytes | None, headers: dict[str, str], label: str) -> tuple[int, bytes]:
        conn: http.client.HTTPConnection
        if scheme == "https":
            if self._ssl is None:
                self._ssl = make_ssl_context(self.verify_tls)
            conn = http.client.HTTPSConnection(host, port, timeout=self.timeout, context=self._ssl)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=self.timeout)
        try:
            conn.request(method, target, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_RESPONSE_BYTES:
                raise CloudError("The TP-Link cloud answer is too large.", "too_large")
            return resp.status, raw
        except (OSError, socket.timeout, http.client.HTTPException) as exc:
            # str(exc) of socket/TLS/http errors never contains the request (no secrets).
            log.info("cloud %s: connection failed (%s)", label, type(exc).__name__)
            raise TapoConnectionError(f"Cannot reach the TP-Link cloud ({type(exc).__name__}: {exc})") from None
        finally:
            conn.close()
