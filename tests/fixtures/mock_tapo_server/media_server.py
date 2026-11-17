"""Media :8800 (POST /stream) of the mock camera ([spec:stream.local8800.*], [spec:vod.*]).

Handshake: first POST (no Authorization) -> 401 + ``WWW-Authenticate: Digest realm=..., qop="auth",
nonce=..., opaque=..., algorithm=MD5|SHA-256[, encrypt_type=3]`` (socket stays open). Second POST
with ``Authorization: Digest`` (user "admin", password' = SHA256U(cloud) when encrypt_type=3 else
MD5U(cloud)) -> ``200`` + ``Key-Exchange: username="admin" nonce="<hex>"``. The socket then carries
multipart parts both ways. Media parts are AES-128-CBC/PKCS7 per part with key=MD5(nonce:pwd'),
iv=MD5(admin:nonce) and ``X-If-Encrypt: 1`` (only when the client sent ``X-Key-Exchange: 1``).

Supported control requests: ``get download`` and ``get playback`` (stream the cached MPEG-TS clip
in ``media_chunk_size`` parts, then a ``stream_status``/``finished`` notification) and
``do stop``. Flow control: the sender stalls when more than ``media_ack_window`` parts are
un-acknowledged ([spec:vod.stream.flow_control_ack]); after ``media_ack_timeout`` seconds without an
ack the camera sends ``connection_closed`` (close_reason "data timeout") and closes the socket.
More than ``max_media_sessions`` authenticated connections -> ``HTTP 503`` body
``{"error_code": -52407}``.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import socket
import threading
import time
from typing import TYPE_CHECKING

from . import crypto, media_assets
from ._net import TcpServer
from .rtsp_server import parse_auth_params

if TYPE_CHECKING:
    from .camera import MockTapoCamera

REALM = "TP-Link Streaming Media"
DEVICE_BOUNDARY = b"--device-stream-boundary--"
CLIENT_BOUNDARY = b"--client-stream-boundary--"


class _Closed(Exception):
    pass


class _Reader:
    def __init__(self, sock: socket.socket, stopping: threading.Event):
        self.sock, self.stopping, self.buf = sock, stopping, b""

    def _fill(self) -> None:
        while True:
            if self.stopping.is_set():
                raise _Closed
            try:
                chunk = self.sock.recv(65536)
            except TimeoutError:
                continue
            except OSError:
                raise _Closed from None
            if not chunk:
                raise _Closed
            self.buf += chunk
            return

    def line(self) -> bytes:
        while b"\r\n" not in self.buf:
            if len(self.buf) > 65536:
                raise _Closed
            self._fill()
        ln, _, self.buf = self.buf.partition(b"\r\n")
        return ln

    def exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            self._fill()
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def headers(self) -> dict[str, str]:
        h: dict[str, str] = {}
        while True:
            ln = self.line()
            if not ln:
                return h
            k, _, v = ln.decode("utf-8", "replace").partition(":")
            h[k.strip().lower()] = v.strip()


class _Session:
    def __init__(self, server: "MediaServer", sock: socket.socket):
        self.server, self.cam, self.sock = server, server.cam, sock
        self.wlock = threading.Lock()
        self.encrypt = False
        self.key = self.iv = b""
        self.authenticated = False
        self.session_id: str | None = None
        self.sender: threading.Thread | None = None
        self.stop_stream = threading.Event()
        self.ack_cond = threading.Condition()
        self.last_ack = 0

    # -- output ---------------------------------------------------------------------------
    def send(self, data: bytes) -> None:
        with self.wlock:
            view, deadline = memoryview(data), time.monotonic() + 5
            while view:
                try:
                    view = view[self.sock.send(view):]
                except TimeoutError:
                    if time.monotonic() > deadline:
                        raise
                    continue

    def send_part(self, headers: dict[str, str], body: bytes) -> None:
        head = b"--" + DEVICE_BOUNDARY + b"\r\n"
        for k, v in headers.items():
            head += f"{k}: {v}\r\n".encode()
        head += f"Content-Length: {len(body)}\r\n\r\n".encode()
        self.send(head + body + b"\r\n")

    def send_json(self, obj: dict) -> None:
        hdr = {"Content-Type": "application/json"}
        if self.session_id:
            hdr["X-Session-Id"] = self.session_id
        self.send_part(hdr, crypto.compact_json(obj).encode("utf-8"))

    def http(self, status: str, headers: list[str], body: bytes = b"") -> None:
        lines = [f"HTTP/1.1 {status}", "Server: mock-tapo-media", *headers]
        if body or not status.startswith("200"):
            lines.append(f"Content-Length: {len(body)}")
        self.send(("\r\n".join(lines) + "\r\n\r\n").encode() + body)

    # -- handshake ------------------------------------------------------------------------
    def _hash(self, alg: str, s: str) -> str:
        return (hashlib.sha256 if alg.upper().startswith("SHA-256") else hashlib.md5)(s.encode()).hexdigest()

    def _challenge(self) -> None:
        nonce = secrets.token_hex(16)
        opaque = secrets.token_hex(8)
        self.server.nonces[nonce] = opaque
        alg = self.cam.media_digest_algorithm
        extra = ", encrypt_type=3" if self.cam.secure else ""
        self.http("401 Unauthorized", [
            f'WWW-Authenticate: Digest realm="{REALM}", qop="auth", nonce="{nonce}", opaque="{opaque}", '
            f"algorithm={alg}{extra}", "Content-Type: application/json"], b'{"error_code":-40401}')

    def _check_digest(self, auth: str, uri_hint: str) -> bool:
        if not auth.lower().startswith("digest "):
            return False
        p = parse_auth_params(auth[7:])
        alg = p.get("algorithm", "MD5")
        if alg.upper() != self.cam.media_digest_algorithm.upper():
            return False
        nonce = p.get("nonce", "")
        if p.get("username") != "admin" or p.get("realm") != REALM or nonce not in self.server.nonces:
            return False
        if p.get("opaque", self.server.nonces[nonce]) != self.server.nonces[nonce]:
            return False
        if p.get("qop") != "auth" or not p.get("cnonce") or not p.get("nc"):
            return False
        uri = p.get("uri", "")
        if not uri.startswith("/stream"):
            return False
        pwd_t = crypto.media_password(self.cam.cloud_password, self.cam.secure)
        ha1 = self._hash(alg, f"admin:{REALM}:{pwd_t}")
        ha2 = self._hash(alg, f"POST:{uri}")
        expected = self._hash(alg, f"{ha1}:{nonce}:{p['nc']}:{p['cnonce']}:{p['qop']}:{ha2}")
        return secrets.compare_digest(expected, p.get("response", "").lower())

    def run(self) -> None:
        reader = _Reader(self.sock, self.server.tcp.stopping)
        try:
            while not self.authenticated:
                req = reader.line()
                while not req:
                    req = reader.line()
                parts = req.decode("utf-8", "replace").split(" ")
                h = reader.headers()
                n = int(h.get("content-length", "0") or 0)
                if n:
                    reader.exact(n)
                if len(parts) < 2 or parts[0] != "POST" or not parts[1].startswith("/stream"):
                    self.http("404 Not Found", ["Connection: close"])
                    return
                auth = h.get("authorization")
                if not auth:
                    self._challenge()
                    continue
                if not self._check_digest(auth, parts[1]):
                    self.cam._count("media_auth_failures")
                    self._challenge()
                    continue
                if not self.server.try_acquire():
                    self.http("503 Service Unavailable", ["Content-Type: application/json", "Connection: close"],
                              b'{"error_code":-52407}')
                    return
                self.authenticated = True
                hdrs = ["Content-Type: multipart/mixed;boundary=" + DEVICE_BOUNDARY.decode(), "Connection: keep-alive"]
                if h.get("x-key-exchange") == "1":
                    knonce = secrets.token_hex(16)
                    pwd_t = crypto.media_password(self.cam.cloud_password, self.cam.secure)
                    self.key, self.iv = crypto.media_keys(knonce, pwd_t, "admin")
                    self.encrypt = True
                    hdrs.append(f'Key-Exchange: username="admin" nonce="{knonce}"')
                self.http("200 OK", hdrs)
            self._parts(reader)
        except _Closed:
            pass
        finally:
            self.stop_stream.set()
            with self.ack_cond:
                self.ack_cond.notify_all()
            if self.sender is not None:
                self.sender.join(3)
            if self.authenticated:
                self.server.release()

    # -- multipart control ----------------------------------------------------------------
    def _parts(self, reader: _Reader) -> None:
        while True:
            ln = reader.line()
            if not ln:
                continue
            if ln.startswith(b"--" + CLIENT_BOUNDARY + b"--"):
                return  # client close marker
            if CLIENT_BOUNDARY not in ln:
                continue
            h = reader.headers()
            body = reader.exact(int(h.get("content-length", "0") or 0))
            try:
                msg = json.loads(body.decode("utf-8")) if "json" in h.get("content-type", "") else None
            except (UnicodeDecodeError, ValueError):
                msg = None
            self.cam._log_media(h, msg)
            if "x-data-received" in h:
                try:
                    n = int(h["x-data-received"])
                except ValueError:
                    n = 0
                with self.ack_cond:
                    self.last_ack = max(self.last_ack, n)
                    self.ack_cond.notify_all()
                self.cam._media_acks.append(n)
            if isinstance(msg, dict) and msg.get("type") == "request":
                self._request(msg)

    def _request(self, msg: dict) -> None:
        seq = msg.get("seq")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        method = params.get("method")
        if method == "do" and "stop" in params:
            self.stop_stream.set()
            with self.ack_cond:
                self.ack_cond.notify_all()
            self.send_json({"type": "response", "seq": seq, "result": {"error_code": 0}})
            return
        verb = "download" if "download" in params else ("playback" if "playback" in params else None)
        if method != "get" or verb is None:
            self.send_json({"type": "response", "seq": seq, "result": {"error_code": -52402}})
            return
        if self.sender is not None and self.sender.is_alive():
            self.send_json({"type": "response", "seq": seq, "result": {"error_code": -52417}})
            return
        clip = self._find_clip(params[verb])
        if clip is None:
            self.send_json({"type": "response", "seq": seq, "result": {"error_code": -52402}})
            return
        self.session_id = str(self.server.next_session_id())
        result = {"error_code": 0, "session_id": self.session_id}
        if verb == "playback":
            result["speed"] = params[verb].get("scale", "1/1")
        self.send_json({"type": "response", "seq": seq, "result": result})
        self.stop_stream.clear()
        self.sender = threading.Thread(target=self._stream, name="mock-media-stream", daemon=True)
        self.sender.start()

    def _find_clip(self, p: dict):
        if not isinstance(p, dict):
            return None
        try:
            st = int(p["start_time"])
            et = int(p.get("end_time", st))
        except (KeyError, TypeError, ValueError):
            return None
        if not isinstance(p.get("start_time"), str) or st <= 0 or et < st:
            return None
        if p.get("download_type", "normal") != "normal" or p.get("media_type", 0) not in (0, "0"):
            return None
        with self.cam.state.lock:
            for c in self.cam.state.recordings:
                cs, ce = int(c.start.timestamp()), int(c.end.timestamp())
                if cs <= st < ce or (st <= cs and et > cs):
                    return c
        return None

    def _stream(self) -> None:
        try:
            ts = media_assets.load_ts()
        except (FileNotFoundError, RuntimeError):
            self.send_json({"type": "notification", "params": {"event_type": "stream_finish", "reason": "internal_error"}})
            return
        cam = self.cam
        chunk = max(188, (cam.media_chunk_size // 188) * 188)
        chunks = [ts[i:i + chunk] for i in range(0, len(ts), chunk)]
        total_pts = media_assets.TS_SECONDS * 90000
        try:
            for i, data in enumerate(chunks, start=1):
                if not self._wait_window(i):
                    return
                body = crypto.aes_cbc_encrypt(self.key, self.iv, data) if self.encrypt else data
                hdr = {"Content-Type": "video/mp2t", "X-If-Encrypt": "1" if self.encrypt else "0",
                       "X-Session-Id": self.session_id or "", "X-Data-Sequence": str(i),
                       "X-Data-PTS": str(int(total_pts * (i - 1) / len(chunks))),
                       "X-If-IFrame": "1" if i == 1 else "0", "X-Stream-Type": "0"}
                self.send_part(hdr, body)
                cam._count("media_parts_sent")
            if not self.stop_stream.is_set():
                self.send_json({"type": "notification", "params": {"event_type": "stream_status", "status": "finished"}})
        except OSError:
            pass

    def _wait_window(self, seq: int) -> bool:
        window = self.cam.media_ack_window
        deadline = time.monotonic() + self.cam.media_ack_timeout
        with self.ack_cond:
            while not self.stop_stream.is_set():
                if not window or seq - self.last_ack <= window:
                    return True
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self.ack_cond.wait(min(left, 0.1))
        if self.stop_stream.is_set():
            return False
        try:
            self.send_json({"type": "notification", "params": {"event_type": "connection_closed", "error_code": -1,
                                                               "close_reason": "data timeout"}})
        except OSError:
            pass
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        return False


class MediaServer:
    def __init__(self, cam: "MockTapoCamera", host: str, port: int):
        self.cam = cam
        self.tcp = TcpServer(host, port, self._handle, name="mock-media")
        self.port = self.tcp.port
        self.lock = threading.Lock()
        self.active = 0
        self.nonces: dict[str, str] = {}
        self._sid = 0

    def next_session_id(self) -> int:
        with self.lock:
            self._sid += 1
            return self._sid

    def try_acquire(self) -> bool:
        with self.lock:
            if self.active >= self.cam.max_media_sessions:
                return False
            self.active += 1
            return True

    def release(self) -> None:
        with self.lock:
            self.active = max(0, self.active - 1)

    def _handle(self, sock: socket.socket, addr) -> None:
        _Session(self, sock).run()

    def start(self) -> None:
        self.tcp.start()

    def stop(self) -> None:
        self.tcp.stop()
