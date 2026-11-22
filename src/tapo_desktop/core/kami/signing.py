"""Kami Home request signing (pure functions; no vendor secret lives in this file).

* Login password: ``base64(HMAC-SHA256(app_key, password))``, one line [spec:kami.auth.password_hash].
  The app key is a vendor constant that is loaded at run time (see :mod:`tapo_desktop.core.kami.app_key`).
* Every call after sign-in: ``hmac = base64(HMAC-SHA1(token + "&" + token_secret, "k1=v1&k2=v2"))`` over the
  parameters in send order, values NOT url-encoded, without the ``hmac`` parameter itself
  [spec:kami.auth.request_signing].
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Sequence
from urllib.parse import quote

Pairs = Sequence[tuple[str, str]]


def password_hash(password: str, app_key: str) -> str:
    """The value of the ``password`` query parameter of ``/v4/users/login``."""
    digest = hmac.new(app_key.encode("utf-8"), password.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def request_hmac(pairs: Pairs, token: str, token_secret: str) -> str:
    """Signature of ``pairs`` (in order, ``hmac`` excluded) for a signed call."""
    message = "&".join(f"{k}={v}" for k, v in pairs if k != "hmac")
    key = f"{token}&{token_secret}".encode("utf-8")
    return base64.b64encode(hmac.new(key, message.encode("utf-8"), hashlib.sha1).digest()).decode("ascii")


def signed_pairs(pairs: Pairs, token: str, token_secret: str) -> list[tuple[str, str]]:
    """``pairs`` followed by their ``hmac``."""
    return [*pairs, ("hmac", request_hmac(pairs, token, token_secret))]


def encode_query(pairs: Pairs) -> str:
    """Percent-encode ``pairs`` for the URL query, keeping their order (``+`` becomes ``%2B``)."""
    return "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in pairs)
