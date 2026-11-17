"""TP-Link cloud V2 request signing [spec:cloud.signing] [spec:cloud.signing.keys].

Every account / device / MFA request carries two headers computed exactly like the app's OkHttp
interceptor ``m7.r``::

    Content-MD5     = Base64(MD5(body))            # an empty body is sent and hashed as b"{}"
    X-Authorization = "Timestamp=9999999999, Nonce=<uuid4>, AccessKey=<AK>, Signature=<sig>"
    sig             = hex(HMAC-SHA1(SecretKey, content_md5 + "\\n" + "9999999999" + "\\n"
                                              + nonce + "\\n" + url_path))

``url_path`` is the encoded path only (no host, no query). The timestamp is the constant the app
sends, not the current time. AccessKey / SecretKey are the production APK constants.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import uuid

ACCESS_KEY = "4d11b6b9d5ea4d19a829adbb9714b057"
SECRET_KEY = "6ed7d97f3e73467f8a5bab90b577ba4c"
TIMESTAMP = "9999999999"


def content_md5(body: bytes) -> str:
    """Base64(MD5(body)); ``b""`` hashes as ``b"{}"`` like the app."""
    return base64.b64encode(hashlib.md5(body or b"{}").digest()).decode("ascii")


def sign_headers(body: bytes, path: str, *, nonce: str | None = None, access_key: str = ACCESS_KEY,
                 secret_key: str = SECRET_KEY) -> dict[str, str]:
    """``{"Content-MD5": ..., "X-Authorization": ...}`` for a request to ``path`` with ``body``."""
    md5 = content_md5(body)
    nonce = nonce or str(uuid.uuid4())
    to_sign = f"{md5}\n{TIMESTAMP}\n{nonce}\n{path}".encode("utf-8")
    signature = hmac.new(secret_key.encode("utf-8"), to_sign, hashlib.sha1).hexdigest()
    return {
        "Content-MD5": md5,
        "X-Authorization": f"Timestamp={TIMESTAMP}, Nonce={nonce}, AccessKey={access_key}, Signature={signature}",
    }
