"""Lane-private test helpers for media/vod unit tests (lane-tdd-media).

Independent reference implementations of [spec:stream.local8800.*] written straight from the spec
text (not shared with production code), plus a tiny in-process fake of the camera's :8800
``POST /stream`` endpoint so MediaSession can be unit tested without the full mock camera.
"""
from __future__ import annotations

import hashlib
import json
import re
import socket
import threading
import time
from dataclasses import dataclass, field

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

DEFAULT_MEDIA_PASSWORD = "TPL075526460603"
CLIENT_PART_PREFIX = b"----client-stream-boundary--\r\n"
DEVICE_PART_PREFIX = b"----device-stream-boundary--\r\n"


# ------------------------------------------------------------------ reference crypto (from the spec)
def ref_transform(password: str, encrypt_type: str | None) -> str:
    if str(encrypt_type) == "3":
        return hashlib.sha256(password.encode()).hexdigest().upper()
    return hashlib.md5(password.encode()).hexdigest().upper()


def ref_hash(alg: str, s: str) -> str:
    name = "sha256" if alg.upper().replace("-SESS", "") == "SHA-256" else "md5"
    return hashlib.new(name, s.encode()).hexdigest()


def ref_digest_response(username, realm, password, method, uri, nonce, nc, cnonce, qop, algorithm="MD5"):
    ha1 = ref_hash(algorithm, f"{username}:{realm}:{password}")
    if algorithm.upper().endswith("-SESS"):
        ha1 = ref_hash(algorithm, f"{ha1}:{nonce}:{cnonce}")
    ha2 = ref_hash(algorithm, f"{method}:{uri}")
    return ref_hash(algorithm, f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")


def ref_key_iv(ke_username: str, nonce: str, transformed_password: str) -> tuple[bytes, bytes]:
    secret = transformed_password if ke_username == "admin" else DEFAULT_MEDIA_PASSWORD
    key = hashlib.md5(f"{nonce}:{secret}".encode()).digest()
    iv = hashlib.md5(f"{ke_username}:{nonce}".encode()).digest()
    return key, iv


def aes_encrypt(plaintext: bytes, key: bytes, iv: bytes) -> bytes:
    padder = padding.PKCS7(128).padder()
    data = padder.update(plaintext) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return enc.update(data) + enc.finalize()


def parse_auth_params(value: str) -> dict[str, str]:
    assert value.startswith("Digest "), value
    out = {}
    for m in re.finditer(r'(\w+)=("([^"]*)"|[^,]*)', value[len("Digest "):]):
        out[m.group(1)] = m.group(3) if m.group(3) is not None else m.group(2)
    return out


def device_part(headers: dict[str, str], body: bytes, trailing_crlf: bool = True) -> bytes:
    head = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    head += f"Content-Length: {len(body)}\r\n\r\n"
    return DEVICE_PART_PREFIX + head.encode() + body + (b"\r\n" if trailing_crlf else b"")


def parse_client_parts(data: bytes) -> list[tuple[dict[str, str], bytes]]:
    """Strict reference parser for what the client sent after the 200 (spec framing)."""
    parts = []
    pos = 0
    while True:
        start = data.find(CLIENT_PART_PREFIX, pos)
        if start < 0:
            return parts
        hdr_end = data.find(b"\r\n\r\n", start)
        if hdr_end < 0:
            return parts
        lines = data[start + len(CLIENT_PART_PREFIX):hdr_end].decode().split("\r\n")
        headers = {}
        for line in lines:
            if line:
                k, _, v = line.partition(":")
                headers[k.strip()] = v.strip()
        n = int(headers["Content-Length"])
        body = data[hdr_end + 4:hdr_end + 4 + n]
        if len(body) < n:
            return parts
        parts.append((headers, body))
        pos = hdr_end + 4 + n


# ------------------------------------------------------------------ fake :8800 endpoint
@dataclass
class FakePart:
    headers: dict[str, str]
    body: bytes  # plaintext
    encrypt: bool = False


@dataclass
class FakeMediaServer:
    password: str  # cloud password (plaintext) the camera expects
    algorithm: str | None = "MD5"
    encrypt_type: str | None = None
    ke_username: str = "admin"
    realm: str = "TP-LINK IP-CAM"
    nonce: str = "8c5d1f22a1b04e6f"
    opaque: str = "64943214654649846565646421"
    ke_nonce: str = "0d4a5e9f3b2c1a77"
    parts: list[FakePart] = field(default_factory=list)
    busy: bool = False
    silent: bool = False  # accept and never answer
    linger: float = 0.3  # keep the stream open this long after the last part

    def __post_init__(self):
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.client_bytes = bytearray()
        self.auth_ok = False
        self.connections = 0
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self._sock.settimeout(0.1)
        self.port = self._sock.getsockname()[1]
        self._threads: list[threading.Thread] = []

    # context manager
    def __enter__(self):
        t = threading.Thread(target=self._accept_loop, daemon=True)
        t.start()
        self._threads.append(t)
        return self

    def __exit__(self, *exc):
        self._stop.set()
        for t in self._threads:
            t.join(3)
        self._sock.close()

    def wait_client_bytes(self, needle: bytes, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if needle in self.client_bytes:
                return True
            time.sleep(0.01)
        return needle in self.client_bytes

    # internals
    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (socket.timeout, OSError):
                continue
            self.connections += 1
            t = threading.Thread(target=self._serve, args=(conn,), daemon=True)
            t.start()
            self._threads.append(t)

    def _read_request(self, conn, buf: bytearray):
        while b"\r\n\r\n" not in buf:
            try:
                chunk = conn.recv(65536)
            except socket.timeout:
                if self._stop.is_set():
                    return None
                continue
            except OSError:
                return None
            if not chunk:
                return None
            buf += chunk
        raw, _, rest = bytes(buf).partition(b"\r\n\r\n")
        buf[:] = rest
        lines = raw.decode("latin-1").split("\r\n")
        headers = {}
        for line in lines[1:]:
            k, _, v = line.partition(":")
            headers[k.strip()] = v.strip()
        return lines[0], headers

    def _challenge(self) -> str:
        parts = [f'realm="{self.realm}"', 'qop="auth"', f'nonce="{self.nonce}"', f'opaque="{self.opaque}"']
        if self.algorithm:
            parts.append(f"algorithm={self.algorithm}")
        if self.encrypt_type:
            parts.append(f"encrypt_type={self.encrypt_type}")
        return "Digest " + ", ".join(parts)

    def _auth_valid(self, value: str) -> bool:
        try:
            p = parse_auth_params(value)
            alg = p.get("algorithm", "MD5")
            if (self.algorithm or "MD5") != alg or p["nonce"] != self.nonce or p["uri"] != "/stream":
                return False
            if p.get("opaque") != self.opaque or p["username"] != "admin":
                return False
            pw = ref_transform(self.password or DEFAULT_MEDIA_PASSWORD, self.encrypt_type)
            expected = ref_digest_response(
                p["username"], self.realm, pw, "POST", p["uri"], p["nonce"], p["nc"], p["cnonce"], p["qop"], alg
            )
            return p["response"] == expected and p["nc"] == "00000001" and p["qop"] == "auth"
        except Exception:
            return False

    def _serve(self, conn: socket.socket):
        conn.settimeout(0.05)
        buf = bytearray()
        try:
            if self.silent:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not self._stop.is_set():
                    time.sleep(0.05)
                return
            while not self._stop.is_set():
                req = self._read_request(conn, buf)
                if req is None:
                    return
                line, headers = req
                self.requests.append((line, headers))
                if self.busy:
                    body = json.dumps({"error_code": -52407}).encode()
                    conn.sendall(
                        b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\n"
                        + f"Content-Length: {len(body)}\r\n\r\n".encode() + body
                    )
                    return
                auth = headers.get("Authorization")
                if not auth:
                    conn.sendall(
                        b"HTTP/1.1 401 Unauthorized\r\nWWW-Authenticate: " + self._challenge().encode()
                        + b"\r\nContent-Length: 0\r\n\r\n"
                    )
                    continue
                if not self._auth_valid(auth):
                    conn.sendall(
                        b"HTTP/1.1 401 Unauthorized\r\nWWW-Authenticate: " + self._challenge().encode()
                        + b"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                    )
                    return
                self.auth_ok = True
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: multipart/mixed;boundary=--device-stream-boundary--\r\n"
                    b"Connection: keep-alive\r\n"
                    + f'Key-Exchange: username="{self.ke_username}" nonce="{self.ke_nonce}" cipher="AES_128_CBC"\r\n\r\n'.encode()
                )
                self.client_bytes += buf
                pw_t = ref_transform(self.password or DEFAULT_MEDIA_PASSWORD, self.encrypt_type)
                key, iv = ref_key_iv(self.ke_username, self.ke_nonce, pw_t)
                for part in self.parts:
                    headers_out = dict(part.headers)
                    body = part.body
                    if part.encrypt:
                        headers_out["X-If-Encrypt"] = "1"
                        body = aes_encrypt(body, key, iv)
                    conn.sendall(device_part(headers_out, body))
                deadline = time.monotonic() + self.linger
                while time.monotonic() < deadline and not self._stop.is_set():
                    try:
                        chunk = conn.recv(65536)
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    if not chunk:
                        break
                    self.client_bytes += chunk
                return
        finally:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()
