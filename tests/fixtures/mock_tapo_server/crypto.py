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


def media_hkdf_keys(nonce: str, password_t: str, salt: str) -> tuple[bytes, bytes]:
    """[spec:stream.local8800.encryption.hkdf] (aes_key, hmac_key) = HKDF-SHA256(nonce:pwd', salt, info)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    ikm = (nonce + ":" + password_t).encode("utf-8")

    def _k(info: bytes) -> bytes:
        return HKDF(algorithm=hashes.SHA256(), length=16, salt=salt.encode("utf-8"), info=info).derive(ikm)

    return _k(b"stream_hkdf_aes_key"), _k(b"stream_hkdf_hmac_key")


def media_hkdf_encrypt(aes_key: bytes, hmac_key: bytes, data: bytes, *, corrupt_hmac: bool = False
                       ) -> tuple[bytes, dict[str, str]]:
    """Encrypt one part with a fresh random IV; returns (ciphertext, {X-Nonce, X-Data-Hmac})."""
    import hmac as _hmac
    import os

    iv = os.urandom(16)
    ct = aes_cbc_encrypt(aes_key, iv, data)
    mac = _hmac.new(hmac_key, ct, hashlib.sha256).digest()
    if corrupt_hmac:
        mac = bytes(b ^ 0xFF for b in mac)
    return ct, {"X-Nonce": iv.hex(), "X-Data-Hmac": base64.b64encode(mac).decode("ascii")}


def compact_json(obj) -> str:
    import json

    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
