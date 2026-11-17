"""Minimal raw protocol clients used only by the fixture self-check (no ``tapo_desktop`` imports).

They are written from the spec with stdlib + ``cryptography`` so the self-check proves the mock
independently of the product code.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import secrets
import socket
import ssl
import struct
import time
from datetime import datetime, timezone

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def sha256u(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest().upper()


def md5u(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest().upper()


def cj(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def aes_enc(key: bytes, iv: bytes, data: bytes) -> bytes:
    p = padding.PKCS7(128).padder()
    e = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return e.update(p.update(data) + p.finalize()) + e.finalize()


def aes_dec(key: bytes, iv: bytes, data: bytes) -> bytes:
    d = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    u = padding.PKCS7(128).unpadder()
    return u.update(d.update(data) + d.finalize()) + u.finalize()


# ---------------------------------------------------------------------------------------------
# HTTPS control
# ---------------------------------------------------------------------------------------------
class HttpsRaw:
    def __init__(self, host: str, port: int, timeout: float = 5.0):
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE
        self.conn = http.client.HTTPSConnection(host, port, context=self.ctx, timeout=timeout)

    def post(self, path: str, body: str, headers: dict | None = None) -> tuple[int, dict | None]:
        self.conn.request("POST", path, body=body.encode(), headers={"Content-Type": "application/json", **(headers or {})})
        r = self.conn.getresponse()
        raw = r.read()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, None

    def peer_fingerprint(self) -> str:
        if self.conn.sock is None:
            self.conn.connect()
        return hashlib.sha256(self.conn.sock.getpeercert(binary_form=True)).hexdigest()

    def close(self) -> None:
        self.conn.close()


class SecureClient(HttpsRaw):
    def __init__(self, host: str, port: int, password: str, cnonce: str | None = None):
        super().__init__(host, port)
        self.password = password
        self.cnonce = cnonce or secrets.token_hex(8).upper()
        self.stok = ""
        self.seq = 0
        self.lsk = self.ivb = b""
        self.nonce = ""

    def step1(self) -> dict:
        _, r = self.post("/", cj({"method": "login", "params": {"cnonce": self.cnonce, "encrypt_type": "3",
                                                                "username": "admin"}}))
        return r

    def step2(self, nonce: str, password: str | None = None) -> dict:
        pw = password if password is not None else self.password
        dp = sha256u(sha256u(pw) + self.cnonce + nonce) + self.cnonce + nonce
        _, r = self.post("/", cj({"method": "login", "params": {"cnonce": self.cnonce, "encrypt_type": "3",
                                                                "username": "admin", "digest_passwd": dp}}))
        return r

    def login(self) -> dict:
        r1 = self.step1()
        assert r1["error_code"] == -40413, r1
        self.nonce = r1["result"]["data"]["nonce"]
        r2 = self.step2(self.nonce)
        assert r2["error_code"] == 0, r2
        self.stok = r2["result"]["stok"]
        self.seq = int(r2["result"]["start_seq"])
        fh = sha256u(self.cnonce + sha256u(self.password) + self.nonce)
        self.lsk = hashlib.sha256(("lsk" + self.cnonce + self.nonce + fh).encode()).digest()[:16]
        self.ivb = hashlib.sha256(("ivb" + self.cnonce + self.nonce + fh).encode()).digest()[:16]
        return r2

    def outer_body(self, inner: dict) -> str:
        ct = base64.b64encode(aes_enc(self.lsk, self.ivb, cj(inner).encode())).decode()
        return cj({"method": "securePassthrough", "params": {"request": ct}})

    def tag(self, body: str, seq: int) -> str:
        return sha256u(sha256u(sha256u(self.password) + self.cnonce) + body + str(seq))

    def send(self, inner: dict, *, seq: int | None = None, tag: str | None = None, body: str | None = None) -> dict:
        """Returns the decrypted inner reply, or the outer reply if it carries a non-zero error_code."""
        use = self.seq if seq is None else seq
        if seq is None:
            self.seq += 1
        b = body if body is not None else self.outer_body(inner)
        status, r = self.post(f"/stok={self.stok}/ds", b, {"seq": str(use), "tapo_tag": tag or self.tag(b, use)})
        if status != 200 or r is None or r.get("error_code") != 0:
            return {"_status": status, **(r or {})}
        return json.loads(aes_dec(self.lsk, self.ivb, base64.b64decode(r["result"]["response"])).decode())

    def multi(self, *reqs: dict) -> list[dict]:
        r = self.send({"method": "multipleRequest", "params": {"requests": list(reqs)}})
        assert r.get("error_code") == 0, r
        return r["result"]["responses"]


class LegacyClient(HttpsRaw):
    def __init__(self, host: str, port: int, password: str):
        super().__init__(host, port)
        self.password = password
        self.stok = ""

    def login(self, password: str | None = None) -> dict:
        pw = self.password if password is None else password
        _, r = self.post("/", cj({"method": "login", "params": {"hashed": True, "password": md5u(pw),
                                                                "username": "admin"}}))
        if r and r.get("error_code") == 0:
            self.stok = r["result"]["stok"]
        return r

    def multi(self, *reqs: dict) -> dict:
        _, r = self.post(f"/stok={self.stok}/ds", cj({"method": "multipleRequest", "params": {"requests": list(reqs)}}))
        return r


# ---------------------------------------------------------------------------------------------
# RTSP
# ---------------------------------------------------------------------------------------------
class RtspRaw:
    def __init__(self, host: str, port: int, user: str = "", password: str = "", timeout: float = 5.0):
        self.host, self.port, self.user, self.password = host, port, user, password
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.buf = b""
        self.cseq = 0
        self.auth: tuple[str, dict] | None = None   # ("digest", params) | ("basic", {})
        self.session: str | None = None
        self.rtp: list[tuple[int, bytes]] = []

    def url(self, path: str) -> str:
        return f"rtsp://{self.host}:{self.port}/{path.lstrip('/')}"

    def _recv(self) -> None:
        chunk = self.sock.recv(65536)
        if not chunk:
            raise ConnectionError("closed")
        self.buf += chunk

    def read_response(self) -> tuple[int, dict[str, list[str]], bytes]:
        while True:
            while self.buf[:1] == b"$":
                while len(self.buf) < 4:
                    self._recv()
                n = struct.unpack(">H", self.buf[2:4])[0]
                while len(self.buf) < 4 + n:
                    self._recv()
                self.rtp.append((self.buf[1], self.buf[4:4 + n]))
                self.buf = self.buf[4 + n:]
            if self.buf.startswith(b"RTSP/") and b"\r\n\r\n" in self.buf:
                break
            self._recv()
        end = self.buf.index(b"\r\n\r\n")
        lines = self.buf[:end].decode().split("\r\n")
        status = int(lines[0].split(" ")[1])
        headers: dict[str, list[str]] = {}
        for ln in lines[1:]:
            k, _, v = ln.partition(":")
            headers.setdefault(k.strip().lower(), []).append(v.strip())
        n = int(headers.get("content-length", ["0"])[0])
        while len(self.buf) < end + 4 + n:
            self._recv()
        body, self.buf = self.buf[end + 4:end + 4 + n], self.buf[end + 4 + n:]
        return status, headers, body

    def _auth_header(self, method: str, uri: str) -> str | None:
        if not self.auth:
            return None
        kind, p = self.auth
        if kind == "basic":
            return "Basic " + base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        ha1 = hashlib.md5(f"{self.user}:{p['realm']}:{self.password}".encode()).hexdigest()
        ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
        resp = hashlib.md5(f"{ha1}:{p['nonce']}:{ha2}".encode()).hexdigest()
        return (f'Digest username="{self.user}", realm="{p["realm"]}", nonce="{p["nonce"]}", uri="{uri}", '
                f'response="{resp}"')

    def request(self, method: str, url: str, headers: dict | None = None, retry_auth: bool = True):
        self.cseq += 1
        h = {"CSeq": str(self.cseq), "User-Agent": "selfcheck", **(headers or {})}
        a = self._auth_header(method, url)
        if a:
            h["Authorization"] = a
        if self.session and "Session" not in h:
            h["Session"] = self.session
        msg = f"{method} {url} RTSP/1.0\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n"
        self.sock.sendall(msg.encode())
        status, hdrs, body = self.read_response()
        assert hdrs.get("cseq", [""])[0] == str(self.cseq)
        if status == 401 and retry_auth and self.user and not self.auth:
            for w in hdrs.get("www-authenticate", []):
                if w.lower().startswith("digest"):
                    import re

                    p = dict(re.findall(r'(\w+)="([^"]*)"', w))
                    self.auth = ("digest", p)
                    break
            return self.request(method, url, headers, retry_auth=False)
        if "session" in hdrs:
            self.session = hdrs["session"][0].split(";")[0]
        return status, hdrs, body

    def play(self, path: str = "stream2") -> None:
        s, _, _ = self.request("DESCRIBE", self.url(path), {"Accept": "application/sdp"})
        assert s == 200, s
        s, _, _ = self.request("SETUP", self.url(path) + "/track1", {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"})
        assert s == 200, s
        s, _, _ = self.request("SETUP", self.url(path) + "/track2", {"Transport": "RTP/AVP/TCP;unicast;interleaved=2-3"})
        assert s == 200, s
        s, _, _ = self.request("PLAY", self.url(path), {"Range": "npt=0.000-"})
        assert s == 200, s

    def read_rtp(self, count: int, timeout: float = 5.0) -> list[tuple[int, bytes]]:
        deadline = time.monotonic() + timeout
        while len(self.rtp) < count and time.monotonic() < deadline:
            while self.buf[:1] == b"$" and len(self.buf) >= 4:
                n = struct.unpack(">H", self.buf[2:4])[0]
                if len(self.buf) < 4 + n:
                    break
                self.rtp.append((self.buf[1], self.buf[4:4 + n]))
                self.buf = self.buf[4 + n:]
            if len(self.rtp) >= count:
                break
            self._recv()
        return self.rtp

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------------------------
# ONVIF
# ---------------------------------------------------------------------------------------------
def wsse(user: str, password: str, created: str | None = None, nonce: bytes | None = None,
         password_type: str = "PasswordDigest") -> str:
    created = created or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    nonce = nonce if nonce is not None else secrets.token_bytes(16)
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()
    pw = digest if password_type == "PasswordDigest" else password
    return ('<wsse:Security xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" '
            'xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
            f"<wsse:UsernameToken><wsse:Username>{user}</wsse:Username>"
            f'<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#{password_type}">'
            f"{pw}</wsse:Password><wsse:Nonce>{base64.b64encode(nonce).decode()}</wsse:Nonce>"
            f"<wsu:Created>{created}</wsu:Created></wsse:UsernameToken></wsse:Security>")


def soap(host: str, port: int, body: str, user: str | None = None, password: str | None = None,
         path: str = "/onvif/device_service", header_extra: str = "", timeout: float = 10.0) -> tuple[int, bytes]:
    hdr = (wsse(user, password) if user is not None else "") + header_extra
    env = ('<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
           'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
           'xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl" xmlns:tev="http://www.onvif.org/ver10/events/wsdl" '
           'xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2" xmlns:tt="http://www.onvif.org/ver10/schema" '
           'xmlns:wsa="http://www.w3.org/2005/08/addressing">'
           f"<s:Header>{hdr}</s:Header><s:Body>{body}</s:Body></s:Envelope>")
    c = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        c.request("POST", path, body=env.encode(), headers={"Content-Type": "application/soap+xml; charset=utf-8"})
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()


# ---------------------------------------------------------------------------------------------
# :8800 media
# ---------------------------------------------------------------------------------------------
class MediaRaw:
    BOUNDARY = b"----client-stream-boundary--"

    def __init__(self, host: str, port: int, timeout: float = 5.0, sock=None):
        self.host, self.port = host, port
        self.sock = sock if sock is not None else socket.create_connection((host, port), timeout=timeout)
        if sock is not None:
            sock.settimeout(timeout)
        self.buf = b""
        self.key = self.iv = b""
        self.seq = 0
        self.session_id = ""

    def _recv(self) -> None:
        c = self.sock.recv(65536)
        if not c:
            raise ConnectionError("closed")
        self.buf += c

    def _line(self) -> bytes:
        while b"\r\n" not in self.buf:
            self._recv()
        ln, _, self.buf = self.buf.partition(b"\r\n")
        return ln

    def _exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            self._recv()
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _headers(self) -> dict[str, str]:
        h = {}
        while True:
            ln = self._line()
            if not ln:
                return h
            k, _, v = ln.decode().partition(":")
            h[k.strip().lower()] = v.strip()

    def http_post(self, auth: str | None = None, key_exchange: bool = True) -> tuple[int, dict, bytes]:
        lines = ["POST /stream HTTP/1.1", "Content-Type: multipart/mixed; boundary=--client-stream-boundary--",
                 f"Host: {self.host}:{self.port}", "Connection: Keep-Alive", "Content-Length: 0"]
        if auth:
            lines.insert(2, f"Authorization: {auth}")
        if key_exchange:
            lines.append("X-Key-Exchange: 1")
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        status = int(self._line().decode().split(" ")[1])
        h = self._headers()
        body = self._exact(int(h["content-length"])) if "content-length" in h else b""
        return status, h, body

    def connect(self, cloud_password: str, encrypt_v3: bool, algorithm: str | None = None,
                key_exchange: bool = True) -> tuple[int, dict, bytes]:
        import re

        s, h, _ = self.http_post(key_exchange=key_exchange)
        assert s == 401, s
        p = dict((k, a or b) for k, a, b in re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]+))', h["www-authenticate"][7:]))
        alg = algorithm or p.get("algorithm", "MD5")
        H = (lambda x: hashlib.sha256(x.encode()).hexdigest()) if alg.upper() == "SHA-256" else \
            (lambda x: hashlib.md5(x.encode()).hexdigest())
        pwd_t = sha256u(cloud_password) if p.get("encrypt_type") == "3" else md5u(cloud_password)
        assert (p.get("encrypt_type") == "3") == encrypt_v3
        cnonce = "a9h5b7i3j2y8c0a6"
        ha1, ha2 = H(f"admin:{p['realm']}:{pwd_t}"), H("POST:/stream")
        resp = H(f"{ha1}:{p['nonce']}:00000001:{cnonce}:auth:{ha2}")
        auth = (f'Digest username="admin",realm="{p["realm"]}",uri="/stream",algorithm={alg},nonce="{p["nonce"]}",'
                f'nc=00000001,cnonce="{cnonce}",qop=auth,response="{resp}",opaque="{p.get("opaque", "")}"')
        s, h, body = self.http_post(auth, key_exchange=key_exchange)
        if s == 200 and "key-exchange" in h:
            kx = dict(re.findall(r'(\w+)="([^"]*)"', h["key-exchange"]))
            user = kx.get("username", "admin")
            self.key = hashlib.md5((kx["nonce"] + ":" + pwd_t).encode()).digest()
            self.iv = hashlib.md5((user + ":" + kx["nonce"]).encode()).digest()
        return s, h, body

    def send_part(self, obj: dict, extra: dict | None = None) -> None:
        body = cj(obj).encode()
        hdr = {"Content-Type": "application/json", **({"X-Session-Id": self.session_id} if self.session_id else {}),
               **(extra or {}), "Content-Length": str(len(body))}
        self.sock.sendall(self.BOUNDARY + b"\r\n" + "".join(f"{k}: {v}\r\n" for k, v in hdr.items()).encode()
                          + b"\r\n" + body + b"\r\n")

    def request(self, params: dict) -> int:
        self.seq += 1
        self.send_part({"type": "request", "seq": self.seq, "params": params})
        return self.seq

    def read_part(self) -> tuple[dict, bytes]:
        while True:
            ln = self._line()
            if b"--device-stream-boundary--" in ln:
                break
        h = self._headers()
        body = self._exact(int(h["content-length"]))
        if h.get("x-if-encrypt") == "1":
            body = aes_dec(self.key, self.iv, body)
        return h, body

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------------------------
# in-memory socket pair (lets the self-check drive a server handler without the OS TCP stack,
# e.g. when a local HTTP filter such as AdGuard hijacks loopback HTTP connections)
# ---------------------------------------------------------------------------------------------
import threading  # noqa: E402


class MemSocket:
    def __init__(self) -> None:
        self.inbox = bytearray()
        self.cond = threading.Condition()
        self.peer: "MemSocket | None" = None
        self.closed = False
        self.eof = False          # peer will send no more data
        self.timeout: float | None = None

    def settimeout(self, t: float | None) -> None:
        self.timeout = t

    def gettimeout(self) -> float | None:
        return self.timeout

    def setsockopt(self, *a) -> None:
        pass

    def recv(self, n: int) -> bytes:
        deadline = None if self.timeout is None else time.monotonic() + self.timeout
        with self.cond:
            while not self.inbox and not self.eof and not self.closed:
                left = None if deadline is None else deadline - time.monotonic()
                if left is not None and left <= 0:
                    raise TimeoutError("timed out")
                self.cond.wait(left)
            if self.closed:
                raise OSError("socket closed")
            out = bytes(self.inbox[:n])
            del self.inbox[:n]
            return out

    def send(self, data) -> int:
        peer = self.peer
        if self.closed or peer is None or peer.closed or self.eof_sent:
            raise ConnectionResetError("peer closed")
        with peer.cond:
            peer.inbox += bytes(data)
            peer.cond.notify_all()
        return len(data)

    eof_sent = False

    def sendall(self, data) -> None:
        self.send(data)

    def _signal_eof(self) -> None:
        self.eof_sent = True
        peer = self.peer
        if peer is not None:
            with peer.cond:
                peer.eof = True
                peer.cond.notify_all()

    def shutdown(self, how: int) -> None:
        self._signal_eof()

    def close(self) -> None:
        self._signal_eof()
        with self.cond:
            self.closed = True
            self.cond.notify_all()


def mem_socketpair() -> tuple[MemSocket, MemSocket]:
    a, b = MemSocket(), MemSocket()
    a.peer, b.peer = b, a
    return a, b


def loopback_http_stream_passthrough(timeout: float = 2.0) -> bool:
    """True if raw bytes sent after an HTTP response on a loopback socket reach the server unmodified.

    Local HTTP filters (e.g. AdGuard for Windows) proxy loopback HTTP and answer the follow-up
    multipart bytes of the :8800 protocol with their own ``400 Bad request`` page.
    """
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    got: list[bytes] = []

    def serve() -> None:
        try:
            c, _ = srv.accept()
            c.settimeout(timeout)
            buf = b""
            while b"\r\n\r\n" not in buf:
                buf += c.recv(4096)
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: multipart/mixed;boundary=--device-stream-boundary--\r\n\r\n")
            got.append(c.recv(4096))
            c.close()
        except OSError:
            pass

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        s = socket.create_connection(srv.getsockname(), timeout=timeout)
        s.sendall(b"POST /stream HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n")
        s.recv(4096)
        s.sendall(b"----client-stream-boundary--\r\n")
        t.join(timeout + 0.5)
        s.close()
    except OSError:
        return False
    finally:
        srv.close()
    return bool(got) and got[0].startswith(b"----client-stream-boundary--")
