"""Local mock of the Kami Home cloud REST gateway (lane A tests; plain HTTP on 127.0.0.1, no real server).

Follows ``specs/kami_auth_spec.json`` and ``specs/kami_cloud_spec.json``:

* ``GET /v4/users/login``: query ``seq, account|email, password, dev_*``; ``password`` must equal
  ``base64(HMAC-SHA256(app_key, password))`` [spec:kami.auth.password_hash]. Success is
  ``{"code": 20000, "data": {userid, token, token_secret, name, email, ...}}`` [spec:kami.auth.login_response].
* ``GET /v5/devices/list``: query ``seq, userid, hmac``; ``hmac`` must equal
  ``base64(HMAC-SHA1(token + "&" + token_secret, "seq=1&userid=<id>"))`` [spec:kami.auth.request_signing].
  A bad signature gets code 20202 (the code the app treats as "session invalid").

The mock re-implements both HMACs with ``hmac``/``hashlib`` on purpose, so it does not share code with the client.
It records every request (path, decoded params, headers) for the tests. The app key is a made-up value.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

MOCK_APP_KEY = "mock-app-key-not-the-real-one"
#: Stand-in for "login rejected". The real wrong-password code is unknown (kami_auth_spec unresolved).
REJECT_CODE = 30301
EXTRA_STEP_CODE = 20282  # [spec:kami.auth.result_codes] "handled as a social / extra step"
SESSION_INVALID_CODE = 20202


def make_device(uid: str, name: str = "", *, model: str = "1", online: bool = True, share: bool = False,
                **extra) -> dict:
    """One element of ``data[]`` of ``/v5/devices/list`` [spec:kami.cloud.device_fields]."""
    dev = {
        "uid": uid, "name": name or f"Kami {uid}", "model": model, "did": f"did-{uid}", "flag": True,
        "share": share, "category": 1 if share else 0, "count": 0, "type": 0, "interVersion": "1.0.0",
        "accessRight": 1 if share else 0, "state": 1 if online else 0, "online": online,
        "password": "ENCRYPTED-CAMERA-PASSWORD-DO-NOT-STORE", "relayFlag": 0,
        "ipcParam": json.dumps({"p2p_encrypt": True, "ssid": "Home WiFi A", "mac": "AA:BB:CC:00:00:01",
                                "ip": "192.168.1.50", "rssi": -40}),
    }
    if share:
        dev["nickname"] = "Friend A"
    dev.update(extra)
    return dev


class RecordedRequest:
    def __init__(self, path: str, params: dict[str, str], pairs: list[tuple[str, str]], headers: dict[str, str]):
        self.path, self.params, self.pairs, self.headers = path, params, pairs, headers


class MockKamiCloud:
    def __init__(self, *, app_key: str = MOCK_APP_KEY, region_label: str = ""):
        self.app_key = app_key
        self.region_label = region_label
        self.accounts: dict[str, dict] = {}
        self.devices: list = []
        self.requests: list[RecordedRequest] = []
        self.login_reject_code = REJECT_CODE
        self.login_delay = 0.0  # seconds the login reply is held back (to test a click while one is running)
        self.http_status: dict[str, int] = {}  # path -> forced HTTP status
        self.raw_body: dict[str, bytes] = {}  # path -> forced raw body (malformed JSON etc.)
        self._sessions: dict[str, tuple[str, str]] = {}  # userid -> (token, token_secret)
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ setup
    def add_account(self, email: str, password: str, *, userid: str = "u-100", extra_step_code: int | None = None):
        """``extra_step_code``: this account (e.g. a social-only one) always gets that result code at login."""
        self.accounts[email] = {"password": password, "userid": userid, "extra": extra_step_code}

    def start(self) -> "MockKamiCloud":
        mock = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):  # silence
                pass

            def do_GET(self):  # noqa: N802
                mock._handle(self)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="mock-kami")
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    # ------------------------------------------------------------------ inspection
    def count(self, path: str) -> int:
        with self._lock:
            return sum(1 for r in self.requests if r.path == path)

    @property
    def login_count(self) -> int:
        return self.count("/v4/users/login")

    @property
    def list_count(self) -> int:
        return self.count("/v5/devices/list")

    def last_session(self, userid: str = "u-100") -> tuple[str, str] | None:
        return self._sessions.get(userid)

    def expire_sessions(self) -> None:
        """Make every stored token invalid (the server forgot the session)."""
        self._sessions.clear()

    # ------------------------------------------------------------------ server side
    def _send(self, h: BaseHTTPRequestHandler, status: int, body: bytes) -> None:
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    def _reply(self, h: BaseHTTPRequestHandler, path: str, doc) -> None:
        if path in self.raw_body:
            return self._send(h, self.http_status.get(path, 200), self.raw_body[path])
        self._send(h, self.http_status.get(path, 200), json.dumps(doc).encode("utf-8"))

    def _handle(self, h: BaseHTTPRequestHandler) -> None:
        parts = urlsplit(h.path)
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        headers = {k.lower(): v for k, v in h.headers.items()}
        with self._lock:
            self.requests.append(RecordedRequest(parts.path, dict(pairs), pairs, headers))
        if parts.path == "/v4/users/login":
            if self.login_delay:
                time.sleep(self.login_delay)
            return self._reply(h, parts.path, self._login(dict(pairs)))
        if parts.path == "/v5/devices/list":
            return self._reply(h, parts.path, self._list(pairs))
        self._send(h, 404, b"{}")

    def _login(self, p: dict[str, str]) -> dict:
        email = p.get("account") or p.get("email") or p.get("mobile") or ""
        acct = self.accounts.get(email)
        if acct is not None and acct["extra"] is not None:
            return {"code": acct["extra"], "msg": "extra step"}
        digest = hmac.new(self.app_key.encode(), (self._pw_for(acct) or "\x00").encode(), hashlib.sha256).digest()
        if acct is None or p.get("password") != base64.b64encode(digest).decode():
            return {"code": self.login_reject_code, "msg": "rejected"}
        token, secret = secrets.token_hex(16), secrets.token_hex(16)
        self._sessions[acct["userid"]] = (token, secret)
        return {"code": 20000, "data": {
            "userid": acct["userid"], "token": token, "token_secret": secret, "name": "Owner A", "email": email,
            "img": "", "register_time": 1, "flag": True, "aiOptionFlag": 0}}

    @staticmethod
    def _pw_for(acct: dict | None) -> str | None:
        return acct["password"] if acct else None

    def _list(self, pairs: list[tuple[str, str]]) -> dict:
        params = dict(pairs)
        got = params.get("hmac")
        signed = [(k, v) for k, v in pairs if k != "hmac"]
        session = self._sessions.get(params.get("userid", ""))
        if session is None or got is None:
            return {"code": SESSION_INVALID_CODE, "msg": "invalid session"}
        message = "&".join(f"{k}={v}" for k, v in signed)
        key = f"{session[0]}&{session[1]}".encode()
        want = base64.b64encode(hmac.new(key, message.encode(), hashlib.sha1).digest()).decode()
        if not hmac.compare_digest(want, got):
            return {"code": SESSION_INVALID_CODE, "msg": "bad signature"}
        return {"code": 20000, "data": list(self.devices)}
