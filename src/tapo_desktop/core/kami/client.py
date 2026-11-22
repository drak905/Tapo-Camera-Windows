"""Kami Home cloud client: sign in and list cameras (pure Python, blocking, one request per call).

* ``login``: ``GET {gateway}/v4/users/login`` with the HMAC-hashed password in the query [spec:kami.auth.login_request].
* ``list_devices``: ``GET {gateway}/v5/devices/list`` signed with the session token [spec:kami.cloud.device_list].

Safety rules (plan review finding 10): each method sends exactly one request, never retries and never logs a URL,
query string, password, token or reply body. The password is in the URL query of a GET (that is how the vendor app
does it), so URLs are kept out of every log line and exception message here. ``base_url`` exists for tests (a local
mock); the app never sets it. Mock tests prove agreement with the spec, not with the real server.
"""
from __future__ import annotations

import http.client
import json
import logging
import ssl
from typing import Any
from urllib.parse import urlsplit

from tapo_desktop.core.kami.dtos import (
    KamiConnectionError, KamiKeyMissingError, KamiProtocolError, KamiServerError, KamiSession, KamiSessionExpired,
    parse_devices, parse_login,
)
from tapo_desktop.core.kami.models import KamiDevice
from tapo_desktop.core.kami.regions import gateway_url
from tapo_desktop.core.kami.signing import encode_query, password_hash, signed_pairs

log = logging.getLogger(__name__)

#: Version of the Kami Home app whose protocol this follows (header ``x-xiaoyi-appVersion``) [spec:kami.auth.required_headers].
APP_VERSION = "5.0.2"
USER_AGENT = "TapoDesktop-Kami/0.1"
_MAX_BODY = 4 * 1024 * 1024


class KamiClient:
    def __init__(self, region: str, country_code: str, app_key: str | None = None, *, base_url: str | None = None,
                 timeout: float = 15.0):
        self._region = region
        self._country = country_code.strip().upper()
        self._app_key = app_key or None
        self._base = base_url or gateway_url(region)
        self._timeout = timeout

    def __repr__(self) -> str:
        return f"KamiClient(region={self._region!r})"

    # ------------------------------------------------------------------ API
    def login(self, email: str, password: str) -> KamiSession:
        """One sign-in request. Raises ``KamiKeyMissingError`` before any network call if the key is absent."""
        reply = self._get("/v4/users/login", self._login_pairs(email, password))
        session = parse_login(reply, region=self._region, country=self._country, email=email)
        log.info("kami sign-in accepted (region %s)", self._region)
        return session

    def login_raw(self, email: str, password: str) -> tuple[list[tuple[str, str]], int, list[tuple[str, str]], bytes]:
        """One sign-in request, unparsed: ``(sent pairs, HTTP status, reply headers, body)``. For the owner capture tool."""
        pairs = self._login_pairs(email, password)
        status, headers, body = self._fetch("/v4/users/login", pairs)
        return pairs, status, headers, body

    def _login_pairs(self, email: str, password: str) -> list[tuple[str, str]]:
        if not self._app_key:
            raise KamiKeyMissingError("the Kami password key is not installed")
        if self._region == "cn":  # the China build sends email= or mobile= [spec:kami.auth.login_request]
            who = ("email", email) if "@" in email else ("mobile", email)
        else:
            who = ("account", email)
        return [("seq", "1"), who, ("password", password_hash(password, self._app_key)),
                ("dev_name", "Tapo Desktop"), ("dev_type", "Windows PC"), ("dev_os_version", "Windows")]

    def list_devices(self, session: KamiSession) -> list[KamiDevice]:
        """One signed request for the account's cameras (owned and shared)."""
        pairs = signed_pairs([("seq", "1"), ("userid", session.userid)], session.token, session.token_secret)
        devices = parse_devices(self._get("/v5/devices/list", pairs, signed=True), self._region)
        log.info("kami device list: %d camera(s)", len(devices))
        return devices

    # ------------------------------------------------------------------ transport
    def _get(self, path: str, pairs: list[tuple[str, str]], *, signed: bool = False) -> dict[str, Any]:
        status, _headers, body = self._fetch(path, pairs)
        if status != 200:
            if signed and status in (401, 403):
                raise KamiSessionExpired("the Kami session is no longer valid")
            raise KamiServerError(f"Kami server answered HTTP {status}", status=status)
        if len(body) > _MAX_BODY:
            raise KamiProtocolError("Kami reply is too large")
        try:
            reply = json.loads(body.decode("utf-8"))
        except ValueError:
            raise KamiProtocolError("Kami reply is not JSON") from None
        if not isinstance(reply, dict):
            raise KamiProtocolError("Kami reply is not a JSON object")
        return reply

    def _fetch(self, path: str, pairs: list[tuple[str, str]]) -> tuple[int, list[tuple[str, str]], bytes]:
        base = urlsplit(self._base)
        headers = {"x-kamihome-appType": "ANDROID", "x-kamihome-packageType": "RELEASE", "User-Agent": USER_AGENT,
                   "x-xiaoyi-appCountryCode": self._country, "x-xiaoyi-appVersion": APP_VERSION}
        try:
            if base.scheme == "https":
                conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                    base.hostname, base.port or 443, timeout=self._timeout, context=ssl.create_default_context())
            else:
                conn = http.client.HTTPConnection(base.hostname, base.port or 80, timeout=self._timeout)
            try:
                conn.request("GET", f"{path}?{encode_query(pairs)}", headers=headers)
                response = conn.getresponse()
                status, headers, body = response.status, response.getheaders(), response.read(_MAX_BODY + 1)
            finally:
                conn.close()
        except (OSError, http.client.HTTPException):
            # no ``from exc``: the original message may carry the host name
            raise KamiConnectionError("Cannot reach the Kami servers.") from None
        return status, headers, body
