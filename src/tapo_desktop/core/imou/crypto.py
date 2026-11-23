"""Decryption of the stream URL from ``createVideoDownloadStreamUrl``.

The reply's ``url`` is Base64 of ``IV (12 bytes) + AES-256-GCM ciphertext + tag (16 bytes)``; the key is
``SHA-256(UTF-8(token + "ENCRYPTKEY"))`` with the same token that was sent in the request. The plaintext is the
RTSP URL, which is a secret (anyone holding it can watch the camera).
"""
from __future__ import annotations

import base64
import binascii
import hashlib

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from tapo_desktop.core.imou.errors import ImouProtocolError

_IV = 12
_TAG = 16


def decrypt_stream_url(encrypted_b64: str, token: str) -> str:
    try:
        raw = base64.b64decode(encrypted_b64, validate=True)
        if len(raw) <= _IV + _TAG:
            raise ValueError("too short")
        key = hashlib.sha256((token + "ENCRYPTKEY").encode("utf-8")).digest()
        return AESGCM(key).decrypt(raw[:_IV], raw[_IV:], None).decode("utf-8")
    except (binascii.Error, ValueError, InvalidTag, UnicodeDecodeError, TypeError):
        raise ImouProtocolError("the stream address could not be decrypted") from None
