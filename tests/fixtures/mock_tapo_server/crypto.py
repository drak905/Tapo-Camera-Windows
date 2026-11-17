"""Camera-side crypto primitives for the mock (independent re-implementation of the spec).

[spec:auth.secure.*], [spec:stream.local8800.encryption.md5]. Deliberately does NOT import
``tapo_desktop`` so the mock stays an independent reference implementation.
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def md5_upper(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest().upper()


def sha256_upper(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest().upper()


def first_hash(password: str, cnonce: str, nonce: str) -> str:
    return sha256_upper(cnonce + sha256_upper(password) + nonce)


def device_confirm(password: str, cnonce: str, nonce: str) -> str:
    """[spec:auth.secure.device_confirm] SHA256U(cnonce + SHA256U(pwd) + nonce) + nonce + cnonce."""
    return first_hash(password, cnonce, nonce) + nonce + cnonce


def digest_passwd(password: str, cnonce: str, nonce: str) -> str:
    """[spec:auth.secure.digest_passwd] SHA256U(SHA256U(pwd) + cnonce + nonce) + cnonce + nonce."""
    return sha256_upper(sha256_upper(password) + cnonce + nonce) + cnonce + nonce


def session_keys(password: str, cnonce: str, nonce: str) -> tuple[bytes, bytes]:
    """[spec:auth.secure.session_keys] -> (lsk, ivb), 16 raw bytes each."""
    fh = first_hash(password, cnonce, nonce)
    lsk = hashlib.sha256(("lsk" + cnonce + nonce + fh).encode("utf-8")).digest()[:16]
    ivb = hashlib.sha256(("ivb" + cnonce + nonce + fh).encode("utf-8")).digest()[:16]
    return lsk, ivb


def tag_prefix(password: str, cnonce: str) -> str:
    return sha256_upper(sha256_upper(password) + cnonce)


def tapo_tag(password: str, cnonce: str, body: str, seq: int) -> str:
    """[spec:auth.secure.tag] SHA256U(SHA256U(SHA256U(pwd) + cnonce) + body + str(seq))."""
    return sha256_upper(tag_prefix(password, cnonce) + body + str(seq))


def aes_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    padder = padding.PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return enc.update(padded) + enc.finalize()


def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """Raises ValueError on bad length / padding."""
    if not data or len(data) % 16:
        raise ValueError("ciphertext length is not a multiple of 16")
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = dec.update(data) + dec.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def b64_encrypt(key: bytes, iv: bytes, text: str) -> str:
    return base64.b64encode(aes_cbc_encrypt(key, iv, text.encode("utf-8"))).decode("ascii")


def b64_decrypt(key: bytes, iv: bytes, b64: str) -> str:
    """Raises ValueError on bad base64 / padding / UTF-8."""
    raw = base64.b64decode(b64.encode("ascii"), validate=True)
    return aes_cbc_decrypt(key, iv, raw).decode("utf-8")


def media_password(cloud_password: str, encrypt_v3: bool) -> str:
    """[spec:stream.local8800.auth.digest] password_transform."""
    return sha256_upper(cloud_password) if encrypt_v3 else md5_upper(cloud_password)


def media_keys(nonce: str, password_t: str, username: str = "admin") -> tuple[bytes, bytes]:
    """[spec:stream.local8800.encryption.md5] key=MD5(nonce:pwd'), iv=MD5(username:nonce)."""
    key = hashlib.md5((nonce + ":" + password_t).encode("utf-8")).digest()
    iv = hashlib.md5((username + ":" + nonce).encode("utf-8")).digest()
    return key, iv


def compact_json(obj) -> str:
    import json

    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
