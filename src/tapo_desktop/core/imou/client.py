"""The iMou Open Platform client: a strict allowlist of read-only calls.

``ALLOWED_PATHS`` is the complete list of requests this class can build. ``_call`` refuses every other path before
anything is signed or sent; the guard test lists them. There is no call that adds, removes or changes a camera.

Rules (from the study, section 7):

* one ``accessToken`` call per ~3 days, kept in memory only (never logged, never written to disk);
* ``TK1002`` -> one new token and one retry of that call, no more;
* ``SN1001``, ``DV1007`` and every other vendor error -> no retry; quota codes stop all calls through the
  :class:`~tapo_desktop.core.imou.guard.ImouGuard`;
* no polling anywhere: the device list is read when the user connects or refreshes;
* the host comes from the fixed data-center list; the app secret is only used to sign, never sent.

Nothing is logged except method names and vendor codes: never a body, token, secret or live address.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from typing import Any

from tapo_desktop.core.imou import regions, signing
from tapo_desktop.core.imou.crypto import decrypt_stream_url
from tapo_desktop.core.imou.errors import (
    ImouBlockedError, ImouConnectionError, ImouError, ImouNotAllowedError, ImouProtocolError, ImouTokenExpiredError,
    error_for,
)
from tapo_desktop.core.imou.guard import ImouGuard
from tapo_desktop.core.imou.models import ImouDevice, ImouLive

log = logging.getLogger(__name__)

_TOKEN = "/openapi/accessToken"
_LIST = "/openapi/listDeviceDetailsByPage"
_STREAM_URL = "/openapi/createVideoDownloadStreamUrl"
_BIND_LIVE = "/openapi/bindDeviceLive"
_LIVE_LIST = "/openapi/liveList"
_UNBIND_LIVE = "/openapi/unbindLive"
ALLOWED_PATHS = frozenset({_TOKEN, _LIST, _STREAM_URL, _BIND_LIVE, _LIVE_LIST, _UNBIND_LIVE})

PAGE_SIZE = 50
MAX_PAGES = 4  # shortcut: at most 200 devices in v1; raise it if someone has more cameras than that
_TOKEN_MARGIN_S = 600.0
_TIMEOUT_S = 20.0

Transport = Callable[[str, bytes, float], "tuple[int, bytes]"]


class UrllibTransport:
    """HTTPS POST through ``urllib`` to one of the allowed hosts only (checked here as well as in the client)."""

    def post(self, url: str, body: bytes, timeout: float = _TIMEOUT_S) -> tuple[int, bytes]:
        if not regions.is_allowed_url(url):
            raise ImouNotAllowedError("this address is not an iMou data center")
        request = urllib.request.Request(url, data=body, method="POST",
                                         headers={"Content-Type": "application/json;charset=UTF-8"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return int(response.getcode() or response.status), response.read()
        except urllib.error.HTTPError as exc:
            return int(exc.code), b""
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ImouConnectionError(f"cannot reach the iMou servers ({type(exc).__name__})") from None


class ImouClient:
    """Blocking client; call it from a worker thread. Not thread-safe for one token refresh at a time (the
    account manager runs its calls on one worker; a live start is one call chain per camera)."""

    def __init__(self, app_id: str, app_secret: str, data_center: str, *, transport: Transport | None = None,
                 clock: Callable[[], float] = time.time, guard: ImouGuard | None = None,
                 nonce_factory: Callable[[], str] | None = None, timeout: float = _TIMEOUT_S):
        self._base = regions.base_url(data_center)  # ValueError for an unknown data center
        self._app_id = app_id
        self._secret = app_secret
        self._post = transport or UrllibTransport().post
        self._clock = clock
        self._guard = guard
        self._nonce = nonce_factory or (lambda: str(uuid.uuid4()))
        self._timeout = timeout
        self._token: str | None = None
        self._token_until = 0.0

    def __repr__(self) -> str:
        return f"ImouClient(data_center={self._base.split('-')[1].split('.')[0]!r})"

    # ================================================================== allowed calls
    def access_token(self) -> str:
        """The administrator token; one ``accessToken`` call when none is held or it is about to expire."""
        if self._guard is not None:
            self._guard.check()
        if self._token is None or self._clock() >= self._token_until:
            self._fetch_token()
        return self._token  # type: ignore[return-value]

    def list_devices(self) -> list[ImouDevice]:
        """All devices of the account with their state (one call per 50 devices, at most :data:`MAX_PAGES`)."""
        devices: list[ImouDevice] = []
        for page in range(1, MAX_PAGES + 1):
            data = self._authed(_LIST, {"pageSize": PAGE_SIZE, "page": page})
            rows = data.get("deviceList")
            if not isinstance(rows, list):
                raise ImouProtocolError("the device list has an unexpected shape")
            devices.extend(d for d in (_device(r) for r in rows) if d is not None)
            if len(rows) < PAGE_SIZE:
                break
        return devices

    def create_rtsp_url(self, serial: str, channel: str, stream_type: int) -> str:
        """Temporary RTSP address for live view (``stream_type`` 0 = main/HD, 1 = sub/SD). A secret."""
        data = self._authed(_STREAM_URL, {"deviceId": serial, "channelId": str(channel), "businessType": "real",
                                          "streamType": str(int(stream_type))})
        url = data.get("url")
        if not isinstance(url, str) or not url:
            raise ImouProtocolError("the stream address reply has an unexpected shape")
        return decrypt_stream_url(url, self.access_token())

    def bind_live(self, serial: str, channel: str, stream_id: int) -> ImouLive:
        """Create a cloud live (HLS) address; ``stream_id`` 0 = HD, 1 = SD. Delete it with :meth:`unbind_live`."""
        data = self._authed(_BIND_LIVE, {"deviceId": serial, "channelId": str(channel), "streamId": int(stream_id)})
        live = _live(data, serial, channel, stream_id)
        if live is None:
            raise ImouProtocolError("the live address reply has an unexpected shape")
        return live

    def find_live(self, serial: str, channel: str, stream_id: int | None = None) -> ImouLive | None:
        """An existing live address of this camera (``liveList``), or ``None``."""
        data = self._authed(_LIVE_LIST, {"queryRange": "1-100"})
        for row in data.get("lives") or []:
            if isinstance(row, dict) and str(row.get("deviceId")) == serial and str(row.get("channelId")) == str(channel):
                live = _live(row, serial, channel, stream_id)
                if live is not None:
                    return live
        return None

    def unbind_live(self, live_token: str) -> None:
        """Delete a live address that this app created."""
        self._authed(_UNBIND_LIVE, {"liveToken": live_token})

    # ================================================================== plumbing
    def _fetch_token(self) -> None:
        data = self._call(_TOKEN, {})
        token, ttl = data.get("accessToken"), data.get("expireTime")
        if not isinstance(token, str) or not token:
            raise ImouProtocolError("the token reply has an unexpected shape")
        seconds = float(ttl) if isinstance(ttl, (int, float)) and not isinstance(ttl, bool) and ttl > 0 else 3600.0
        self._token, self._token_until = token, self._clock() + max(60.0, seconds - _TOKEN_MARGIN_S)

    def _authed(self, path: str, params: dict) -> dict:
        """A call that needs the token; ``TK1002`` gets one new token and one retry."""
        token = self.access_token()
        try:
            return self._call(path, {**params, "token": token})
        except ImouTokenExpiredError:
            self._token = None
            token = self.access_token()
            return self._call(path, {**params, "token": token})

    def _call(self, path: str, params: dict) -> dict:
        if path not in ALLOWED_PATHS:
            raise ImouNotAllowedError("this request is not allowed")
        if self._guard is not None:
            self._guard.check()
        url = self._base + path
        if not regions.is_allowed_url(url):  # pragma: no cover - base_url() only yields allowed hosts
            raise ImouNotAllowedError("this address is not an iMou data center")
        body = signing.encode(signing.build_request(self._app_id, self._secret, params, time_s=int(self._clock()),
                                                    nonce=self._nonce(), request_id=str(uuid.uuid4())))
        try:
            status, raw = self._post(url, body, self._timeout)
        except ImouError:
            raise
        except OSError as exc:
            raise ImouConnectionError(f"cannot reach the iMou servers ({type(exc).__name__})") from None
        if status != 200:
            raise ImouConnectionError(f"the iMou servers answered with HTTP {status}")
        result = _result(raw)
        code = str(result.get("code", ""))
        if code != "0":
            exc = error_for(code, str(result.get("msg", "")))
            log.info("imou %s refused (%s)", path.rsplit("/", 1)[-1], code)
            if self._guard is not None:
                self._guard.note(exc)
            if code == "SN1001":
                self._token = None
            raise exc
        data = result.get("data")
        return data if isinstance(data, dict) else {}


def _result(raw: bytes) -> dict[str, Any]:
    try:
        doc = json.loads(raw.decode("utf-8"))
        result = doc["result"]
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeDecodeError):
        raise ImouProtocolError("the iMou servers sent an unexpected reply") from None
    if not isinstance(result, dict):
        raise ImouProtocolError("the iMou servers sent an unexpected reply")
    return result


def _device(row: Any) -> ImouDevice | None:
    if not isinstance(row, dict) or not isinstance(row.get("deviceId"), str) or not row["deviceId"]:
        return None
    channels = row.get("channelList")
    channel = "0"
    if isinstance(channels, list) and channels and isinstance(channels[0], dict) and "channelId" in channels[0]:
        channel = str(channels[0]["channelId"])
    return ImouDevice(serial=row["deviceId"], name=str(row.get("deviceName") or row["deviceId"]),
                      model=str(row.get("deviceModel") or ""), status=str(row.get("deviceStatus") or ""),
                      channel=channel)


def _live(row: dict, serial: str, channel: str, stream_id: int | None) -> ImouLive | None:
    token, streams = row.get("liveToken"), row.get("streams")
    if not isinstance(token, str) or not token or not isinstance(streams, list):
        return None
    urls = [s for s in streams if isinstance(s, dict) and isinstance(s.get("hls"), str) and s["hls"]]
    if stream_id is not None:
        urls = [s for s in urls if s.get("streamId") == stream_id] or urls
    if not urls:
        return None
    best = next((s for s in urls if s["hls"].startswith("https://")), urls[0])  # prefer the TLS address
    return ImouLive(live_token=token, hls=best["hls"], device=serial, channel=str(channel))
