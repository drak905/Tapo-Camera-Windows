"""Secure-login (encrypt_type "3") primitives and securePassthrough cipher.

Spec: [spec:auth.secure.*], golden vector [spec:auth.golden_vector.v3].
All hex digests are UPPERCASE unless noted. Strings are UTF-8.
"""
from __future__ import annotations

from dataclasses import dataclass


def md5_upper(s: str) -> str:
    """MD5 over UTF-8 bytes of ``s`` as UPPERCASE hex (legacy login password hash)."""
    raise NotImplementedError


def sha256_upper(s: str) -> str:
    """SHA-256 over UTF-8 bytes of ``s`` as UPPERCASE hex (``SHA256U`` in the spec)."""
    raise NotImplementedError


def generate_cnonce() -> str:
    """Return a fresh client nonce: 16 uppercase hex chars (8 random bytes from ``secrets``)."""
    raise NotImplementedError


def expected_device_confirm(password: str, cnonce: str, nonce: str) -> str:
    """``SHA256U(cnonce + SHA256U(password) + nonce) + nonce + cnonce`` [spec:auth.secure.device_confirm]."""
    raise NotImplementedError


def verify_device_confirm(password: str, cnonce: str, nonce: str, device_confirm: str) -> bool:
    """True iff ``device_confirm`` equals :func:`expected_device_confirm` (constant-time compare)."""
    raise NotImplementedError


def digest_passwd(password: str, cnonce: str, nonce: str) -> str:
    """``SHA256U(SHA256U(password) + cnonce + nonce) + cnonce + nonce`` [spec:auth.secure.digest_passwd]."""
    raise NotImplementedError


@dataclass(frozen=True)
class SessionKeys:
    """AES-128 session key (``lsk``) and fixed CBC IV (``ivb``), 16 bytes each."""

    lsk: bytes
    ivb: bytes


def derive_session_keys(password: str, cnonce: str, nonce: str) -> SessionKeys:
    """Derive lsk/ivb per [spec:auth.secure.session_keys]."""
    raise NotImplementedError


def compute_tag(password: str, cnonce: str, body: str, seq: int) -> str:
    """``tapo_tag = SHA256U(SHA256U(SHA256U(password) + cnonce) + body + str(seq))`` [spec:auth.secure.tag]."""
    raise NotImplementedError


class SecureSessionCipher:
    """AES-128-CBC / PKCS7 with the session lsk/ivb; base64 (standard alphabet, padded, no newlines)."""

    def __init__(self, keys: SessionKeys):
        raise NotImplementedError

    def encrypt(self, plaintext: str) -> str:
        """Encrypt UTF-8 ``plaintext`` and return base64 text."""
        raise NotImplementedError

    def decrypt(self, b64: str) -> str:
        """Decrypt base64 ciphertext to text. Raises TapoProtocolError on bad base64/length/padding/UTF-8."""
        raise NotImplementedError


def wrap_secure_passthrough(cipher: SecureSessionCipher, inner: dict) -> str:
    """Serialize ``inner`` compactly, encrypt it and return the exact outer body JSON string
    ``{"method":"securePassthrough","params":{"request":"<b64>"}}`` [spec:auth.secure.passthrough_envelope]."""
    raise NotImplementedError
