"""Local media connection (POST /stream on :8800)  [spec:stream.local8800.*, vod.stream.*].

Pure helpers (module level, no I/O) are part of the public contract so they can be unit tested:
``parse_www_authenticate``, ``transform_password``, ``generate_cnonce``, ``digest_response``,
``digest_authorization``, ``parse_key_exchange``, ``derive_stream_key_iv``, ``decrypt_part``,
``encode_client_part``, ``build_ack_part``, ``MultipartReader``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import socket
import string
import threading
from dataclasses import dataclass
from typing import Iterator

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from tapo_desktop.core.errors import (
    StreamLimitError,
    TapoAuthError,
    TapoConnectionError,
    TapoError,
    TapoProtocolError,
)

__all__ = [
    "CLIENT_BOUNDARY",
    "DEVICE_BOUNDARY",
    "DEFAULT_MEDIA_USERNAME",
    "DEFAULT_MEDIA_PASSWORD",
    "MediaPart",
    "MediaSession",
    "MultipartReader",
    "parse_www_authenticate",
    "transform_password",
    "generate_cnonce",
    "digest_response",
    "digest_authorization",
    "parse_key_exchange",
    "derive_stream_key_iv",
    "decrypt_part",
    "encode_client_part",
    "build_ack_part",
]

logger = logging.getLogger(__name__)

CLIENT_BOUNDARY = "--client-stream-boundary--"  # parts start with "--" + CLIENT_BOUNDARY
DEVICE_BOUNDARY = "--device-stream-boundary--"  # parts start with "--" + DEVICE_BOUNDARY
DEFAULT_MEDIA_USERNAME = "admin"  # [spec:stream.local8800.default_credentials]
DEFAULT_MEDIA_PASSWORD = "TPL075526460603"

_STREAM_LIMIT_CODE = -52407
_AUTH_PARAM_RE = re.compile(r'([A-Za-z0-9_\-]+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^,\s]*))')
_KEY_EXCHANGE_RE = re.compile(r'([A-Za-z0-9_\-]+)=(?:"([^"]*)"|(\S*))')


@dataclass
class MediaPart:
    headers: dict[str, str]  # header names exactly as received, values stripped
    body: bytes  # MediaSession.parts(): already decrypted; MultipartReader: raw


def _compact_json(obj: object) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


# --------------------------------------------------------------------------- pure helpers


def parse_www_authenticate(value: str) -> dict[str, str]:
    """Parse a ``WWW-Authenticate: Digest k=v, k="v", ...`` value into a dict.

    Keys are lower-cased, quotes stripped, commas inside quoted values preserved.
    Raises TapoProtocolError if the scheme is not Digest or there is no nonce.
    """
    if not isinstance(value, str):
        raise TapoProtocolError("WWW-Authenticate header is not a string")
    text = value.strip()
    scheme, _, rest = text.partition(" ")
    if scheme.lower() != "digest":
        raise TapoProtocolError("media port did not offer Digest authentication")
    params: dict[str, str] = {}
    for match in _AUTH_PARAM_RE.finditer(rest):
        quoted, bare = match.group(2), match.group(3)
        params[match.group(1).lower()] = quoted.replace('\\"', '"') if quoted is not None else bare
    if not params.get("nonce"):
        raise TapoProtocolError("Digest challenge without nonce")
    return params


def transform_password(password: str, encrypt_type: str | int | None = None) -> str:
    """[spec:stream.local8800.auth.digest] UPPERCASE_HEX(SHA256(pw)) if encrypt_type == 3 else UPPERCASE_HEX(MD5(pw))."""
    algorithm = hashlib.sha256 if str(encrypt_type).strip() == "3" else hashlib.md5
    return algorithm(password.encode("utf-8")).hexdigest().upper()


def generate_cnonce() -> str:
    """16 chars alternating random lowercase letter / digit (``a1b2...``)."""
    return "".join(secrets.choice(string.ascii_lowercase) + secrets.choice(string.digits) for _ in range(8))


def _digest_hash(algorithm: str, text: str) -> str:
    base = algorithm.upper()
    if base.endswith("-SESS"):
        base = base[: -len("-SESS")]
    if base in ("SHA-256", "SHA256"):
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    if base == "MD5":
        return hashlib.md5(text.encode("utf-8")).hexdigest()
    raise TapoProtocolError(f"unsupported Digest algorithm {algorithm!r}")


def digest_response(
    *,
    username: str,
    realm: str,
    password: str,
    method: str,
    uri: str,
    nonce: str,
    nc: str,
    cnonce: str,
    qop: str,
    algorithm: str = "MD5",
) -> str:
    """RFC 7616 response (lowercase hex). ``password`` is used as-is (no transform).

    ``algorithm``: MD5, SHA-256 and their ``-sess`` variants (HA1 = H(H(u:r:p):nonce:cnonce)).
    """
    ha1 = _digest_hash(algorithm, f"{username}:{realm}:{password}")
    if algorithm.upper().endswith("-SESS"):
        ha1 = _digest_hash(algorithm, f"{ha1}:{nonce}:{cnonce}")
    ha2 = _digest_hash(algorithm, f"{method}:{uri}")
    return _digest_hash(algorithm, f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")


def digest_authorization(
    challenge: dict[str, str],
    password: str,
    *,
    username: str = DEFAULT_MEDIA_USERNAME,
    method: str = "POST",
    uri: str = "/stream",
    cnonce: str | None = None,
) -> str:
    """Build the ``Authorization`` header value for the media port.

    ``challenge`` is :func:`parse_www_authenticate` output; ``password`` is the PLAINTEXT cloud
    password (empty -> DEFAULT_MEDIA_PASSWORD), transformed with :func:`transform_password` using
    ``challenge["encrypt_type"]``. Hash = challenge algorithm (default MD5); qop = first token of the
    challenge qop; nc = 00000001; userhash=true -> username := H(username:realm). Format:
    ``Digest username="admin",realm="<r>",uri="/stream",algorithm=<alg>,nonce="<n>",nc=00000001,
    cnonce="<c>",qop=<qop>,response="<resp>",opaque="<o>"[,userhash=true]`` (no spaces).
    """
    secret = transform_password(password or DEFAULT_MEDIA_PASSWORD, challenge.get("encrypt_type"))
    algorithm = challenge.get("algorithm") or "MD5"
    qop = (challenge.get("qop") or "auth").split(",")[0].strip() or "auth"
    realm = challenge.get("realm", "")
    nonce = challenge["nonce"]
    cnonce = cnonce or generate_cnonce()
    userhash = challenge.get("userhash", "").strip().lower() == "true"
    if userhash:
        username = _digest_hash(algorithm, f"{username}:{realm}")
    response = digest_response(
        username=username, realm=realm, password=secret, method=method, uri=uri,
        nonce=nonce, nc="00000001", cnonce=cnonce, qop=qop, algorithm=algorithm,
    )
    header = (
        f'Digest username="{username}",realm="{realm}",uri="{uri}",algorithm={algorithm},nonce="{nonce}",'
        f'nc=00000001,cnonce="{cnonce}",qop={qop},response="{response}",opaque="{challenge.get("opaque", "")}"'
    )
    return header + (",userhash=true" if userhash else "")


def parse_key_exchange(value: str) -> dict[str, str]:
    """Parse ``Key-Exchange: username="admin" nonce="<hex>" [cipher=..] ...`` (space separated k=v, quotes stripped).

    Raises TapoProtocolError when ``nonce`` is missing.
    """
    if not isinstance(value, str):
        raise TapoProtocolError("Key-Exchange header is not a string")
    params = {m.group(1): m.group(2) if m.group(2) is not None else m.group(3)
              for m in _KEY_EXCHANGE_RE.finditer(value)}
    if not params.get("nonce"):
        raise TapoProtocolError("Key-Exchange header without nonce")
    return params


def derive_stream_key_iv(key_exchange: dict[str, str], transformed_password: str) -> tuple[bytes, bytes] | None:
    """[spec:stream.local8800.encryption.md5] Return (key, iv), 16 raw bytes each, or None.

    username "admin" -> secret = transformed_password; "none" -> secret = DEFAULT_MEDIA_PASSWORD;
    any other username -> None (no decryption possible).
    key = MD5(nonce + ":" + secret), iv = MD5(username + ":" + nonce).
    """
    username = key_exchange.get("username")
    if username == "admin":
        secret = transformed_password
    elif username == "none":
        secret = DEFAULT_MEDIA_PASSWORD
    else:
        return None
    nonce = key_exchange["nonce"]
    key = hashlib.md5(f"{nonce}:{secret}".encode("utf-8")).digest()
    iv = hashlib.md5(f"{username}:{nonce}".encode("utf-8")).digest()
    return key, iv


def decrypt_part(body: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-128-CBC / PKCS7 decrypt one part (fresh cipher per part). Bad length/padding -> TapoProtocolError."""
    if not body or len(body) % 16:
        raise TapoProtocolError(f"encrypted part length {len(body)} is not a positive multiple of 16")
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    plain = decryptor.update(body) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    try:
        return unpadder.update(plain) + unpadder.finalize()
    except ValueError as exc:
        raise TapoProtocolError("encrypted part has invalid PKCS7 padding") from exc


def encode_client_part(
    body: bytes,
    *,
    content_type: str = "application/json",
    headers: dict[str, str] | None = None,
) -> bytes:
    """Encode one client part [spec:stream.local8800.framing]::

        ----client-stream-boundary--\\r\\n
        <extra headers in insertion order>\\r\\n...
        Content-Type: <content_type>\\r\\n
        Content-Length: <len(body)>\\r\\n
        \\r\\n
        <body>\\r\\n
    """
    lines = [f"--{CLIENT_BOUNDARY}"]
    lines += [f"{name}: {value}" for name, value in (headers or {}).items()]
    lines += [f"Content-Type: {content_type}", f"Content-Length: {len(body)}", "", ""]
    return "\r\n".join(lines).encode("utf-8") + bytes(body) + b"\r\n"


def build_ack_part(received_seq: int, session_id: str) -> bytes:
    """[spec:vod.stream.flow_control_ack] ``encode_client_part`` of the compact JSON
    ``{"type":"notification","params":{"event_type":"stream_sequence"}}`` with headers
    ``X-Data-Received: <received_seq>`` then ``X-Session-Id: <session_id>``.
    """
    body = _compact_json({"type": "notification", "params": {"event_type": "stream_sequence"}})
    return encode_client_part(body, headers={"X-Data-Received": str(received_seq), "X-Session-Id": str(session_id)})


class MultipartReader:
    """Incremental parser of the device multipart stream (bodies NOT decrypted).

    A part starts at a line equal to ``"--" + boundary``; headers follow until a blank line, then
    exactly Content-Length body bytes. CRLFs / junk lines between parts and the closing
    ``"--" + boundary + "--"`` line are ignored. Missing/invalid/negative Content-Length or a
    body larger than ``max_part_size`` raises TapoProtocolError (never any other exception).
    """

    _MAX_HEADER_BLOCK = 64 * 1024

    def __init__(self, boundary: str = DEVICE_BOUNDARY, max_part_size: int = 16 * 1024 * 1024):
        self._delimiter = f"--{boundary}".encode("utf-8")
        self._max_part_size = max_part_size
        self._buf = bytearray()
        self._pending: tuple[dict[str, str], int] | None = None  # headers parsed, body awaited

    def feed(self, data: bytes) -> list[MediaPart]:
        """Add bytes (arbitrary chunking) and return every part completed by them, in order."""
        self._buf += data
        parts: list[MediaPart] = []
        while True:
            if self._pending is None and not self._parse_headers():
                return parts
            headers, length = self._pending
            if len(self._buf) < length:
                return parts
            parts.append(MediaPart(headers, bytes(self._buf[:length])))
            del self._buf[:length]
            self._pending = None

    @property
    def buffered(self) -> int:
        """Number of bytes held that do not yet form a complete part."""
        return len(self._buf)

    def _parse_headers(self) -> bool:
        """Skip to the next delimiter line and parse its header block. False = need more data."""
        buf = self._buf
        while True:
            eol = buf.find(b"\r\n")
            if eol < 0:
                if len(buf) > self._MAX_HEADER_BLOCK:  # junk without line breaks: keep a tail only
                    del buf[: len(buf) - len(self._delimiter)]
                return False
            if buf[:eol].rstrip() == self._delimiter:
                break
            del buf[: eol + 2]  # junk, blank or closing-delimiter line
        end = buf.find(b"\r\n\r\n", eol)
        if end < 0:
            if len(buf) > self._MAX_HEADER_BLOCK:
                raise TapoProtocolError("multipart header block too large")
            return False
        headers: dict[str, str] = {}
        for line in bytes(buf[eol + 2:end]).decode("latin-1").split("\r\n"):
            name, sep, value = line.partition(":")
            if sep and name.strip():
                headers[name.strip()] = value.strip()
        length = self._content_length(headers)
        del buf[: end + 4]
        self._pending = (headers, length)
        return True

    def _content_length(self, headers: dict[str, str]) -> int:
        raw = next((v for k, v in headers.items() if k.lower() == "content-length"), None)
        if raw is None:
            raise TapoProtocolError("multipart part without Content-Length")
        if not (raw.isascii() and raw.isdigit()):
            raise TapoProtocolError("multipart part with invalid Content-Length")
        length = int(raw)
        if length > self._max_part_size:
            raise TapoProtocolError(f"multipart part length {length} out of range")
        return length


# --------------------------------------------------------------------------- connection


@dataclass
class _HttpResponse:
    status: int
    headers: dict[str, str]  # lower-cased names
    body: bytes


class MediaSession:
    """One persistent ``POST /stream`` connection to the camera media port."""

    _MAX_HEAD = 64 * 1024
    _MAX_ERROR_BODY = 1024 * 1024
    _RECV_SIZE = 256 * 1024

    def __init__(
        self,
        host: str,
        password: str,
        *,
        port: int = 8800,
        username: str = DEFAULT_MEDIA_USERNAME,
        timeout: float = 10.0,
        local_token: str | None = None,
    ):
        """``password`` = cloud password (plaintext)."""
        self._host = host
        self._port = int(port)
        self._password = password
        self._local_token = local_token or None
        self._username = username
        self._timeout = timeout
        self._sock: socket.socket | None = None
        self._rx = bytearray()  # bytes received after the HTTP response head
        self._reader = MultipartReader()
        self._keys: tuple[bytes, bytes] | None = None
        self._seq = 0
        self._session_id: str | None = None
        self._closed = False
        self._send_lock = threading.Lock()

    def __repr__(self) -> str:
        return f"MediaSession(host={self._host!r}, port={self._port}, connected={self._sock is not None})"

    @property
    def session_id(self) -> str | None:
        """Stream session id announced by the camera (None until a response carried one)."""
        return self._session_id

    # ------------------------------------------------------------------ handshake
    def connect(self) -> None:
        """Digest handshake [spec:stream.local8800.handshake] and Key-Exchange parsing.

        401 after the authorized retry -> TapoAuthError; HTTP 503 / -52407 -> StreamLimitError;
        socket errors / timeouts -> TapoConnectionError; malformed response -> TapoProtocolError.
        """
        if self._sock is not None:
            raise TapoProtocolError("media session is already connected")
        self._closed = False
        self._reader = MultipartReader()
        self._session_id = None
        try:
            self._open_socket()
            response = self._post(None)
            challenge: dict[str, str] = {}
            if response.status == 401:
                challenge = parse_www_authenticate(response.headers.get("www-authenticate", ""))
                if response.headers.get("connection", "").lower() == "close":
                    self._reopen_socket()
                authorization = digest_authorization(challenge, self._password, username=self._username)
                response = self._post(authorization)
                if response.status == 401:
                    raise TapoAuthError(f"media port {self._host}:{self._port} rejected the cloud password")
            self._check_status(response)
            self._setup_encryption(response, challenge)
            logger.debug("media session to %s:%d established (encrypted=%s)", self._host, self._port,
                         self._keys is not None)
        except TapoError:
            self.close()
            raise
        except socket.timeout:
            self.close()
            raise TapoConnectionError(f"timed out talking to media port {self._host}:{self._port}") from None
        except OSError as exc:
            self.close()
            reason = exc.strerror or type(exc).__name__
            raise TapoConnectionError(f"cannot reach media port {self._host}:{self._port}: {reason}") from None

    def _open_socket(self) -> None:
        self._sock = socket.create_connection((self._host, self._port), timeout=self._timeout)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._rx.clear()

    def _reopen_socket(self) -> None:
        self._close_socket()
        self._open_socket()

    def _host_header(self) -> str:
        host = self._host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"{host}:{self._port}"

    def _post(self, authorization: str | None) -> _HttpResponse:
        lines = [
            "POST /stream HTTP/1.1",
            f"Host: {self._host_header()}",
            f"Content-Type: multipart/mixed; boundary={CLIENT_BOUNDARY}",
            "Connection: keep-alive",
            "Content-Length: 0",
            "X-Key-Exchange: 1",
        ]
        if authorization:
            lines.append(f"Authorization: {authorization}")
        self._sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        return self._read_response()

    def _recv_more(self) -> None:
        assert self._sock is not None
        chunk = self._sock.recv(self._RECV_SIZE)
        if not chunk:
            raise TapoConnectionError(f"media port {self._host}:{self._port} closed the connection")
        self._rx += chunk

    def _read_response(self) -> _HttpResponse:
        while (end := self._rx.find(b"\r\n\r\n")) < 0:
            if len(self._rx) > self._MAX_HEAD:
                raise TapoProtocolError("HTTP response head too large")
            self._recv_more()
        head = bytes(self._rx[:end]).decode("latin-1")
        del self._rx[: end + 4]
        status_line, *header_lines = head.split("\r\n")
        fields = status_line.split(" ", 2)
        if len(fields) < 2 or not fields[0].startswith("HTTP/") or not fields[1].isdigit():
            raise TapoProtocolError("malformed HTTP status line from media port")
        headers: dict[str, str] = {}
        for line in header_lines:
            name, sep, value = line.partition(":")
            if sep:
                headers[name.strip().lower()] = value.strip()
        status = int(fields[1])
        body = b""
        if status != 200:  # a 200 starts the multipart stream; anything else may carry a body
            length = headers.get("content-length", "0")
            n = int(length) if length.isdigit() else 0
            if n > self._MAX_ERROR_BODY:
                raise TapoProtocolError("HTTP error body too large")
            while len(self._rx) < n:
                self._recv_more()
            body = bytes(self._rx[:n])
            del self._rx[:n]
        return _HttpResponse(status, headers, body)

    def _check_status(self, response: _HttpResponse) -> None:
        if response.status == 200:
            return
        code = None
        try:
            code = json.loads(response.body or b"{}").get("error_code")
        except (ValueError, AttributeError):
            pass
        if response.status == 503 or code == _STREAM_LIMIT_CODE:
            raise StreamLimitError(
                f"camera {self._host} refused another media session (HTTP {response.status}, code {code})"
            )
        if response.status == 401:
            raise TapoAuthError(f"media port {self._host}:{self._port} rejected the cloud password")
        raise TapoProtocolError(f"unexpected HTTP status {response.status} from media port")

    def _setup_encryption(self, response: _HttpResponse, challenge: dict[str, str]) -> None:
        self._keys = None
        value = response.headers.get("key-exchange")
        if not value:
            return
        key_exchange = parse_key_exchange(value)
        transformed = transform_password(self._password or DEFAULT_MEDIA_PASSWORD, challenge.get("encrypt_type"))
        self._keys = derive_stream_key_iv(key_exchange, transformed)
        if self._keys is None:
            logger.warning("camera %s offered an unsupported Key-Exchange user; encrypted parts cannot be read",
                           self._host)

    # ------------------------------------------------------------------ streaming
    def send_json(self, message: dict, *, headers: dict[str, str] | None = None) -> int:
        """Send ``message`` as an application/json client part; adds ``seq`` (incrementing from 1) if
        missing and returns the seq used. ``headers`` are extra part headers (e.g. the flow-control
        ack headers); once a session_id is known ``X-Session-Id`` is added automatically if absent.
        """
        msg = dict(message)
        with self._send_lock:
            if "seq" not in msg:
                self._seq += 1
                msg["seq"] = self._seq
        part_headers = {str(k): str(v) for k, v in (headers or {}).items()}
        if self._session_id and not any(k.lower() == "x-session-id" for k in part_headers):
            part_headers["X-Session-Id"] = self._session_id
        try:
            self._sendall(encode_client_part(_compact_json(msg), headers=part_headers))
        except socket.timeout:
            raise TapoConnectionError("timed out sending to the media port") from None
        except OSError as exc:
            raise TapoConnectionError(f"media connection lost: {exc.strerror or type(exc).__name__}") from None
        return msg["seq"]

    def send_ack(self, received_seq: int) -> None:
        """Send the flow-control ack exactly as the app does [spec:vod.stream.flow_control_ack]
        (:func:`build_ack_part`, no ``seq`` in the body). Needs a known ``session_id``."""
        if not self._session_id:
            raise TapoProtocolError("cannot ack before the camera announced a session id")
        try:
            self._sendall(build_ack_part(received_seq, self._session_id))
        except socket.timeout:
            raise TapoConnectionError("timed out sending to the media port") from None
        except OSError as exc:
            raise TapoConnectionError(f"media connection lost: {exc.strerror or type(exc).__name__}") from None

    def _sendall(self, data: bytes) -> None:
        sock = self._sock
        if sock is None:
            raise TapoConnectionError("media session is not connected")
        with self._send_lock:
            sock.sendall(data)

    def parts(self) -> Iterator[MediaPart]:
        """Yield device parts until the connection closes; bodies with ``X-If-Encrypt: 1`` are decrypted."""
        data = bytes(self._rx)
        self._rx.clear()
        while True:
            for part in self._reader.feed(data):
                yield self._decode(part)
            sock = self._sock
            if sock is None:
                return
            try:
                data = sock.recv(self._RECV_SIZE)
            except socket.timeout:
                if self._closed:
                    return
                raise TapoConnectionError(f"no data from media port for {self._timeout:g} s") from None
            except OSError as exc:
                if self._closed:
                    return
                raise TapoConnectionError(f"media connection lost: {exc.strerror or type(exc).__name__}") from None
            if not data:
                return

    def _decode(self, part: MediaPart) -> MediaPart:
        if part.headers.get("X-If-Encrypt") == "1":
            if self._keys is None:
                raise TapoProtocolError("encrypted media part but no stream key was negotiated")
            part = MediaPart(part.headers, decrypt_part(part.body, *self._keys))
        if self._session_id is None and "json" in part.headers.get("Content-Type", ""):
            self._remember_session_id(part.body)
        return part

    def _remember_session_id(self, body: bytes) -> None:
        try:
            msg = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return
        result = msg.get("result") if isinstance(msg, dict) else None
        if isinstance(result, dict) and result.get("session_id"):
            self._session_id = str(result["session_id"])

    # ------------------------------------------------------------------ lifetime
    def _close_socket(self) -> None:
        sock, self._sock = self._sock, None
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()

    def close(self) -> None:
        """Close the socket. Idempotent."""
        self._closed = True
        self._close_socket()

    def __enter__(self) -> "MediaSession":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
