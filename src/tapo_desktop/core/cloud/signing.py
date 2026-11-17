"""Cloud request signing [spec:cloud.signing] (stub)."""
from __future__ import annotations

ACCESS_KEY = "4d11b6b9d5ea4d19a829adbb9714b057"
SECRET_KEY = "6ed7d97f3e73467f8a5bab90b577ba4c"


def content_md5(body: bytes) -> str:
    raise NotImplementedError


def sign_headers(body: bytes, path: str, *, nonce: str | None = None, access_key: str = ACCESS_KEY,
                 secret_key: str = SECRET_KEY) -> dict[str, str]:
    raise NotImplementedError
