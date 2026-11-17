"""Secure-login (encrypt_type "3") primitives and securePassthrough cipher.

Spec: [spec:auth.secure.*], golden vector [spec:auth.golden_vector.v3].
All hex digests are UPPERCASE unless noted. Strings are UTF-8.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import secrets
from dataclasses import dataclass
from hmac import compare_digest

from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from tapo_desktop.core.errors import TapoProtocolError

__all__ = [
    "SecureSessionCipher", "SessionKeys", "compute_tag", "derive_session_keys", "digest_passwd",
    "dumps_compact", "expected_device_confirm", "generate_cnonce", "md5_upper", "sha256_upper",
    "verify_device_confirm", "wrap_secure_passthrough",
]

_BLOCK_BITS = 128
_BLOCK_BYTES = _BLOCK_BITS // 8


def dumps_compact(obj: object) -> str:
    """Wire JSON: compact separators, no ASCII escaping (Gson without HTML escaping)."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def md5_upper(s: str) -> str:
    """MD5 over UTF-8 bytes of ``s`` as UPPERCASE hex (legacy login password hash)."""
    return hashlib.md5(s.encode("utf-8")).hexdigest().upper()


def sha256_upper(s: str) -> str:
    """SHA-256 over UTF-8 bytes of ``s`` as UPPERCASE hex (``SHA256U`` in the spec)."""
    return hashlib.sha256(s.encode("utf-8")).hexdigest().upper()


def generate_cnonce() -> str:
    """Return a fresh client nonce: 16 uppercase hex chars (8 random bytes from ``secrets``)."""
    return secrets.token_hex(8).upper()


def _first_hash(password: str, cnonce: str, nonce: str) -> str:
    return sha256_upper(cnonce + sha256_upper(password) + nonce)


def expected_device_confirm(password: str, cnonce: str, nonce: str) -> str:
    """``SHA256U(cnonce + SHA256U(password) + nonce) + nonce + cnonce`` [spec:auth.secure.device_confirm]."""
    return _first_hash(password, cnonce, nonce) + nonce + cnonce


def verify_device_confirm(password: str, cnonce: str, nonce: str, device_confirm: str) -> bool:
    """True iff ``device_confirm`` equals :func:`expected_device_confirm` (constant-time compare)."""
    expected = expected_device_confirm(password, cnonce, nonce).encode("utf-8")
    return compare_digest(expected, str(device_confirm).encode("utf-8"))


def digest_passwd(password: str, cnonce: str, nonce: str) -> str:
    """``SHA256U(SHA256U(password) + cnonce + nonce) + cnonce + nonce`` [spec:auth.secure.digest_passwd]."""
    return sha256_upper(sha256_upper(password) + cnonce + nonce) + cnonce + nonce


@dataclass(frozen=True)
class SessionKeys:
    """AES-128 session key (``lsk``) and fixed CBC IV (``ivb``), 16 bytes each."""

    lsk: bytes
    ivb: bytes

    def __repr__(self) -> str:  # never leak key material
        return "SessionKeys(lsk=***, ivb=***)"


def derive_session_keys(password: str, cnonce: str, nonce: str) -> SessionKeys:
    """Derive lsk/ivb per [spec:auth.secure.session_keys]."""
    first = _first_hash(password, cnonce, nonce)

    def key(label: str) -> bytes:
        return hashlib.sha256((label + cnonce + nonce + first).encode("utf-8")).digest()[:_BLOCK_BYTES]

    return SessionKeys(lsk=key("lsk"), ivb=key("ivb"))


def compute_tag(password: str, cnonce: str, body: str, seq: int) -> str:
    """``tapo_tag = SHA256U(SHA256U(SHA256U(password) + cnonce) + body + str(seq))`` [spec:auth.secure.tag]."""
    return sha256_upper(sha256_upper(sha256_upper(password) + cnonce) + body + str(seq))


class SecureSessionCipher:
    """AES-128-CBC / PKCS7 with the session lsk/ivb; base64 (standard alphabet, padded, no newlines)."""

    def __init__(self, keys: SessionKeys):
        self._keys = keys

    def __repr__(self) -> str:
        return "SecureSessionCipher(keys=***)"

    def _cipher(self) -> Cipher:
        return Cipher(algorithms.AES(self._keys.lsk), modes.CBC(self._keys.ivb))

    def encrypt(self, plaintext: str) -> str:
        """Encrypt UTF-8 ``plaintext`` and return base64 text."""
        padder = sym_padding.PKCS7(_BLOCK_BITS).padder()
        data = padder.update(plaintext.encode("utf-8")) + padder.finalize()
        enc = self._cipher().encryptor()
        return base64.b64encode(enc.update(data) + enc.finalize()).decode("ascii")

    def decrypt(self, b64: str) -> str:
        """Decrypt base64 ciphertext to text. Raises TapoProtocolError on bad base64/length/padding/UTF-8."""
        try:
            raw = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError, TypeError) as exc:
            raise TapoProtocolError("encrypted response is not valid base64") from exc
        if not raw or len(raw) % _BLOCK_BYTES:
            raise TapoProtocolError("encrypted response length is not a positive multiple of the AES block")
        dec = self._cipher().decryptor()
        padded = dec.update(raw) + dec.finalize()
        try:
            unpadder = sym_padding.PKCS7(_BLOCK_BITS).unpadder()
            data = unpadder.update(padded) + unpadder.finalize()
        except ValueError as exc:
            raise TapoProtocolError("encrypted response has invalid padding") from exc
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TapoProtocolError("decrypted response is not valid UTF-8") from exc


def wrap_secure_passthrough(cipher: SecureSessionCipher, inner: dict) -> str:
    """Serialize ``inner`` compactly, encrypt it and return the exact outer body JSON string
    ``{"method":"securePassthrough","params":{"request":"<b64>"}}`` [spec:auth.secure.passthrough_envelope]."""
    request = cipher.encrypt(dumps_compact(inner))
    return dumps_compact({"method": "securePassthrough", "params": {"request": request}})
