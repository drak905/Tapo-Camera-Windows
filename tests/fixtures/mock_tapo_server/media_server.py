"""Media :8800 (POST /stream) of the mock camera ([spec:stream.local8800.*], [spec:vod.*]).

Handshake: first POST (no Authorization) -> 401 + ``WWW-Authenticate: Digest realm=..., qop="auth",
nonce=..., opaque=..., algorithm=MD5|SHA-256[, encrypt_type=3]`` (socket stays open). Second POST
with ``Authorization: Digest`` (user "admin", password' = SHA256U(cloud) when encrypt_type=3 else
MD5U(cloud)) -> ``200`` + ``Key-Exchange: username="admin" nonce="<hex>"``. The socket then carries
multipart parts both ways. Media parts are AES-128-CBC/PKCS7 per part with key=MD5(nonce:pwd'),
iv=MD5(admin:nonce) and ``X-If-Encrypt: 1`` (only when the client sent ``X-Key-Exchange: 1``).

Supported control requests: ``get download`` and ``get playback`` (stream the cached MPEG-TS clip
in ``media_chunk_size`` parts, then a ``stream_status``/``finished`` notification; Phase 5, additive:
with ``cam.media_playback_mode = "continuous"`` ``get playback`` streams the day's recordings from
``start_time`` to ``end_time`` paced by ``media_playback_speed`` with real PTS and honours ``do pause``,
``do play "null"`` (resume) and ``do play {start_time}`` (seek), see :mod:`.playback_feed`), ``get preview``
(live view [spec:stream.control.preview]: ``resolutions`` ``["HD"]`` -> 1280x720 clip, ``["VGA"]`` ->
640x360 clip, looped forever as one continuous live stream by :mod:`.ts_loop` and paced in real time
(``media_preview_speed`` multiplier), one part per video frame with ``X-If-IFrame``/``X-Data-PTS``,
until ``do stop`` or the client closes) and ``do stop``. ``MockTapoCamera.drop_media_connections()``
resets every :8800 connection. Flow control: the sender stalls when more than ``media_ack_window`` parts are
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

from . import crypto, media_assets, playback_feed, ts_loop
from ._net import TcpServer, rst_close
from .rtsp_server import parse_auth_params

if TYPE_CHECKING:
    from .camera import MockTapoCamera

REALM = "TP-Link Streaming Media"
DEVICE_BOUNDARY = b"--device-stream-boundary--"
CLIENT_BOUNDARY = b"--client-stream-boundary--"
PREVIEW_SIZES = {"HD": (1280, 720), "VGA": (640, 360)}  # clip used per preview resolution


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
        self.hkdf_keys: tuple[bytes, bytes] | None = None
        self.hkdf_parts = 0
        self.authenticated = False
        self.session_id: str | None = None
        self.sender: threading.Thread | None = None
        self.stop_stream = threading.Event()
        self.ack_cond = threading.Condition()
        self.last_ack = 0
        self.feed: playback_feed.PlaybackFeed | None = None  # continuous playback (Phase 5 knob)

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
        pwd_t = crypto.media_password(self.cam.device_secret, self.cam.secure)
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
                    pwd_t = crypto.media_password(self.cam.device_secret, self.cam.secure)
                    self.key, self.iv = crypto.media_keys(knonce, pwd_t, "admin")
                    self.encrypt = True
                    if getattr(self.cam, "media_kex_mode", "md5") == "hkdf":
                        salt = secrets.token_hex(16)
                        self.hkdf_keys = crypto.media_hkdf_keys(knonce, pwd_t, salt)
                        hdrs.append(f'Key-Exchange: cipher="AES_128_CBC" username="admin" padding="PKCS7_16" '
                                    f'algorithm="HKDF" encrypt_type="3" nonce="{knonce}" salt="{salt}"')
                    else:
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
    def _encrypt_part(self, data: bytes) -> tuple[bytes, dict]:
        """Encrypt one media part per the negotiated Key-Exchange mode (MD5 fixed IV or HKDF)."""
        if not self.encrypt:
            return data, {}
        if self.hkdf_keys is None:
            return crypto.aes_cbc_encrypt(self.key, self.iv, data), {}
        self.hkdf_parts += 1
        every = int(getattr(self.cam, "media_corrupt_hmac_every", 0) or 0)
        return crypto.media_hkdf_encrypt(*self.hkdf_keys, data,
                                         corrupt_hmac=bool(every) and self.hkdf_parts % every == 0)

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
        if method == "do" and self.feed is not None and ("pause" in params or "play" in params):
            self._feed_control(seq, params)
            return
        if method == "get" and "preview" in params:
            self._preview(seq, params["preview"])
            return
        verb = "download" if "download" in params else ("playback" if "playback" in params else None)
        if method != "get" or verb is None:
            self.send_json({"type": "response", "seq": seq, "result": {"error_code": -52402}})
            return
        if self.sender is not None and self.sender.is_alive():
            self.send_json({"type": "response", "seq": seq, "result": {"error_code": -52417}})
            return
        if verb == "playback" and getattr(self.cam, "media_playback_mode", "clip") == "continuous":
            self._start_continuous(seq, params[verb])
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
                body, extra = self._encrypt_part(data)
                hdr = {"Content-Type": "video/mp2t", "X-If-Encrypt": "1" if self.encrypt else "0", **extra,
                       "X-Session-Id": self.session_id or "", "X-Data-Sequence": str(i),
                       "X-Data-PTS": str(int(total_pts * (i - 1) / len(chunks))),
                       "X-If-IFrame": "1" if i == 1 else "0", "X-Stream-Type": "0"}
                self.send_part(hdr, body)
                cam._count("media_parts_sent")
            if not self.stop_stream.is_set():
                self.send_json({"type": "notification", "params": {"event_type": "stream_status", "status": "finished"}})
        except OSError:
            pass

    # -- continuous playback (Phase 5, ``media_playback_mode = "continuous"``) ---------------
    def _start_continuous(self, seq, p) -> None:
        """``get playback`` over the day's recordings from start_time to end_time, paced by
        ``media_playback_speed`` (None = unpaced, ack window only), real PTS, in-session controls."""
        try:
            st = int(p["start_time"])
            et = int(p.get("end_time", st + 86400))
        except (KeyError, TypeError, ValueError):
            st, et = 0, -1
        with self.cam.state.lock:
            recordings = playback_feed.recordings_of(self.cam.state.recordings)
        if (not isinstance(p.get("start_time"), str) or st <= 0 or et < st
                or not any(e > st and s < et for s, e in recordings)):
            self.send_json({"type": "response", "seq": seq, "result": {"error_code": -52402}})
            return
        self.stop_stream.clear()
        self.feed = playback_feed.PlaybackFeed(
            recordings, st, et, stop=self.stop_stream, speed=getattr(self.cam, "media_playback_speed", 1.0),
            pts_mode=getattr(self.cam, "media_playback_pts", "reset"))
        self.cam.playback_feeds.append(self.feed)
        self.session_id = str(self.server.next_session_id())
        self.send_json({"type": "response", "seq": seq,
                        "result": {"error_code": 0, "session_id": self.session_id, "speed": p.get("scale", "1/1")}})
        self.sender = threading.Thread(target=self._continuous_stream, args=(self.feed,),
                                       name="mock-media-playback", daemon=True)
        self.sender.start()

    def _feed_control(self, seq, params) -> None:
        feed = self.feed
        assert feed is not None
        if "pause" in params:
            feed.pause()
        elif params.get("play") == "null":
            feed.resume()
        elif isinstance(params.get("play"), dict) and str(params["play"].get("start_time", "")).isdigit():
            feed.seek(int(params["play"]["start_time"]))
        else:
            self.send_json({"type": "response", "seq": seq, "result": {"error_code": -52402}})
            return
        self.send_json({"type": "response", "seq": seq, "result": {"error_code": 0}})

    def _continuous_stream(self, feed: playback_feed.PlaybackFeed) -> None:
        try:
            for i, unit in enumerate(feed.units(), start=1):
                if not self._wait_window(i):
                    return
                body, extra = self._encrypt_part(unit.data)
                hdr = {"Content-Type": "video/mp2t", "X-If-Encrypt": "1" if self.encrypt else "0", **extra,
                       "X-Session-Id": self.session_id or "", "X-Data-Sequence": str(i),
                       "X-Data-PTS": str(unit.pts), "X-If-IFrame": "1" if unit.keyframe else "0",
                       "X-Stream-Type": "0"}
                self.send_part(hdr, body)
                self.cam._count("media_parts_sent")
            if feed.finished and not self.stop_stream.is_set():
                self.send_json({"type": "notification", "params": {"event_type": "stream_status", "status": "finished"}})
        except (OSError, FileNotFoundError, RuntimeError):
            pass

    # -- live preview [spec:stream.control.preview] ----------------------------------------
    def _preview(self, seq, p) -> None:
        resolution = None
        if isinstance(p, dict) and isinstance(p.get("channels"), list) and isinstance(p.get("resolutions"), list):
            res = p["resolutions"]
            if p["channels"] and len(res) == len(p["channels"]) and res[0] in PREVIEW_SIZES:
                resolution = res[0]
        if resolution is None:
            self.send_json({"type": "response", "seq": seq, "result": {"error_code": -52402}})
            return
        if self.sender is not None and self.sender.is_alive():
            self.send_json({"type": "response", "seq": seq, "result": {"error_code": -52417}})
            return
        self.session_id = str(self.server.next_session_id())
        self.send_json({"type": "response", "seq": seq,
                        "result": {"error_code": 0, "session_id": self.session_id, "resolutions": [resolution]}})
        self.cam._count("media_preview_requests")
        self.stop_stream.clear()
        self.sender = threading.Thread(target=self._preview_stream, args=(resolution,), name="mock-media-preview",
                                       daemon=True)
        self.sender.start()

    def _preview_stream(self, resolution: str) -> None:
        """Loop the cached clip as a live stream, paced by its PTS, until stop / close / ack timeout."""
        try:
            template = ts_loop.template_for(media_assets.load_ts(*PREVIEW_SIZES[resolution]))
        except (FileNotFoundError, RuntimeError, ValueError):
            self.send_json({"type": "notification", "params": {"event_type": "stream_finish", "reason": "internal_error"}})
            return
        speed = max(0.01, float(self.cam.media_preview_speed))
        started = time.monotonic()
        try:
            for i, unit in enumerate(ts_loop.TsLoop(template), start=1):
                delay = started + unit.elapsed / speed - time.monotonic()
                if delay > 0 and self.stop_stream.wait(delay):
                    return
                if self.stop_stream.is_set() or not self._wait_window(i):
                    return
                body, extra = self._encrypt_part(unit.data)
                hdr = {"Content-Type": "video/mp2t", "X-If-Encrypt": "1" if self.encrypt else "0", **extra,
                       "X-Session-Id": self.session_id or "", "X-Data-Sequence": str(i),
                       "X-Data-PTS": str(unit.pts), "X-If-IFrame": "1" if unit.keyframe else "0",
                       "X-Stream-Type": "0"}
                self.send_part(hdr, body)
                self.cam._count("media_parts_sent")
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
        self.sessions: set[_Session] = set()
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
        session = _Session(self, sock)
        with self.lock:
            self.sessions.add(session)
        try:
            session.run()
        finally:
            with self.lock:
                self.sessions.discard(session)

    def drop_all(self) -> None:
        """Reset (TCP RST) every open :8800 connection; their senders stop at once."""
        with self.lock:
            sessions = list(self.sessions)
        for s in sessions:
            s.stop_stream.set()
            with s.ack_cond:
                s.ack_cond.notify_all()
            rst_close(s.sock)

    def start(self) -> None:
        self.tcp.start()

    def stop(self) -> None:
        self.tcp.stop()
