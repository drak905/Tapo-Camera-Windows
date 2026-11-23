"""Request signing of the iMou Open Platform.

``password = lowercase hex SHA-256(appSecret)``; ``sign = Base64(HMAC-SHA256(key=password,
"time:<time>,nonce:<nonce>,appSecret:<appSecret>"))``. The time must be within 5 minutes of the server, the nonce
may not repeat inside 5 minutes. The public doc vector (secret ``test123456789test123456789``, time 1706511734,
nonce ``f5a1ae2d-c09c-4d39-a744-83a5c2c653c2``) gives ``xjhCQBoJ9hRDsCjyDcHjtDNzRZ3ZJezcawsfWeiaoxU=``.
The secret itself is never part of a request body.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json


def password_key(app_secret: str) -> str:
    return hashlib.sha256(app_secret.encode("utf-8")).hexdigest().lower()


def sign(app_secret: str, time_s: int, nonce: str) -> str:
    template = f"time:{time_s},nonce:{nonce},appSecret:{app_secret}"
    digest = hmac.new(password_key(app_secret).encode("utf-8"), template.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def build_request(app_id: str, app_secret: str, params: dict, *, time_s: int, nonce: str, request_id: str) -> dict:
    return {"system": {"ver": "1.0", "appId": app_id, "sign": sign(app_secret, time_s, nonce), "time": time_s,
                       "nonce": nonce},
            "id": request_id, "params": params}


def encode(body: dict) -> bytes:
    """Compact UTF-8 JSON, as the vendor samples send it."""
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
