"""RTSP :554 server of the mock camera ([spec:stream.rtsp.*], [spec:rtsp.endpoints]).

RTP/AVP/TCP interleaved only (UDP SETUP -> 461). H.264 (PT 96, RFC 6184 single NAL + FU-A,
90 kHz) + PCMA (PT 8, 8 kHz, 20 ms packets). Real-time pacing at 15 fps from a looping clip with
continuous timestamps. Digest (RFC 2069 / qop=auth) or Basic auth with the camera account.
``max_rtsp_clients`` is enforced at DESCRIBE and SETUP (453 Not Enough Bandwidth).
"""
from __future__ import annotations

import base64
import hashlib
import random
import re
import secrets
import socket
import struct
import threading
import time
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from . import media_assets
from ._net import TcpServer, rst_close

if TYPE_CHECKING:
    from .camera import MockTapoCamera

REALM = "TP-LINK IP-CAM"
MTU = 1400
_STATUS = {200: "OK", 401: "Unauthorized", 404: "Not Found", 405: "Method Not Allowed", 400: "Bad Request",
           453: "Not Enough Bandwidth", 454: "Session Not Found", 455: "Method Not Valid in This State",
           459: "Aggregate Operation Not Allowed", 461: "Unsupported Transport", 500: "Internal Server Error",
           501: "Not Implemented"}
_PATHS = {"stream1": (1280, 720), "stream2": (640, 360), "stream6": (1280, 720), "stream7": (640, 360)}
_AUTH_PARAM_RE = re.compile(r'(\w+)\s*=\s*(?:"([^"]*)"|([^,\s]+))')


def parse_auth_params(value: str) -> dict[str, str]:
    return {k.lower(): (q or u) for k, q, u in _AUTH_PARAM_RE.findall(value)}


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


class _Conn:
    def __init__(self, server: "RtspServer", sock: socket.socket):
        self.server = server
        self.cam = server.cam
        self.sock = sock
        self.wlock = threading.Lock()
        self.session_id: str | None = None
        self.tracks: dict[str, int] = {}     # "video"/"audio" -> interleaved RTP channel
        self.path: str | None = None
        self.holds_slot = False
        self.playing = False
        self.sender: threading.Thread | None = None
        self.stop_sender = threading.Event()
        self.alive = True
        self.vseq = random.randint(0, 0xFFFF)
        self.aseq = random.randint(0, 0xFFFF)
        self.vts0 = random.randint(0, 0x7FFFFFFF)
        self.ats0 = random.randint(0, 0x7FFFFFFF)
        self.vssrc = random.getrandbits(32)
        self.assrc = random.getrandbits(32)
        self.vframe = 0
        self.apkt = 0

    # -- io ---------------------------------------------------------------------------------
    def send_raw(self, data: bytes, deadline_s: float = 5.0) -> None:
        """sendall that tolerates the short socket timeout (slow readers) without corrupting framing."""
        view = memoryview(data)
        deadline = time.monotonic() + deadline_s
        with self.wlock:
            while view:
                try:
                    n = self.sock.send(view)
                except TimeoutError:
                    if time.monotonic() > deadline or not self.alive:
                        raise
                    continue
                view = view[n:]

    def reply(self, status: int, cseq: str | None, headers: dict[str, str] | None = None, body: bytes = b"",
              extra_lines: list[str] | None = None) -> None:
        lines = [f"RTSP/1.0 {status} {_STATUS.get(status, 'Error')}"]
        if cseq is not None:
            lines.append(f"CSeq: {cseq}")
        lines.append("Server: TP-LINK RTSP Server (mock)")
        for k, v in (headers or {}).items():
            lines.append(f"{k}: {v}")
        lines.extend(extra_lines or [])
        if body:
            lines.append(f"Content-Length: {len(body)}")
        self.send_raw(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + body)

    # -- main loop --------------------------------------------------------------------------
    def run(self) -> None:
        buf = b""
        stopping = self.server.tcp.stopping
        while not stopping.is_set():
            try:
                chunk = self.sock.recv(65536)
            except TimeoutError:
                continue
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while buf:
                if buf[:1] == b"$":  # interleaved RTCP from client
                    if len(buf) < 4:
                        break
                    n = struct.unpack(">H", buf[2:4])[0]
                    if len(buf) < 4 + n:
                        break
                    buf = buf[4 + n:]
                    continue
                end = buf.find(b"\r\n\r\n")
                if end < 0:
                    if len(buf) > 65536:
                        return
                    break
                head = buf[:end].decode("utf-8", "replace")
                lines = head.split("\r\n")
                headers: dict[str, str] = {}
                for ln in lines[1:]:
                    if ":" in ln:
                        k, v = ln.split(":", 1)
                        headers[k.strip().lower()] = v.strip()
                clen = int(headers.get("content-length", "0") or 0)
                if len(buf) < end + 4 + clen:
                    break
                buf = buf[end + 4 + clen:]
                try:
                    method, url, _ver = lines[0].split(" ", 2)
                except ValueError:
                    self.reply(400, headers.get("cseq"))
                    continue
                self.handle(method.upper(), url, headers)
        self.close()

    def close(self) -> None:
        self.alive = False
        self._stop_play()
        self._release_slot()

    # -- helpers ----------------------------------------------------------------------------
    def _stop_play(self) -> None:
        self.stop_sender.set()
        if self.sender is not None and self.sender is not threading.current_thread():
            self.sender.join(2)
        self.sender = None
        if self.playing:
            self.playing = False
            self.server.change_playing(-1)

    def _release_slot(self) -> None:
        if self.holds_slot:
            self.holds_slot = False
            self.server.release_slot()

    def _auth_ok(self, method: str, headers: dict[str, str]) -> bool:
        cam = self.cam
        auth = headers.get("authorization", "")
        if auth.lower().startswith("basic "):
            try:
                user, _, pw = base64.b64decode(auth[6:].strip()).decode("utf-8").partition(":")
            except (ValueError, UnicodeDecodeError):
                return False
            return user == cam.camera_user and pw == cam.camera_password
        if auth.lower().startswith("digest "):
            p = parse_auth_params(auth[7:])
            if p.get("username") != cam.camera_user or p.get("realm") != REALM:
                return False
            if p.get("nonce") not in self.server.nonces:
                return False
            ha1 = _md5(f"{cam.camera_user}:{REALM}:{cam.camera_password}")
            ha2 = _md5(f"{method}:{p.get('uri', '')}")
            if p.get("qop"):
                expected = _md5(f"{ha1}:{p['nonce']}:{p.get('nc', '')}:{p.get('cnonce', '')}:{p['qop']}:{ha2}")
            else:
                expected = _md5(f"{ha1}:{p['nonce']}:{ha2}")
            return secrets.compare_digest(expected, p.get("response", "").lower())
        return False

    def _challenge(self, cseq: str | None) -> None:
        nonce = secrets.token_hex(16)
        self.server.nonces.add(nonce)
        self.reply(401, cseq, extra_lines=[f'WWW-Authenticate: Digest realm="{REALM}", nonce="{nonce}"',
                                           f'WWW-Authenticate: Basic realm="{REALM}"'])

    def _resolve(self, url: str) -> tuple[str | None, str | None]:
        """-> (stream name, track name) from rtsp://host:port/streamN[/trackX]."""
        path = urlsplit(url).path if "://" in url else url
        parts = [p for p in path.split("/") if p]
        if not parts:
            return None, None
        stream = parts[0]
        allowed = {"stream1", "stream2"} | ({"stream6", "stream7"} if self.cam.dual_lens else set())
        if stream not in allowed:
            return None, None
        track = parts[1] if len(parts) > 1 else None
        return stream, track

    def _base_url(self, stream: str) -> str:
        return f"rtsp://{self.cam.host}:{self.server.port}/{stream}/"

    # -- methods ----------------------------------------------------------------------------
    def handle(self, method: str, url: str, h: dict[str, str]) -> None:
        cseq = h.get("cseq")
        if method == "OPTIONS":
            self.reply(200, cseq, {"Public": "OPTIONS, DESCRIBE, SETUP, PLAY, PAUSE, TEARDOWN, GET_PARAMETER, SET_PARAMETER"})
            return
        if method not in ("DESCRIBE", "SETUP", "PLAY", "PAUSE", "TEARDOWN", "GET_PARAMETER", "SET_PARAMETER"):
            self.reply(501, cseq)
            return
        if not self._auth_ok(method, h):
            self._challenge(cseq)
            return
        sess_hdr = {"Session": f"{self.session_id};timeout=60"} if self.session_id else {}
        if method == "DESCRIBE":
            stream, _ = self._resolve(url)
            if stream is None:
                self.reply(404, cseq)
                return
            if self.cam._take_rtsp_busy() or (not self.holds_slot and self.server.slots_full()):
                self.server.count_reject()
                self.reply(453, cseq)
                return
            assets = self.server.assets(stream)
            if assets is None:
                self.reply(500, cseq)
                return
            sdp = self._sdp(stream, assets[0]).encode("utf-8")
            self.reply(200, cseq, {"Content-Type": "application/sdp", "Content-Base": self._base_url(stream)}, sdp)
            return
        if method == "SETUP":
            self._setup(url, h, cseq)
            return
        if self.session_id is None or h.get("session", "").split(";")[0].strip() != self.session_id:
            if method in ("GET_PARAMETER", "SET_PARAMETER") and self.session_id is None:
                self.reply(200, cseq)
                return
            self.reply(454, cseq)
            return
        if method == "PLAY":
            assets = self.server.assets(self.path or "stream1")
            ts_v = self.vts0 + int(self.vframe * 90000 / media_assets.FPS)
            ts_a = self.ats0 + self.apkt * 160
            base = self._base_url(self.path or "stream1")
            info = []
            if "video" in self.tracks:
                info.append(f"url={base}track1;seq={self.vseq};rtptime={ts_v}")
            if "audio" in self.tracks:
                info.append(f"url={base}track2;seq={self.aseq};rtptime={ts_a}")
            hdr = {**sess_hdr, "Range": "npt=0.000-"}
            if info and assets is not None:
                hdr["RTP-Info"] = ",".join(info)
            if assets is not None and not self.playing:
                self.playing = True
                self.server.change_playing(+1)
                start = True
            else:
                start = False
            self.reply(200, cseq, hdr)       # reply first, then the first RTP packet
            if start:
                self._start_play(assets)
            return
        if method == "PAUSE":
            self._stop_play()
            self.reply(200, cseq, sess_hdr)
            return
        if method == "TEARDOWN":
            self._stop_play()
            self._release_slot()
            self.reply(200, cseq, sess_hdr)
            self.session_id = None
            self.tracks.clear()
            return
        self.reply(200, cseq, sess_hdr)  # GET_PARAMETER / SET_PARAMETER keep-alive

    def _sdp(self, stream: str, video) -> str:
        host = self.cam.host
        return "\r\n".join([
            "v=0", f"o=- {random.randint(10**7, 10**8)} 1 IN IP4 {host}",
            's=Session streamed by "TP-LINK RTSP Server"', "c=IN IP4 0.0.0.0", "t=0 0",
            "a=tool:mock_tapo_server", "a=range:npt=now-", "a=control:*",
            "m=video 0 RTP/AVP 96", "a=rtpmap:96 H264/90000",
            f"a=fmtp:96 packetization-mode=1;profile-level-id={video.profile_level_id};"
            f"sprop-parameter-sets={video.sprop_parameter_sets}",
            f"a=framerate:{video.fps}", "a=control:track1",
            "m=audio 0 RTP/AVP 8", "a=rtpmap:8 PCMA/8000", "a=ptime:20", "a=control:track2", ""])

    def _setup(self, url: str, h: dict[str, str], cseq: str | None) -> None:
        stream, track = self._resolve(url)
        if stream is None or track not in ("track1", "track2"):
            self.reply(404, cseq)
            return
        if self.path is not None and self.session_id is not None and stream != self.path:
            self.reply(459, cseq)
            return
        transport = h.get("transport", "")
        if "RTP/AVP/TCP" not in transport.upper():
            self.reply(461, cseq)
            return
        if not self.holds_slot:
            if self.cam._take_rtsp_busy() or not self.server.try_acquire_slot():
                self.server.count_reject()
                self.reply(453, cseq)
                return
            self.holds_slot = True
        m = re.search(r"interleaved=(\d+)(?:-(\d+))?", transport)
        kind = "video" if track == "track1" else "audio"
        if m:
            ch = int(m.group(1))
        else:
            ch = 0 if kind == "video" else 2
        self.tracks[kind] = ch
        self.path = stream
        if self.session_id is None:
            self.session_id = secrets.token_hex(6).upper()
        ssrc = self.vssrc if kind == "video" else self.assrc
        self.reply(200, cseq, {"Transport": f"RTP/AVP/TCP;unicast;interleaved={ch}-{ch + 1};ssrc={ssrc:08X};mode=\"play\"",
                               "Session": f"{self.session_id};timeout=60"})

    def _start_play(self, assets) -> None:
        self.stop_sender.clear()
        self.sender = threading.Thread(target=self._send_loop, args=(assets,), name="mock-rtsp-rtp", daemon=True)
        self.sender.start()

    # -- RTP --------------------------------------------------------------------------------
    def _rtp(self, channel: int, pt: int, marker: bool, seq: int, ts: int, ssrc: int, payload: bytes) -> bytes:
        hdr = struct.pack(">BBHII", 0x80, (0x80 if marker else 0) | pt, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc)
        pkt = hdr + payload
        return b"$" + bytes([channel]) + struct.pack(">H", len(pkt)) + pkt

    def _video_packets(self, au: tuple[bytes, ...], ts: int, ch: int) -> bytes:
        out = []
        for ni, nal in enumerate(au):
            last_nal = ni == len(au) - 1
            if len(nal) <= MTU:
                out.append(self._rtp(ch, 96, last_nal, self.vseq, ts, self.vssrc, nal))
                self.vseq = (self.vseq + 1) & 0xFFFF
                continue
            hdr = nal[0]
            ind = (hdr & 0xE0) | 28
            body = nal[1:]
            chunks = [body[i:i + MTU - 2] for i in range(0, len(body), MTU - 2)]
            for ci, c in enumerate(chunks):
                fu = hdr & 0x1F
                if ci == 0:
                    fu |= 0x80
                if ci == len(chunks) - 1:
                    fu |= 0x40
                out.append(self._rtp(ch, 96, last_nal and ci == len(chunks) - 1, self.vseq, ts, self.vssrc,
                                     bytes([ind, fu]) + c))
                self.vseq = (self.vseq + 1) & 0xFFFF
        return b"".join(out)

    def _audio_packet(self, pcma: bytes, ch: int) -> bytes:
        n = len(pcma) // 160
        off = (self.apkt % n) * 160
        payload = pcma[off:off + 160]
        ts = self.ats0 + self.apkt * 160
        if self.cam._rtsp_corrupt_audio:
            # garbled + truncated payloads (valid RTP header); every 7th packet is dropped (seq gap)
            if self.apkt % 7 == 3:
                self.aseq = (self.aseq + 1) & 0xFFFF
                return b""
            payload = secrets.token_bytes(random.randint(1, 159))
        pkt = self._rtp(ch, 8, self.apkt == 0, self.aseq, ts, self.assrc, payload)
        self.aseq = (self.aseq + 1) & 0xFFFF
        return pkt

    def _send_loop(self, assets) -> None:
        video, pcma = assets
        aus = video.access_units
        fps = video.fps
        vch = self.tracks.get("video")
        ach = self.tracks.get("audio")
        t0 = time.monotonic() - self.vframe / fps
        a0 = t0
        try:
            while not self.stop_sender.is_set() and self.alive:
                now = time.monotonic()
                out = []
                while vch is not None and t0 + self.vframe / fps <= now:
                    au = aus[self.vframe % len(aus)]
                    ts = self.vts0 + int(self.vframe * 90000 / fps)
                    out.append(self._video_packets(au, ts, vch))
                    self.vframe += 1
                while ach is not None and a0 + self.apkt * 0.02 <= now:
                    out.append(self._audio_packet(pcma, ach))
                    self.apkt += 1
                if out:
                    self.send_raw(b"".join(out))
                nxt = min(t0 + self.vframe / fps if vch is not None else now + 0.02,
                          a0 + self.apkt * 0.02 if ach is not None else now + 0.02)
                self.stop_sender.wait(max(0.001, min(0.02, nxt - time.monotonic())))
        except OSError:
            self.alive = False


class RtspServer:
    def __init__(self, cam: "MockTapoCamera", host: str, port: int):
        self.cam = cam
        self.tcp = TcpServer(host, port, self._handle, name="mock-rtsp")
        self.port = self.tcp.port
        self.lock = threading.Lock()
        self.conns: set[_Conn] = set()
        self.nonces: set[str] = set()
        self.slots = 0
        self.playing = 0
        self.reject_count = 0

    def assets(self, stream: str):
        w, h = _PATHS[stream]
        try:
            return media_assets.load_video(w, h), media_assets.load_pcma()
        except (FileNotFoundError, RuntimeError):
            return None

    def slots_full(self) -> bool:
        with self.lock:
            return self.slots >= self.cam.max_rtsp_clients

    def try_acquire_slot(self) -> bool:
        with self.lock:
            if self.slots >= self.cam.max_rtsp_clients:
                return False
            self.slots += 1
            return True

    def release_slot(self) -> None:
        with self.lock:
            self.slots = max(0, self.slots - 1)

    def change_playing(self, d: int) -> None:
        with self.lock:
            self.playing = max(0, self.playing + d)

    def count_reject(self) -> None:
        with self.lock:
            self.reject_count += 1

    def _handle(self, sock: socket.socket, addr) -> None:
        conn = _Conn(self, sock)
        with self.lock:
            self.conns.add(conn)
        try:
            conn.run()
        finally:
            conn.close()
            with self.lock:
                self.conns.discard(conn)

    def drop_all(self) -> None:
        with self.lock:
            conns = list(self.conns)
        for c in conns:
            c.alive = False
            c.stop_sender.set()
            rst_close(c.sock)

    def start(self) -> None:
        self.tcp.start()

    def stop(self) -> None:
        self.drop_all()
        self.tcp.stop()
