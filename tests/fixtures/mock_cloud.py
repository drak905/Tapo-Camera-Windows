"""Mock TP-Link cloud (V2 REST account + device-list API) for Phase 2 tests.

Implements the subset of ``specs/cloud_account_spec.json`` the desktop client uses and VERIFIES
every request like the real server would: ``Content-MD5`` must be Base64(MD5(body)) and
``X-Authorization`` must carry ``Timestamp=9999999999``, the production AccessKey and a valid
HMAC-SHA1 signature over ``content_md5\\n9999999999\\nnonce\\npath`` [spec:cloud.signing].
Unsigned / badly signed requests get HTTP 401 ``{"errorCode": -1}``.

The signing check is implemented independently of ``tapo_desktop.core.cloud.signing`` on purpose.

Routes (``REGION_PREFIX`` = ``/eu`` emulates the regional ``appServerUrlV2``):
* ``POST /api/v2/account/getAccountStatusAndUrl`` [spec:cloud.account_status_and_url.request]
* ``POST [/eu]/api/v2/account/login`` and ``.../captchaLogin`` [spec:cloud.login.request]
  (``region_redirect=True``: login at the bootstrap URL answers -20212 with the regional URL)
* ``POST /eu/api/v2/account/get{Email,Sms,Push}VC4TerminalMFA`` and ``checkMFACodeAndLogin``
  [spec:cloud.mfa] (``mfa=True``: login answers -20677 until the terminal is bound)
* ``POST /eu/api/v2/common/getDeviceListByPage`` [spec:cloud.device_list] (``?token=`` required;
  unknown/expired token -> envelope errorCode -20651)
* ``POST /eu/api/v2/common/getAppServiceUrl`` [spec:lat.cloud.base_url] (signed, ``?token=``) ->
  ``serviceUrls`` with ``nbu.iot-app-server.app-v2`` = :attr:`MockTapoCloud.iot_url`
* ``GET /iot/v2/things?page=&pageSize=&deviceTypes=`` [spec:lat.source.thing_list] - NOT signed; checks
  the IoT app headers [spec:lat.cloud.headers] (``Authorization: ut|<token>``, ``app-cid``,
  ``x-app-name``, ``x-app-version``, ``x-term-id``, ``x-strict: 0``). Bad/expired token -> HTTP 401
  ``{"code": -20651, "message": ...}``; bad headers -> HTTP 400. Each ThingInfo carries
  ``localAccessToken`` from :attr:`MockTapoCloud.local_tokens` (deviceId -> LAT; absent -> null).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

# Production keys [spec:cloud.signing.keys] (deliberately duplicated, not imported).
ACCESS_KEY = "4d11b6b9d5ea4d19a829adbb9714b057"
SECRET_KEY = "6ed7d97f3e73467f8a5bab90b577ba4c"
REGION_PREFIX = "/eu"
IOT_PREFIX = "/iot"
IOT_SERVICE_ID = "nbu.iot-app-server.app-v2"
APP_TYPE = "TP-Link_Tapo_Android"

_AUTH_RE = re.compile(
    r"^Timestamp=(?P<ts>[^,]+), Nonce=(?P<nonce>[^,]+), AccessKey=(?P<ak>[^,]+), Signature=(?P<sig>[0-9a-f]+)$")


def make_cloud_device(index: int, **overrides: Any) -> dict:
    """A DeviceInfoResult-shaped dict [spec:cloud.device_list.response] with deterministic ids."""
    dev = {
        "deviceType": "SMART.IPCAMERA",
        "role": 0,
        "status": 1,
        "deviceName": "C210",
        "deviceModel": "C210",
        "deviceId": f"8022{index:036X}",
        "deviceMac": f"A0B1C2{index:06X}",
        "alias": f"Camera {index}",
        "deviceHwVer": "2.0",
        "fwVer": "1.3.11 Build 231121 Rel.41203n",
        "fwId": "0" * 32,
        "hwId": "0" * 32,
        "oemId": "0" * 32,
        "isSameRegion": True,
        "appServerUrl": "",
        "appServerUrlV2": "",
    }
    dev.update(overrides)
    return dev


class _Handler(BaseHTTPRequestHandler):
    server_version = "MockTapoCloud/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # keep pytest output quiet
        pass

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        cloud: MockTapoCloud = self.server.cloud  # type: ignore[attr-defined]
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length > 0 else b""
        parts = urlsplit(self.path)
        status, payload = cloud._handle(parts.path, parse_qs(parts.query), body, dict(self.headers.items()))
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json;charset=UTF-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        cloud: MockTapoCloud = self.server.cloud  # type: ignore[attr-defined]
        parts = urlsplit(self.path)
        if parts.path.startswith(IOT_PREFIX + "/"):
            status, payload = cloud._handle_iot_get(parts.path[len(IOT_PREFIX):], parse_qs(parts.query),
                                                    dict(self.headers.items()))
            data = json.dumps(payload).encode("utf-8")
        else:
            status, data = 405, b'{"errorCode":-1,"msg":"method not allowed"}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json;charset=UTF-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class MockTapoCloud:
    """In-process HTTP mock of the TP-Link cloud on ``127.0.0.1:<ephemeral>``.

    Mutable attributes (may be changed while running): ``accounts`` ({email: password}),
    ``devices`` (DeviceInfoResult dicts), ``mfa``, ``mfa_types``, ``mfa_code``, ``region_redirect``,
    ``locked`` (emails answering -20661), ``local_tokens`` ({deviceId: LAT}), ``things_max_page_size``
    (server-side cap of ``/v2/things`` pages). Inspection: ``request_log`` (list of dicts with
    ``path``, ``query``, ``body``, ``headers``, ``signature_ok``; GETs also have ``method="GET"``),
    ``sent_codes`` ([(mfa_type, email)]), ``bound_terminals``.
    """

    def __init__(self, accounts: dict[str, str] | None = None, devices: list[dict] | None = None,
                 mfa: bool = False, region_redirect: bool = False, *, mfa_types: list[int] | None = None,
                 mfa_code: str = "123456", nickname: str = "Tester", local_tokens: dict[str, str] | None = None):
        self.accounts: dict[str, str] = dict(accounts or {})
        self.devices: list[dict] = list(devices or [])
        self.mfa = mfa
        self.mfa_types: list[int] = list(mfa_types or [1, 2, 3])
        self.mfa_code = mfa_code
        self.region_redirect = region_redirect
        self.nickname = nickname
        self.locked: set[str] = set()
        self.local_tokens: dict[str, str] = dict(local_tokens or {})
        self.things_max_page_size = 100
        self.request_log: list[dict] = []
        self.sent_codes: list[tuple[int, str]] = []
        self.bound_terminals: set[str] = set()
        self._tokens: dict[str, str] = {}
        self._mfa_processes: dict[str, str] = {}  # process id -> email
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> "MockTapoCloud":
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        httpd.daemon_threads = True
        httpd.cloud = self  # type: ignore[attr-defined]
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.1},
                                        name="MockTapoCloud", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> "MockTapoCloud":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def port(self) -> int:
        assert self._httpd is not None, "MockTapoCloud not started"
        return self._httpd.server_address[1]

    @property
    def base_url(self) -> str:
        """Bootstrap URL (pass as ``TapoCloudClient(base_url=...)``)."""
        return f"http://127.0.0.1:{self.port}"

    @property
    def regional_url(self) -> str:
        """The ``appServerUrlV2`` the mock hands out."""
        return self.base_url + REGION_PREFIX

    @property
    def iot_url(self) -> str:
        """IoT app server URL handed out by ``getAppServiceUrl``."""
        return self.base_url + IOT_PREFIX

    def expire_tokens(self) -> None:
        """Invalidate every issued token (next device call answers -20651)."""
        with self._lock:
            self._tokens.clear()

    def valid_tokens(self) -> list[str]:
        with self._lock:
            return list(self._tokens)

    def calls(self, endpoint: str) -> list[dict]:
        """Logged requests whose path ends with ``endpoint`` (e.g. ``"/account/login"``)."""
        return [r for r in self.request_log if r["path"].endswith(endpoint)]

    # ---------------------------------------------------------------- request handling
    @staticmethod
    def _verify(path: str, body: bytes, headers: dict[str, str]) -> bool:
        hdr = {k.lower(): v for k, v in headers.items()}
        expected_md5 = base64.b64encode(hashlib.md5(body or b"{}").digest()).decode("ascii")
        if hdr.get("content-md5") != expected_md5:
            return False
        m = _AUTH_RE.match(hdr.get("x-authorization", ""))
        if not m or m["ts"] != "9999999999" or m["ak"] != ACCESS_KEY:
            return False
        msg = f"{expected_md5}\n{m['ts']}\n{m['nonce']}\n{path}".encode("utf-8")
        sig = hmac.new(SECRET_KEY.encode("utf-8"), msg, hashlib.sha1).hexdigest()
        return hmac.compare_digest(sig, m["sig"])

    def _handle(self, path: str, query: dict[str, list[str]], body: bytes,
                headers: dict[str, str]) -> tuple[int, dict]:
        ok = self._verify(path, body, headers)
        try:
            params = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            params = None
        q = {k: v[0] for k, v in query.items()}
        with self._lock:
            self.request_log.append({"path": path, "query": q, "body": params, "headers": headers,
                                     "signature_ok": ok})
        if not ok:
            return 401, {"errorCode": -1, "msg": "signature verification failed"}
        if not isinstance(params, dict):
            return 400, {"errorCode": -20104, "msg": "bad json"}
        if q.get("appName") != APP_TYPE or not q.get("termID"):
            return 200, {"errorCode": -20104, "msg": "APP_PARAMETER_NOT_FOUND"}

        regional = path.startswith(REGION_PREFIX + "/")
        route = path[len(REGION_PREFIX):] if regional else path
        if route == "/api/v2/account/getAccountStatusAndUrl":
            return 200, self._status_and_url(params)
        if route in ("/api/v2/account/login", "/api/v2/account/captchaLogin"):
            if not regional and self.region_redirect:
                return 200, self._ok({"errorCode": "-20212", "errorMsg": "wrong region",
                                      "appServerUrlV2": self.regional_url,
                                      "appServerUrl": self.regional_url + "/v1"})
            return 200, self._login(params, captcha=route.endswith("captchaLogin"))
        if not regional:
            return 404, {"errorCode": -1, "msg": "not found on bootstrap host"}
        if route in ("/api/v2/account/getEmailVC4TerminalMFA", "/api/v2/account/getSmsVC4TerminalMFA",
                     "/api/v2/account/getPushVC4TerminalMFA"):
            return 200, self._send_code(route, params)
        if route == "/api/v2/account/checkMFACodeAndLogin":
            return 200, self._check_mfa(params, q)
        if route == "/api/v2/common/getDeviceListByPage":
            return 200, self._device_page(q, params)
        if route == "/api/v2/common/getAppServiceUrl":
            return 200, self._service_urls(q, params)
        return 404, {"errorCode": -1, "msg": "not found"}

    @staticmethod
    def _ok(result: dict) -> dict:
        return {"errorCode": 0, "msg": "Success", "result": result}

    def _status_and_url(self, params: dict) -> dict:
        if params.get("appType") != APP_TYPE or not params.get("cloudUserName"):
            return {"errorCode": -20104, "msg": "APP_PARAMETER_NOT_FOUND"}
        if params["cloudUserName"] not in self.accounts:
            return {"errorCode": -20600, "msg": "Account not found"}
        url = self.base_url if self.region_redirect else self.regional_url
        return self._ok({"appServerUrl": url + "/v1", "appServerUrlV2": url, "accountApiUrl": self.base_url,
                         "status": 1, "deletePending": 0, "lockedMinutes": "0", "serviceRegion": "eu-west-1"})

    def _login_result(self, email: str) -> dict:
        token = secrets.token_hex(16)
        with self._lock:
            self._tokens[token] = email
        return {"errorCode": "0", "token": token, "accountId": "12345678", "nickname": self.nickname,
                "email": email, "regionCode": "EU", "registerType": "email",
                "appServerUrlV2": self.regional_url}

    def _login(self, params: dict, *, captcha: bool) -> dict:
        email, password = params.get("cloudUserName"), params.get("cloudPassword")
        required = ("appType", "appVersion", "cloudUserName", "cloudPassword", "terminalUUID", "platform")
        if any(not params.get(k) for k in required) or params.get("appType") != APP_TYPE:
            return {"errorCode": -20104, "msg": "APP_PARAMETER_NOT_FOUND"}
        if email in self.locked:
            return self._ok({"errorCode": "-20661", "errorMsg": "account locked", "lockedMinutes": 30})
        if email not in self.accounts:
            return self._ok({"errorCode": "-20600", "errorMsg": "account not found"})
        if self.accounts[email] != password:
            return self._ok({"errorCode": "-20601", "errorMsg": "credential mismatch", "failedAttempts": 1,
                             "remainAttempts": 4})
        if self.mfa and params["terminalUUID"] not in self.bound_terminals:
            pid = secrets.token_hex(8)
            with self._lock:
                self._mfa_processes[pid] = email
            return self._ok({"errorCode": "-20677", "errorMsg": "MFA required", "MFAProcessId": pid,
                             "supportedMFATypes": list(self.mfa_types), "mfaEmail": "u***@example.com"})
        return self._ok(self._login_result(email))

    def _send_code(self, route: str, params: dict) -> dict:
        email = params.get("cloudUserName")
        if email not in self.accounts or self.accounts[email] != params.get("cloudPassword") \
                or not params.get("terminalUUID"):
            return {"errorCode": -20601, "msg": "credential mismatch"}
        mfa_type = 2 if "Email" in route else 4 if "Sms" in route else 1
        with self._lock:
            self.sent_codes.append((mfa_type, email))
        return self._ok({})

    def _check_mfa(self, params: dict, q: dict[str, str]) -> dict:
        pid = params.get("MFAProcessId")
        with self._lock:
            email = self._mfa_processes.get(pid or "")
        if email is None or email != params.get("cloudUserName") or params.get("appType") != APP_TYPE:
            return self._ok({"errorCode": "-20676", "errorMsg": "MFA process not found"})
        if params.get("MFAType") not in self.mfa_types or str(params.get("code")) != self.mfa_code:
            return self._ok({"errorCode": "-20676", "errorMsg": "verification code incorrect"})
        with self._lock:
            self._mfa_processes.pop(pid, None)
        # terminalBindEnabled trusts this terminal (query termID) so later logins skip MFA.
        if params.get("terminalBindEnabled") is True and q.get("termID"):
            with self._lock:
                self.bound_terminals.add(q["termID"])
        return self._ok(self._login_result(email))

    def _device_page(self, q: dict[str, str], params: dict) -> dict:
        with self._lock:
            email = self._tokens.get(q.get("token", ""))
        if email is None:
            return {"errorCode": -20651, "msg": "Token expired"}
        try:
            index = int(params.get("index", 0))
            limit = min(int(params.get("limit", 20)), 50)
        except (TypeError, ValueError):
            return {"errorCode": -20104, "msg": "bad paging"}
        types = params.get("deviceTypeList")
        devices = [d for d in self.devices if not types or d.get("deviceType") in types]
        page = devices[index:index + limit] if limit > 0 else []
        return self._ok({"currentIndex": index, "totalNum": len(devices), "deviceList": page})

    def _service_urls(self, q: dict[str, str], params: dict) -> dict:
        with self._lock:
            email = self._tokens.get(q.get("token", ""))
        if email is None:
            return {"errorCode": -20651, "msg": "Token expired"}
        ids = params.get("serviceIds")
        if not isinstance(ids, list) or not ids:
            return {"errorCode": -20104, "msg": "serviceIds missing"}
        urls = {sid: self.iot_url for sid in ids if sid == IOT_SERVICE_ID}
        return self._ok({"regionCode": "EU", "serviceUrls": urls})

    # ---------------------------------------------------------------- IoT app server (GET, unsigned)
    def _handle_iot_get(self, path: str, query: dict[str, list[str]],
                        headers: dict[str, str]) -> tuple[int, dict]:
        q = {k: v[0] for k, v in query.items()}
        hdr = {k.lower(): v for k, v in headers.items()}
        with self._lock:
            self.request_log.append({"method": "GET", "path": IOT_PREFIX + path, "query": q, "body": None,
                                     "headers": headers, "signature_ok": None})
        if path != "/v2/things":
            return 404, {"code": -1, "message": "not found"}
        term = hdr.get("x-term-id", "")
        if (not term or hdr.get("x-app-name") != APP_TYPE or hdr.get("x-strict") != "0"
                or hdr.get("app-cid") != f"app:{APP_TYPE}:{term}" or not hdr.get("x-app-version")):
            return 400, {"code": -20104, "message": "missing app headers"}
        auth = hdr.get("authorization", "")
        with self._lock:
            email = self._tokens.get(auth[3:]) if auth.startswith("ut|") else None
        if email is None:
            return 401, {"code": -20651, "message": "Token expired", "data": None}
        try:
            page = int(q.get("page", "0"))
            size = min(int(q.get("pageSize", "20")), int(self.things_max_page_size))
        except ValueError:
            return 400, {"code": -20104, "message": "bad paging"}
        types = [s for s in q.get("deviceTypes", "").split(",") if s]
        things = [self._thing(d) for d in self.devices if not types or d.get("deviceType") in types]
        chunk = things[page * size:(page + 1) * size] if size > 0 and page >= 0 else []
        return 200, {"data": chunk, "page": page, "pageSize": size, "total": len(things)}

    def _thing(self, d: dict) -> dict:
        """ThingInfo-shaped dict [spec:lat.source.thing_list] (thingName == deviceId)."""
        return {"thingName": d["deviceId"], "localAccessToken": self.local_tokens.get(d["deviceId"]),
                "appServerUrl": self.regional_url, "appServerUrlV2": self.regional_url,
                "ownerAccountId": "12345678", "role": d.get("role", 0), "deviceType": d.get("deviceType"),
                "model": d.get("deviceModel"), "mac": d.get("deviceMac"), "fwVer": d.get("fwVer"),
                "hwVer": d.get("deviceHwVer"), "region": "eu-west-1", "status": d.get("status", 1)}
