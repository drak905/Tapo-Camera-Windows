"""Local media connection (POST /stream on :8800)  [spec:stream.local8800.*, vod.stream.*].

Wave 2 stub: signatures and docstrings only (see docs/CONTRACT_MEDIA.md).

Pure helpers (module level, no I/O) are part of the public contract so they can be unit tested:
``parse_www_authenticate``, ``transform_password``, ``generate_cnonce``, ``digest_response``,
``digest_authorization``, ``parse_key_exchange``, ``derive_stream_key_iv``, ``decrypt_part``,
``encode_client_part``, ``build_ack_part``, ``MultipartReader``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

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

CLIENT_BOUNDARY = "--client-stream-boundary--"  # parts start with "--" + CLIENT_BOUNDARY
DEVICE_BOUNDARY = "--device-stream-boundary--"  # parts start with "--" + DEVICE_BOUNDARY
DEFAULT_MEDIA_USERNAME = "admin"  # [spec:stream.local8800.default_credentials]
DEFAULT_MEDIA_PASSWORD = "TPL075526460603"


@dataclass
class MediaPart:
    headers: dict[str, str]  # header names exactly as received, values stripped
    body: bytes  # MediaSession.parts(): already decrypted; MultipartReader: raw


# --------------------------------------------------------------------------- pure helpers


def parse_www_authenticate(value: str) -> dict[str, str]:
    """Parse a ``WWW-Authenticate: Digest k=v, k="v", ...`` value into a dict.

    Keys are lower-cased, quotes stripped, commas inside quoted values preserved.
    Raises TapoProtocolError if the scheme is not Digest or there is no nonce.
    """
    raise NotImplementedError


def transform_password(password: str, encrypt_type: str | int | None = None) -> str:
    """[spec:stream.local8800.auth.digest] UPPERCASE_HEX(SHA256(pw)) if encrypt_type == 3 else UPPERCASE_HEX(MD5(pw))."""
    raise NotImplementedError


def generate_cnonce() -> str:
    """16 chars alternating random lowercase letter / digit (``a1b2...``)."""
    raise NotImplementedError


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
    raise NotImplementedError


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
    raise NotImplementedError


def parse_key_exchange(value: str) -> dict[str, str]:
    """Parse ``Key-Exchange: username="admin" nonce="<hex>" [cipher=..] ...`` (space separated k=v, quotes stripped).

    Raises TapoProtocolError when ``nonce`` is missing.
    """
    raise NotImplementedError


def derive_stream_key_iv(key_exchange: dict[str, str], transformed_password: str) -> tuple[bytes, bytes] | None:
    """[spec:stream.local8800.encryption.md5] Return (key, iv), 16 raw bytes each, or None.

    username "admin" -> secret = transformed_password; "none" -> secret = DEFAULT_MEDIA_PASSWORD;
    any other username -> None (no decryption possible).
    key = MD5(nonce + ":" + secret), iv = MD5(username + ":" + nonce).
    """
    raise NotImplementedError


def decrypt_part(body: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-128-CBC / PKCS7 decrypt one part (fresh cipher per part). Bad length/padding -> TapoProtocolError."""
    raise NotImplementedError


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
    raise NotImplementedError


def build_ack_part(received_seq: int, session_id: str) -> bytes:
    """[spec:vod.stream.flow_control_ack] ``encode_client_part`` of the compact JSON
    ``{"type":"notification","params":{"event_type":"stream_sequence"}}`` with headers
    ``X-Data-Received: <received_seq>`` then ``X-Session-Id: <session_id>``.
    """
    raise NotImplementedError


class MultipartReader:
    """Incremental parser of the device multipart stream (bodies NOT decrypted).

    A part starts at a line equal to ``"--" + boundary``; headers follow until a blank line, then
    exactly Content-Length body bytes. CRLFs / junk lines between parts and the closing
    ``"--" + boundary + "--"`` line are ignored. Missing/invalid/negative Content-Length or a
    body larger than ``max_part_size`` raises TapoProtocolError (never any other exception).
    """

    def __init__(self, boundary: str = DEVICE_BOUNDARY, max_part_size: int = 16 * 1024 * 1024):
        raise NotImplementedError

    def feed(self, data: bytes) -> list[MediaPart]:
        """Add bytes (arbitrary chunking) and return every part completed by them, in order."""
        raise NotImplementedError

    @property
    def buffered(self) -> int:
        """Number of bytes held that do not yet form a complete part."""
        raise NotImplementedError


# --------------------------------------------------------------------------- connection


class MediaSession:
    """One persistent ``POST /stream`` connection to the camera media port."""

    def __init__(
        self,
        host: str,
        password: str,
        *,
        port: int = 8800,
        username: str = DEFAULT_MEDIA_USERNAME,
        timeout: float = 10.0,
    ):
        """``password`` = cloud password (plaintext)."""
        raise NotImplementedError

    def connect(self) -> None:
        """Digest handshake [spec:stream.local8800.handshake] and Key-Exchange parsing.

        401 after the authorized retry -> TapoAuthError; HTTP 503 / -52407 -> StreamLimitError;
        socket errors / timeouts -> TapoConnectionError; malformed response -> TapoProtocolError.
        """
        raise NotImplementedError

    def send_json(self, message: dict, *, headers: dict[str, str] | None = None) -> int:
        """Send ``message`` as an application/json client part; adds ``seq`` (incrementing from 1) if
        missing and returns the seq used. ``headers`` are extra part headers (e.g. the flow-control
        ack headers); once a session_id is known ``X-Session-Id`` is added automatically if absent.
        """
        raise NotImplementedError

    def parts(self) -> Iterator[MediaPart]:
        """Yield device parts until the connection closes; bodies with ``X-If-Encrypt: 1`` are decrypted."""
        raise NotImplementedError

    def close(self) -> None:
        """Close the socket. Idempotent."""
        raise NotImplementedError

    def __enter__(self) -> "MediaSession":
        raise NotImplementedError

    def __exit__(self, exc_type, exc, tb) -> None:
        raise NotImplementedError
