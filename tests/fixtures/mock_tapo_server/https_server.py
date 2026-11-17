"""HTTPS :443 control API of the mock camera ([spec:auth.*], [spec:envelope.*], [spec:control.*]).

Secure (encrypt_type "3") flow:
  POST /  login step 1 {cnonce, encrypt_type:"3", username:"admin"} -> -40413 + nonce + device_confirm
  POST /  login step 2 {cnonce, digest_passwd, ...}                  -> stok + start_seq
  POST /stok=<stok>/ds  headers seq + tapo_tag, body securePassthrough -> encrypted response
Legacy flow: POST / {"method":"login","params":{"hashed":true,"password":MD5U,"username":"admin"}}
  -> stok; then plaintext JSON on /stok=<stok>/ds.

Error behaviour (camera side):
* unknown / expired stok, missing or bad ``tapo_tag``, missing ``seq`` or ``seq`` lower than the
  next expected value (replay) -> HTTP 200 ``{"error_code": -40401}``
* undecodable securePassthrough payload / non-JSON body -> ``{"error_code": -40210}``
* step-2 wrong digest_passwd / legacy wrong password -> ``-40401``; locked -> ``-40404``
* legacy (non secure) camera answering a secure step 1 -> ``-40210`` (no -40413 challenge)

TPAP camera (``MockTapoCamera(protocol="tpap")``): login and ``/ds`` are handled by
:class:`tpap_server.TpapEndpoint` (SPAKE2+ login, binary AES-128-CCM ``/ds``); the v3 step 1 is
answered with ``-40211``. Default cameras are unaffected.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import re
import secrets
import ssl
import tempfile
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from . import crypto
from ._net import TrackedHTTPServer
from .dispatcher import dispatch
from .tpap_server import TpapEndpoint

if TYPE_CHECKING:
    from .camera import MockTapoCamera

_DS_RE = re.compile(r"^/stok=([^/]+)/ds/?$")
_CNONCE_RE = re.compile(r"^[0-9A-F]{16}$")


def generate_certificate(common_name: str = "C210") -> tuple[bytes, bytes, str]:
    """Self-signed EC P-256 cert -> (cert_pem, key_pem, sha256 hex lowercase of DER)."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "TP-Link (mock)")])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _dt.timedelta(days=1)).not_valid_after(now + _dt.timedelta(days=3650))
            .sign(key, hashes.SHA256()))
    der = cert.public_bytes(serialization.Encoding.DER)
    return (cert.public_bytes(serialization.Encoding.PEM),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()),
            hashlib.sha256(der).hexdigest())


def _ssl_context(cert_pem: bytes, key_pem: bytes) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    with tempfile.TemporaryDirectory(prefix="tapo_mock_cert_") as d:
        cp, kp = Path(d) / "c.pem", Path(d) / "k.pem"
        cp.write_bytes(cert_pem)
        kp.write_bytes(key_pem)
        ctx.load_cert_chain(cp, kp)
    return ctx


@dataclass
class SecureSession:
    stok: str
    password: str
    cnonce: str
    nonce: str
    lsk: bytes
    ivb: bytes
    next_seq: int
    tag_prefix: str


class HttpsServer:
    def __init__(self, cam: "MockTapoCamera", host: str, port: int):
        self.cam = cam
        self._ctx_lock = threading.Lock()
        self.rotate_certificate()
        self.server = TrackedHTTPServer((host, port), _Handler, name="mock-https", wrap=self._wrap)
        self.server.cam = cam  # type: ignore[attr-defined]
        self.server.api = self  # type: ignore[attr-defined]
        self.port = self.server.server_address[1]
        # session state
        self.lock = threading.Lock()
        self.pending: dict[str, str] = {}           # cnonce -> nonce (step 1 issued)
        self.sessions: dict[str, SecureSession] = {}
        self.legacy_stoks: set[str] = set()
        self.ds_request_count = 0
        self.tpap: TpapEndpoint | None = (TpapEndpoint(cam, self)
                                          if getattr(cam, "protocol", None) == "tpap" else None)

    # -- TLS -------------------------------------------------------------------------------
    def rotate_certificate(self) -> str:
        cert, key, fp = generate_certificate(self.cam.model)
        ctx = _ssl_context(cert, key)
        with self._ctx_lock:
            self.ctx, self.fingerprint, self.cert_pem = ctx, fp, cert
        return fp

    def _wrap(self, sock):
        with self._ctx_lock:
            ctx = self.ctx
        return ctx.wrap_socket(sock, server_side=True, do_handshake_on_connect=False)

    def start(self) -> None:
        self.server.start()

    def stop(self) -> None:
        self.server.stop()

    def expire_sessions(self) -> None:
        with self.lock:
            self.sessions.clear()
            self.legacy_stoks.clear()
        if self.tpap is not None:
            self.tpap.expire()

    # -- login -----------------------------------------------------------------------------
    def handle_login(self, params: Any) -> dict:
        if self.tpap is not None:
            return self.tpap.handle_login(params)
        cam = self.cam
        cam._count("login_attempts")
        if not isinstance(params, dict):
            return {"error_code": -40210}
        left = cam._lock_remaining()
        if left > 0:
            return {"error_code": -40404, "result": {"data": {"code": -40404, "sec_left": int(left) + 1}}}
        if params.get("username", "admin") != "admin":
            return self._failed()
        if cam.secure:
            return self._secure_login(params)
        return self._legacy_login(params)

    def _failed(self) -> dict:
        self.cam._register_failure()
        return {"error_code": -40401, "result": {"data": {"code": -40401}}}

    def _challenge(self, cnonce: str) -> dict:
        nonce = self.cam.fixed_nonce or secrets.token_hex(8).upper()
        with self.lock:
            self.pending[cnonce] = nonce
        return {"error_code": -40413, "result": {"data": {
            "code": -40401, "encrypt_type": ["3"], "key": secrets.token_urlsafe(24), "nonce": nonce,
            "device_confirm": crypto.device_confirm(self.cam.cloud_password, cnonce, nonce)}}}

    def _secure_login(self, params: dict) -> dict:
        cnonce = params.get("cnonce")
        if params.get("encrypt_type") != "3" or not isinstance(cnonce, str):
            # legacy-style login sent to a secure camera: advertise encryption, no nonce
            return {"error_code": -40413, "result": {"data": {"code": -40401, "encrypt_type": ["3"],
                                                              "key": secrets.token_urlsafe(24)}}}
        if not _CNONCE_RE.match(cnonce):
            return {"error_code": -40210}
        dp = params.get("digest_passwd")
        if dp is None:
            return self._challenge(cnonce)
        with self.lock:
            nonce = self.pending.pop(cnonce, None)
        if nonce is None:
            return self._challenge(cnonce)  # unknown / consumed nonce: fresh challenge
        pwd = self.cam.cloud_password
        if not isinstance(dp, str) or not hmac.compare_digest(dp, crypto.digest_passwd(pwd, cnonce, nonce)):
            return self._failed()
        lsk, ivb = crypto.session_keys(pwd, cnonce, nonce)
        stok = secrets.token_hex(16)
        start_seq = self.cam.fixed_start_seq if self.cam.fixed_start_seq is not None else secrets.randbelow(9000) + 100
        sess = SecureSession(stok, pwd, cnonce, nonce, lsk, ivb, start_seq, crypto.tag_prefix(pwd, cnonce))
        with self.lock:
            self.sessions[stok] = sess
        self.cam._count("login_count")
        self.cam._reset_failures()
        return {"error_code": 0, "result": {"stok": stok, "user_group": "root", "start_seq": start_seq}}

    def _legacy_login(self, params: dict) -> dict:
        if "password" not in params:
            return {"error_code": -40210}
        pw = params.get("password")
        expected = crypto.md5_upper(self.cam.cloud_password) if params.get("hashed") else self.cam.cloud_password
        if not isinstance(pw, str) or not hmac.compare_digest(pw, expected):
            return self._failed()
        stok = secrets.token_hex(16)
        with self.lock:
            self.legacy_stoks.add(stok)
        self.cam._count("login_count")
        self.cam._reset_failures()
        return {"error_code": 0, "result": {"stok": stok, "user_group": "root"}}

    # -- data ------------------------------------------------------------------------------
    def _expiry_fires(self) -> bool:
        n = self.cam._token_expiry_every
        with self.lock:
            self.ds_request_count += 1
            return bool(n) and self.ds_request_count % n == 0

    def handle_ds(self, stok: str, body: str, headers) -> dict:
        cam = self.cam
        with self.lock:
            sess = self.sessions.get(stok)
            legacy_ok = stok in self.legacy_stoks
        if cam.secure:
            if sess is None:
                return {"error_code": -40401}
            if self._expiry_fires():
                with self.lock:
                    self.sessions.pop(stok, None)
                return {"error_code": -40401}
            seq_h, tag_h = headers.get("seq"), headers.get("tapo_tag")
            try:
                seq = int(seq_h)
            except (TypeError, ValueError):
                return {"error_code": -40401}
            if not isinstance(tag_h, str):
                return {"error_code": -40401}
            expected = crypto.sha256_upper(sess.tag_prefix + body + str(seq))
            if not hmac.compare_digest(tag_h.strip().upper(), expected):
                return {"error_code": -40401}
            with self.lock:
                if seq < sess.next_seq:
                    return {"error_code": -40401}
                sess.next_seq = seq + 1
            try:
                outer = json.loads(body)
                if outer.get("method") != "securePassthrough":
                    return {"error_code": -40210}
                inner = json.loads(crypto.b64_decrypt(sess.lsk, sess.ivb, outer["params"]["request"]))
            except (ValueError, KeyError, TypeError, AttributeError):
                return {"error_code": -40210}
            reply = self.handle_inner(inner)
            return {"error_code": 0, "result": {"response": crypto.b64_encrypt(sess.lsk, sess.ivb,
                                                                               crypto.compact_json(reply))}}
        # legacy
        if not legacy_ok:
            return {"error_code": -40401}
        if self._expiry_fires():
            with self.lock:
                self.legacy_stoks.discard(stok)
            return {"error_code": -40401}
        try:
            inner = json.loads(body)
        except ValueError:
            return {"error_code": -40210}
        return self.handle_inner(inner)

    def handle_inner(self, inner: Any) -> dict:
        cam = self.cam
        cam._log_raw(inner)
        if not isinstance(inner, dict):
            return {"error_code": -40210}
        method, params = inner.get("method"), inner.get("params")
        if method == "multipleRequest":
            reqs = params.get("requests") if isinstance(params, dict) else None
            if not isinstance(reqs, list):
                return {"error_code": -40210}
            responses = []
            for r in reqs:
                m = r.get("method") if isinstance(r, dict) else None
                p = r.get("params") if isinstance(r, dict) else None
                code, result = self._one(m, p)
                responses.append({"method": m, "result": result, "error_code": code})
            return {"error_code": 0, "result": {"responses": responses}}
        code, result = self._one(method, params)
        return {"error_code": code, "method": method, "result": result}

    def _one(self, method: Any, params: Any) -> tuple[int, dict]:
        cam = self.cam
        cam._log_request(method, params)
        injected = cam._take_injected_error(method)
        if injected is not None:
            return injected, {}
        code, result = dispatch(cam.state, method, params)
        if code == 0 and method == "rebootDevice":
            # firmware reboots: every session is gone afterwards
            self.expire_sessions()
        return code, result


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "lighttpd"
    sys_version = ""
    timeout = 5.0  # idle keep-alive; stop() closes live connections actively

    def log_message(self, format, *args):  # noqa: A002
        pass

    def _send(self, status: int, payload: Any, content_type: str = "application/json; charset=UTF-8") -> None:
        data = (payload if isinstance(payload, bytes) else crypto.compact_json(payload).encode("utf-8"))
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()

    def do_GET(self):  # noqa: N802
        self._send(404, {"error_code": -40106})

    def do_POST(self):  # noqa: N802
        cam = self.server.cam  # type: ignore[attr-defined]
        api: HttpsServer = self.server.api  # type: ignore[attr-defined]
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        status = cam._take_http_status()
        if status is not None:
            self._send(status, {"error_code": -1})
            return
        if api.tpap is not None:
            m = _DS_RE.match(urlsplit(self.path).path)
            if m:  # TPAP /ds bodies are binary (uint32 seq || CCM ciphertext || tag)
                ctype, data = api.tpap.handle_ds(m.group(1), raw)
                self._send(200, data, ctype)
                return
        try:
            body = raw.decode("utf-8")
        except UnicodeDecodeError:
            self._send(200, {"error_code": -40210})
            return
        path = urlsplit(self.path).path
        if path in ("/", ""):
            try:
                req = json.loads(body)
            except ValueError:
                self._send(200, {"error_code": -40210})
                return
            if not isinstance(req, dict) or req.get("method") != "login":
                self._send(200, {"error_code": -40401})
                return
            self._send(200, api.handle_login(req.get("params")))
            return
        m = _DS_RE.match(path)
        if not m:
            self._send(404, {"error_code": -40106})
            return
        self._send(200, api.handle_ds(m.group(1), body, self.headers))
