"""Mock TP-Link cloud relay node (TLS tunnel) for Phase 3 remote live-view tests [spec:remote.relay.*].

The relay node is a TLS server on ``127.0.0.1:<ephemeral>`` with a self-signed certificate for
``localhost`` (tests inject :meth:`MockRelay.client_context`, which trusts only that certificate).
:class:`~tests.fixtures.mock_cloud.MockTapoCloud` hands out slots from :meth:`MockRelay.issue_slot`
(``relayUrl`` = ``https://localhost:<port>/relayservice?...``, ``relayIp`` = ``127.0.0.1``).

Per tunnel the mock checks what the real relay checks (and what the proven real-world client
sends, tools/diag_relay.py): request line ``POST <relayPath>&retryTime=0 HTTP/1.1`` and the headers
in EXACT order (:data:`EXPECTED_HEADERS`, ``Cookie`` only when the slot had an ``elbCookie``), with
the X-token of an issued slot. Answers:

* missing / misordered / wrong header -> ``400 {"errorCode":3,"closeReason":"illegal_request"}``;
* slot requested with a ``cloudType`` other than :attr:`required_cloud_type` or ``mode="offline"``
  -> ``500 {"errorCode":-1007,"closeReason":"device offline"}`` (the real relay's answer);
* :attr:`redirects` > 0 -> ``304`` with a RequestUrlResult body (new token, same node), the client
  must reconnect with ``X-Redirect-Times`` + 1;
* ``mode="drop"`` -> connection closed without an answer;
* otherwise ``200`` and the mock plays the camera side: the first client part must be the preview
  request (no inner ``POST /stream``, no digest); it answers with a ``session_id`` and loops the
  plaintext MPEG-TS of the mock media assets (``X-If-Encrypt: 0``; ``mode="encrypted"`` sends
  ``1``) paced in real time (``speed`` multiplier). Heartbeats, acks and stop requests are recorded.

Phase 4 (additive): slots issued with ``stream_type`` 1 (SD playback, path ``type=sdvod``) or 3 (download,
``type=download``) expect a first ``get playback`` / ``get download`` request (``start_time`` string > 0)
instead of the preview; the answer carries a ``session_id``, then plaintext TS follows: download = the
cached clip in 6 KiB parts, playback = the PTS-continuous clip loop for ``end - start`` seconds (unpaced),
both ended by ``stream_status``/``finished`` (``playback_finishes=False``: playback keeps going past the
clip end). ``vod_refuse`` = verbs answered with error -52402; ``vod_requests`` records the requests.

Phase 5 (additive): ``playback_continuous = True`` makes a playback tunnel stream like a real camera
(:class:`~tests.fixtures.mock_tapo_server.playback_feed.PlaybackFeed`): the recordings in
``playback_recordings`` (``[(start, end)]`` epoch s; None = one recording spanning the request) from
``start_time`` to ``end_time``, paced at ``playback_speed`` (None = unpaced), at most
``playback_ack_window`` un-acked parts in flight, real PTS (``playback_pts``), ``do pause`` /
``do play "null"`` / ``do play {start_time}`` / ``do stop`` honoured, ``finished`` at the end.

The implementation is independent of ``tapo_desktop`` on purpose (own multipart parser).
"""
from __future__ import annotations

import datetime as _dt
import ipaddress
import json
import secrets
import socket
import ssl
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .mock_tapo_server import media_assets, playback_feed, ts_loop
from .mock_tapo_server._net import quiet_close

RELAY_HOST = "localhost"
CLIENT_DELIMITER = b"----client-stream-boundary--"
DEVICE_DELIMITER = b"----device-stream-boundary--"
CONTENT_LENGTH_SENTINEL = "9223372036854775807"
HEARTBEAT_BODY = b'{"type": "notification", "params": {"event_type": "heartbeat"}}'
#: Header names of the tunnel request in the order the app (and the proven client) sends them.
EXPECTED_HEADERS = ("User-Agent", "Keep-Relay", "Accept", "Content-Type", "Host", "Content-Length", "X-token",
                    "X-Client-Model", "X-Client-UUID", "X-Track-Id", "X-Redirect-Times", "X-Pull-Mode",
                    "Cookie", "X-Version", "X-Arrive-Latency")
PREVIEW_SIZES = {"HD": (1280, 720), "VGA": (640, 360)}

_cert_lock = threading.Lock()
_cert_files: tuple[str, str] | None = None


def _self_signed() -> tuple[str, str]:
    """(cert_path, key_path) of a process-wide self-signed EC certificate for localhost/127.0.0.1."""
    global _cert_files
    with _cert_lock:
        if _cert_files is not None:
            return _cert_files
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID

        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Mock TP-Link Relay")])
        now = _dt.datetime.now(_dt.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - _dt.timedelta(days=1)).not_valid_after(now + _dt.timedelta(days=30))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(RELAY_HOST),
                                                            x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                               critical=False)
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                .sign(key, hashes.SHA256()))
        folder = Path(tempfile.mkdtemp(prefix="mock_relay_"))
        cert_path, key_path = folder / "relay.pem", folder / "relay.key"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
        _cert_files = (str(cert_path), str(key_path))
        return _cert_files


class _Tunnel:
    """Server side of one accepted relay connection."""

    def __init__(self, relay: "MockRelay", sock: ssl.SSLSocket):
        self.relay = relay
        self.sock = sock
        self.buf = bytearray()
        self.send_lock = threading.Lock()
        self.io_lock = threading.Lock()
        self.serialize_io = False  # set for VOD tunnels (unpaced sender + reader on one TLS socket)
        self.session_id: str | None = None
        self.stop_media = threading.Event()
        self.sender: threading.Thread | None = None
        self.slot: dict | None = None
        self.feed: playback_feed.PlaybackFeed | None = None  # Phase 5 continuous playback
        self.ack_cond = threading.Condition()
        self.last_ack = 0

    # ---------------------------------------------------------------- I/O helpers
    def _recv(self) -> bool:
        while not self.relay.stopping.is_set():
            try:
                if self.serialize_io:  # VOD: never read while the sender thread writes (OpenSSL is not thread-safe)
                    with self.io_lock:
                        chunk = self.sock.recv(65536)
                else:
                    chunk = self.sock.recv(65536)
            except (socket.timeout, TimeoutError):
                if self.serialize_io:
                    time.sleep(0.002)  # let the sender take the lock
                continue
            except (OSError, ssl.SSLError):
                return False
            if not chunk:
                return False
            self.buf += chunk
            return True
        return False

    def send(self, data: bytes) -> None:
        with self.send_lock:
            if self.serialize_io:
                with self.io_lock:
                    self.sock.sendall(data)
            else:
                self.sock.sendall(data)

    def send_part(self, headers: dict[str, str], body: bytes) -> None:
        head = DEVICE_DELIMITER + b"\r\n"
        for k, v in headers.items():
            head += f"{k}: {v}\r\n".encode("latin-1")
        head += f"Content-Length: {len(body)}\r\n\r\n".encode("latin-1")
        self.send(head + body + b"\r\n")

    def send_json(self, obj: dict) -> None:
        hdr = {"Content-Type": "application/json"}
        if self.session_id:
            hdr["X-Session-Id"] = self.session_id
        self.send_part(hdr, json.dumps(obj).encode("utf-8"))

    def answer(self, status: str, body: dict | None = None, headers: list[str] | None = None) -> None:
        data = json.dumps(body).encode("utf-8") if body is not None else b""
        lines = [f"HTTP/1.1 {status}", *(headers or [])]
        if body is not None:
            lines += ["Content-Type: application/json", f"Content-Length: {len(data)}"]
        self.send(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + data)

    # ---------------------------------------------------------------- request head
    def read_head(self) -> tuple[str, list[tuple[str, str]]] | None:
        while b"\r\n\r\n" not in self.buf:
            if len(self.buf) > 65536 or not self._recv():
                return None
        end = self.buf.index(b"\r\n\r\n")
        head = bytes(self.buf[:end]).decode("latin-1")
        del self.buf[:end + 4]
        line, *rest = head.split("\r\n")
        headers = []
        for h in rest:
            name, _, value = h.partition(":")
            headers.append((name.strip(), value.strip()))
        return line, headers

    def check_head(self, line: str, headers: list[tuple[str, str]]) -> tuple[str, dict] | None:
        """None = valid; else (status line, JSON body) to answer."""
        illegal = ("400 Bad Request", {"errorCode": 3, "closeReason": "illegal_request"})
        parts = line.split(" ")
        if len(parts) != 3 or parts[0] != "POST" or parts[2] != "HTTP/1.1":
            return illegal
        target = urlsplit(parts[1])
        query = parse_qs(target.query)
        if target.path != "/relayservice" or query.get("retryTime") != ["0"] or not target.query.endswith("&retryTime=0"):
            return illegal
        values = dict(headers)
        token = values.get("X-token", "")
        with self.relay.lock:
            slot = self.relay.slots.get(token)
        expected = [h for h in EXPECTED_HEADERS if h != "Cookie" or (slot and slot.get("elbCookie"))]
        if [n for n, _ in headers] != expected:
            return illegal
        ua = values["User-Agent"]
        if not (ua.startswith("Client=TP-Link_Tapo_Android Android ") and ua.endswith("/1.3")):
            return illegal
        fixed = {"Keep-Relay": "3600", "Accept": "*/*", "Host": RELAY_HOST,
                 "Content-Type": "multipart/mixed;boundary=--client-stream-boundary--",
                 "Content-Length": CONTENT_LENGTH_SENTINEL, "X-Pull-Mode": "2", "X-Version": "2.0"}
        if any(values.get(k) != v for k, v in fixed.items()):
            return illegal
        if not values["X-Client-UUID"] or not values["X-Client-Model"] or not values["X-Arrive-Latency"].isdigit():
            return illegal
        if slot is None:
            return "403 Forbidden", {"errorCode": -1, "closeReason": "invalid token"}
        if (values["X-Track-Id"] != slot["trackId"] or values["X-Redirect-Times"] != str(slot["redirectTimes"])
                or query.get("deviceid") != [slot["deviceId"]]):
            return illegal
        if slot.get("elbCookie") and values.get("Cookie") != slot["elbCookie"]:
            return illegal
        self.slot = slot
        return None

    # ---------------------------------------------------------------- main
    def run(self) -> None:
        relay = self.relay
        got = self.read_head()
        if got is None:
            return
        line, headers = got
        entry = {"line": line, "headers": headers, "status": None, "time": time.monotonic()}
        with relay.lock:
            relay.tunnels.append(entry)
            mode = relay.mode
        if mode == "drop":
            entry["status"] = "dropped"
            return
        problem = self.check_head(line, headers)
        if problem is not None:
            entry["status"] = problem[0]
            self.answer(problem[0], problem[1])
            return
        slot = self.slot
        assert slot is not None
        if mode == "offline" or slot["cloudType"] != relay.required_cloud_type:
            entry["status"] = "500"
            self.answer("500 Internal Server Error", {"errorCode": -1007, "closeReason": "device offline"})
            return
        with relay.lock:
            redirect = relay.redirects > 0
            if redirect:
                relay.redirects -= 1
                new = relay._new_slot(slot["deviceId"], slot["resolution"], slot["cloudType"], slot["trackId"],
                                      redirect_times=slot["redirectTimes"] + 1,
                                      stream_type=slot.get("streamType", 0))
        if redirect:
            entry["status"] = "304"
            self.answer("304 Not Modified", new)
            return
        entry["status"] = "200"
        self.answer("200 OK", None, [f"X-Client-Id: {secrets.token_hex(8)}",
                                     "Content-Type: multipart/mixed;boundary=--device-stream-boundary--"])
        with relay.lock:
            relay.open_tunnels += 1
        try:
            self.device_loop()
        finally:
            self.stop_media.set()
            with relay.lock:
                relay.open_tunnels -= 1
            if self.sender is not None:
                self.sender.join(2)

    def device_loop(self) -> None:
        first = True
        while not self.relay.stopping.is_set():
            part = self.next_part()
            if part is None:
                return
            headers, body = part
            try:
                msg = json.loads(body)
            except (ValueError, UnicodeDecodeError):
                msg = None
            record = {"headers": headers, "body": msg, "raw": body, "time": time.monotonic()}
            with self.relay.lock:
                self.relay.client_parts.append(record)
            if not isinstance(msg, dict):
                continue
            params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
            if msg.get("type") == "notification":
                event = params.get("event_type")
                with self.relay.lock:
                    if event == "heartbeat":
                        self.relay.heartbeats.append((time.monotonic(), body))
                    elif event == "stream_sequence":
                        self.relay.acks.append(headers.get("X-Data-Received"))
                if event == "stream_sequence" and str(headers.get("X-Data-Received", "")).isdigit():
                    with self.ack_cond:
                        self.last_ack = max(self.last_ack, int(headers["X-Data-Received"]))
                        self.ack_cond.notify_all()
                continue
            if msg.get("type") != "request":
                continue
            stream_type = (self.slot or {}).get("streamType", 0)
            feed = self.feed
            if feed is not None and params.get("method") == "do" and ("pause" in params or "play" in params):
                play = params.get("play")
                if "pause" in params:
                    feed.pause()
                elif play == "null":
                    feed.resume()
                elif isinstance(play, dict) and str(play.get("start_time", "")).isdigit():
                    feed.seek(int(play["start_time"]))
                self.send_json({"type": "response", "seq": msg.get("seq"), "result": {"error_code": 0}})
                continue
            if first:
                first = False
                verb = {0: "preview", 1: "playback", 3: "download"}.get(stream_type)
                if params.get("method") != "get" or verb not in params:
                    self.send_json({"type": "response", "seq": msg.get("seq"), "result": {"error_code": -40106}})
                    continue
            if params.get("method") == "get" and stream_type == 0 and isinstance(params.get("preview"), dict):
                self.start_preview(msg.get("seq"), params["preview"])
            elif params.get("method") == "get" and stream_type in (1, 3):
                verb = "playback" if stream_type == 1 else "download"
                if isinstance(params.get(verb), dict):
                    self.start_vod(msg.get("seq"), verb, params[verb])
            elif params.get("method") == "do" and "stop" in params:
                with self.relay.lock:
                    self.relay.stops += 1
                self.stop_media.set()
                if feed is not None:
                    feed.wake()
                with self.ack_cond:
                    self.ack_cond.notify_all()

    def next_part(self) -> tuple[dict[str, str], bytes] | None:
        while True:
            # skip to a delimiter line
            while True:
                eol = self.buf.find(b"\r\n")
                if eol < 0:
                    if not self._recv():
                        return None
                    continue
                line = bytes(self.buf[:eol]).strip()
                del self.buf[:eol + 2]
                if line == CLIENT_DELIMITER:
                    break
            while b"\r\n\r\n" not in self.buf:
                if not self._recv():
                    return None
            end = self.buf.index(b"\r\n\r\n")
            headers = {}
            for h in bytes(self.buf[:end]).decode("latin-1").split("\r\n"):
                name, _, value = h.partition(":")
                headers[name.strip()] = value.strip()
            del self.buf[:end + 4]
            try:
                length = int(headers.get("Content-Length", ""))
            except ValueError:
                return None
            while len(self.buf) < length:
                if not self._recv():
                    return None
            body = bytes(self.buf[:length])
            del self.buf[:length]
            return headers, body

    def start_preview(self, seq, p: dict) -> None:
        res = p.get("resolutions") if isinstance(p.get("resolutions"), list) else []
        resolution = res[0] if res and res[0] in PREVIEW_SIZES else None
        if resolution is None or not isinstance(p.get("channels"), list):
            self.send_json({"type": "response", "seq": seq, "result": {"error_code": -52402}})
            return
        with self.relay.lock:
            self.relay.previews.append({"resolution": resolution, "channels": p.get("channels"),
                                        "slot_resolution": self.slot["resolution"] if self.slot else None})
            self.relay.session_counter += 1
            self.session_id = str(self.relay.session_counter)
        self.send_json({"type": "response", "seq": seq,
                        "result": {"error_code": 0, "session_id": self.session_id, "resolutions": [resolution]}})
        self.stop_media.clear()
        self.sender = threading.Thread(target=self.media_loop, args=(resolution,), name="mock-relay-media",
                                       daemon=True)
        self.sender.start()

    def start_vod(self, seq, verb: str, p: dict) -> None:
        """SD playback (streamType 1) / download (streamType 3) inside the tunnel [spec:remote.relay.playback_download.tunnel]."""
        relay = self.relay
        try:
            start = int(p["start_time"])
            end = int(p.get("end_time", start))
        except (KeyError, TypeError, ValueError):
            start, end = 0, -1
        with relay.lock:
            relay.vod_requests.append({"verb": verb, "params": dict(p), "time": time.monotonic()})
            refused = verb in relay.vod_refuse
        if not isinstance(p.get("start_time"), str) or start <= 0 or end < start or refused:
            self.send_json({"type": "response", "seq": seq, "result": {"error_code": -52402}})
            return
        with relay.lock:
            relay.session_counter += 1
            self.session_id = str(relay.session_counter)
        result = {"error_code": 0, "session_id": self.session_id}
        if verb == "playback":
            result["speed"] = p.get("scale", "1/1")
        self.send_json({"type": "response", "seq": seq, "result": result})
        self.stop_media.clear()
        with self.io_lock:
            self.sock.settimeout(0.02)  # short reads: the sender gets the I/O lock between them
            self.serialize_io = True
        target = self.download_loop if verb == "download" else self.playback_loop
        if verb == "playback" and relay.playback_continuous:
            recordings = relay.playback_recordings if relay.playback_recordings is not None else [(start, end)]
            self.feed = playback_feed.PlaybackFeed(recordings, start, end, stop=self.stop_media,
                                                   speed=relay.playback_speed, pts_mode=relay.playback_pts)
            with relay.lock:
                relay.playback_feeds.append(self.feed)
            target = self.continuous_loop
        self.sender = threading.Thread(target=target, args=(max(1, end - start),), name=f"mock-relay-{verb}",
                                       daemon=True)
        self.sender.start()

    def _send_media(self, i: int, data: bytes, pts: int, keyframe: bool) -> None:
        self.send_part({"Content-Type": "video/mp2t", "X-If-Encrypt": "0", "X-Session-Id": self.session_id or "",
                        "X-Data-Sequence": str(i), "X-Data-PTS": str(pts), "X-If-IFrame": "1" if keyframe else "0"},
                       data)
        with self.relay.lock:
            self.relay.media_parts_sent += 1

    def _finished(self) -> None:
        self.send_json({"type": "notification", "params": {"event_type": "stream_status", "status": "finished"}})

    def download_loop(self, _duration: int) -> None:
        """The cached clip in 188*32-byte parts (like the LAN :8800 download), then ``finished``."""
        try:
            ts = media_assets.load_ts()
            chunk = 188 * 32
            chunks = [ts[i:i + chunk] for i in range(0, len(ts), chunk)]
            total_pts = media_assets.TS_SECONDS * 90000
            for i, data in enumerate(chunks, start=1):
                if self.stop_media.is_set() or self.relay.stopping.is_set():
                    return
                self._send_media(i, data, int(total_pts * (i - 1) / len(chunks)), i == 1)
            self._finished()
        except (OSError, ssl.SSLError):
            pass

    def playback_loop(self, duration: int) -> None:
        """Continuous TS (PTS-continuous loop of the cached clip) for ``duration`` s of stream time, unpaced;
        then ``finished`` - or, with ``playback_finishes=False``, it keeps streaming (the client must stop
        at the clip end) up to a safety cap."""
        try:
            template = ts_loop.template_for(media_assets.load_ts())
            finishes = self.relay.playback_finishes
            limit = duration if finishes else duration * 20 + 30
            for i, unit in enumerate(ts_loop.TsLoop(template), start=1):
                if self.stop_media.is_set() or self.relay.stopping.is_set():
                    return
                if unit.elapsed >= limit:
                    break
                self._send_media(i, unit.data, unit.pts, unit.keyframe)
            if finishes and not self.stop_media.is_set():
                self._finished()
        except (OSError, ssl.SSLError):
            pass

    def continuous_loop(self, _duration: int) -> None:
        """Phase 5: the feed's units with an ack window (no blocking TLS writes), then ``finished``."""
        feed = self.feed
        assert feed is not None
        window = self.relay.playback_ack_window
        try:
            for i, unit in enumerate(feed.units(), start=1):
                with self.ack_cond:
                    while window and i - self.last_ack > window:
                        if self.stop_media.is_set() or self.relay.stopping.is_set():
                            return
                        self.ack_cond.wait(0.05)
                if self.stop_media.is_set() or self.relay.stopping.is_set():
                    return
                self._send_media_blocking(i, unit)
            if feed.finished and not self.stop_media.is_set():
                self._finished()
        except (OSError, ssl.SSLError, FileNotFoundError, RuntimeError):
            pass

    def _send_media_blocking(self, i: int, unit) -> None:
        """One media part with a generous write timeout: a paced client may leave data unread for a while
        and a timed-out partial TLS write would corrupt the stream (the reader waits on the I/O lock)."""
        headers = {"Content-Type": "video/mp2t", "X-If-Encrypt": "0", "X-Session-Id": self.session_id or "",
                   "X-Data-Sequence": str(i), "X-Data-PTS": str(unit.pts),
                   "X-If-IFrame": "1" if unit.keyframe else "0"}
        head = DEVICE_DELIMITER + b"\r\n"
        for k, v in headers.items():
            head += f"{k}: {v}\r\n".encode("latin-1")
        head += f"Content-Length: {len(unit.data)}\r\n\r\n".encode("latin-1")
        with self.send_lock, self.io_lock:
            self.sock.settimeout(10.0)
            try:
                self.sock.sendall(head + unit.data + b"\r\n")
            finally:
                self.sock.settimeout(0.02)
        with self.relay.lock:
            self.relay.media_parts_sent += 1

    def media_loop(self, resolution: str) -> None:
        template = ts_loop.template_for(media_assets.load_ts(*PREVIEW_SIZES[resolution]))
        speed = max(0.01, float(self.relay.speed))
        encrypted = self.relay.mode == "encrypted"
        started = time.monotonic()
        try:
            for i, unit in enumerate(ts_loop.TsLoop(template), start=1):
                delay = started + unit.elapsed / speed - time.monotonic()
                if delay > 0 and self.stop_media.wait(delay):
                    return
                if self.stop_media.is_set() or self.relay.stopping.is_set():
                    return
                self.send_part({"Content-Type": "video/mp2t", "X-If-Encrypt": "1" if encrypted else "0",
                                "X-Session-Id": self.session_id or "", "X-Data-Sequence": str(i),
                                "X-Data-PTS": str(unit.pts), "X-If-IFrame": "1" if unit.keyframe else "0"},
                               unit.data)
                with self.relay.lock:
                    self.relay.media_parts_sent += 1
        except (OSError, ssl.SSLError):
            pass


class MockRelay:
    """TLS relay node mock; see the module docstring. Mutable while running: ``mode``
    (``"ok"`` | ``"offline"`` | ``"drop"`` | ``"encrypted"``), ``redirects``, ``required_cloud_type``,
    ``elb_cookie``, ``speed``. Inspection: ``tunnels`` (request line + ordered headers + answer),
    ``client_parts``, ``heartbeats`` ([(monotonic time, raw body)]), ``acks``, ``stops``, ``previews``,
    ``open_tunnels``, ``media_parts_sent``."""

    def __init__(self, *, speed: float = 1.0):
        self.mode = "ok"
        self.redirects = 0
        self.required_cloud_type = 2
        self.elb_cookie = ""
        self.speed = speed
        self.lock = threading.Lock()
        self.slots: dict[str, dict] = {}
        self.tunnels: list[dict] = []
        self.client_parts: list[dict] = []
        self.heartbeats: list[tuple[float, bytes]] = []
        self.acks: list[str | None] = []
        self.previews: list[dict] = []
        self.stops = 0
        self.open_tunnels = 0
        self.media_parts_sent = 0
        self.session_counter = 0
        self.vod_requests: list[dict] = []  # Phase 4: in-tunnel "get playback" / "get download" params
        self.vod_refuse: set[str] = set()  # verbs ("playback" / "download") answered with error -52402
        self.playback_finishes = True  # False: playback streams past the clip end (client must stop)
        # Phase 5 (additive): continuous, paced playback with in-session controls (see the module docstring)
        self.playback_continuous = False
        self.playback_recordings: list[tuple[float, float]] | None = None
        self.playback_speed: float | None = 1.0
        self.playback_pts = "reset"
        self.playback_ack_window = 50
        self.playback_feeds: list = []
        self.stopping = threading.Event()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._conns: list[socket.socket] = []
        self._workers: list[threading.Thread] = []
        cert, key = _self_signed()
        self._server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._server_ctx.load_cert_chain(cert, key)
        self._cert = cert

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> "MockRelay":
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(32)
        sock.settimeout(0.1)
        self._sock = sock
        self._thread = threading.Thread(target=self._accept_loop, name="mock-relay-accept", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.stopping.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(3)
        self.drop_all()
        for t in list(self._workers):
            t.join(3)

    def __enter__(self) -> "MockRelay":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def port(self) -> int:
        assert self._sock is not None, "MockRelay not started"
        return self._sock.getsockname()[1]

    def client_context(self) -> ssl.SSLContext:
        """Client TLS context that trusts only the mock relay certificate (hostname checked)."""
        ctx = ssl.create_default_context(cafile=self._cert)
        return ctx

    def drop_all(self) -> None:
        """Close every open tunnel (simulates a relay / network drop)."""
        with self.lock:
            conns, self._conns = list(self._conns), []
        for c in conns:
            quiet_close(c)

    # ---------------------------------------------------------------- slots (used by MockTapoCloud)
    def _new_slot(self, device_id: str, resolution: str, cloud_type: int, track_id: str, *,
                  redirect_times: int = 0, stream_type: int = 0) -> dict:
        token = secrets.token_hex(16)
        if stream_type == 1:
            path = f"/relayservice?deviceid={device_id}&type=sdvod&playerid={secrets.token_hex(16)}"
        elif stream_type == 3:
            path = f"/relayservice?deviceid={device_id}&type=download&bidirectionid={secrets.token_hex(16)}"
        else:
            path = f"/relayservice?deviceid={device_id}&type=video&resolution={resolution}"
        result = {"relayUrl": f"https://{RELAY_HOST}:{self.port}{path}", "relayIp": "127.0.0.1",
                  "relayToken": token}
        if self.elb_cookie:
            result["elbCookie"] = self.elb_cookie
        self.slots[token] = {"deviceId": device_id, "resolution": resolution, "cloudType": cloud_type,
                             "trackId": track_id, "redirectTimes": redirect_times,
                             "elbCookie": self.elb_cookie, "streamType": stream_type}
        return result

    def issue_slot(self, device_id: str, resolution: str, cloud_type: int, track_id: str, *,
                   stream_type: int = 0) -> dict:
        """RequestUrlResult for a new slot (called by the mock cloud's /v2/relay/request)."""
        with self.lock:
            return self._new_slot(device_id, resolution, cloud_type, track_id, stream_type=stream_type)

    # ---------------------------------------------------------------- server
    def _accept_loop(self) -> None:
        assert self._sock is not None
        while not self.stopping.is_set():
            try:
                conn, _addr = self._sock.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                return
            t = threading.Thread(target=self._serve, args=(conn,), name="mock-relay-conn", daemon=True)
            self._workers = [w for w in self._workers if w.is_alive()] + [t]
            t.start()

    def _serve(self, raw: socket.socket) -> None:
        raw.settimeout(5)
        try:
            tls = self._server_ctx.wrap_socket(raw, server_side=True)
        except (OSError, ssl.SSLError):
            quiet_close(raw)
            return
        tls.settimeout(0.5)
        with self.lock:
            self._conns.append(tls)
        try:
            _Tunnel(self, tls).run()
        except (OSError, ssl.SSLError):
            pass
        finally:
            with self.lock:
                if tls in self._conns:
                    self._conns.remove(tls)
            quiet_close(tls)
