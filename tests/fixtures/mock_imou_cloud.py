"""Mock of the iMou Open Platform HTTP API (route A), in process: no socket, no real host.

``MockImouCloud`` is a *transport*: ``mock.post(url, body, timeout) -> (status, bytes)``. It checks what the
real service checks and answers in the documented shapes (made-up ids, keys and serials only):

* request signing (``system.sign`` = Base64(HMAC-SHA256(SHA-256-hex(secret), "time:..,nonce:..,appSecret:..")),
  a 5 minute time window (SN1002) and a nonce that may not repeat inside 5 minutes (SN1005);
* ``accessToken`` (3 days) and token checks on every other call (TK1002 once a token is expired);
* ``listDeviceDetailsByPage`` (paged), ``createVideoDownloadStreamUrl`` (reply AES-256-GCM encrypted with
  ``SHA-256(token + "ENCRYPTKEY")``, 12 byte IV in front, 16 byte tag at the end), ``bindDeviceLive`` /
  ``liveList`` / ``unbindLive``;
* ``fail(path, code, msg)`` makes the next call(s) of a method answer with a vendor error code;
* every call is recorded in ``calls`` (path, params, system); ``paths()`` lists the paths used.

Additive fixture: nothing here changes behaviour of any other fixture.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MOCK_APP_ID = "lcdMOCKAPPID000001"
MOCK_APP_SECRET = "mock-app-secret-0000000000000001"
MOCK_RTSP_QUERY = "token=MOCKRTSPSECRET0123456789abcdef"
#: URL handed out for live view (TEST-NET-3 address: nothing listens, nothing is contacted).
MOCK_RTSP_URL = "rtsp://203.0.113.7:554/cam/realmonitor?channel=1&subtype={streamType}&" + MOCK_RTSP_QUERY
#: URL handed out for memory-card playback (``businessType=localRecord``); same secret query as live.
MOCK_PLAYBACK_URL = ("rtsp://203.0.113.7:554/cam/playback?channel=1&subtype={streamType}&starttime={begin}"
                     "&endtime={end}&" + MOCK_RTSP_QUERY)
MOCK_HLS_QUERY = "source=open&proto=https&sig=MOCKHLSSECRET0123456789"
TOKEN_TTL_S = 259176
WINDOW_S = 300


def make_device(serial: str, name: str = "", status: str = "online", model: str = "IPC-MOCK") -> dict:
    return {"deviceId": serial, "deviceName": name or serial, "deviceStatus": status, "deviceModel": model,
            "catalog": "IPC", "source": "bind",
            "channelList": [{"channelId": 0, "channelName": name or serial, "channelStatus": status}]}


def make_record(begin: str, end: str, *, local_begin: str | None = None, local_end: str | None = None,
                stream_type: str = "0", kind: str = "1", name: str | None = None) -> dict:
    """One ``record.file.list`` row (documented fields, made-up values). Times are ``YYYY-MM-DD HH:mm:ss``."""
    return {"fileName": name or f"rec-{begin}", "fileLength": 1024, "type": kind, "streamType": stream_type,
            "beginTime": begin, "endTime": end, "localBeginTime": local_begin or begin,
            "localEndTime": local_end or end, "objectType": "", "xseq": 1, "multiType": "", "picId": 0}


@dataclass
class Call:
    path: str
    params: dict
    system: dict
    url: str


@dataclass
class _Failure:
    code: str
    msg: str
    times: int | None  # None = always


@dataclass
class MockImouCloud:
    app_id: str = MOCK_APP_ID
    app_secret: str = MOCK_APP_SECRET
    clock: object = time.time
    devices: list = field(default_factory=lambda: [make_device("MOCKSERIAL0001", "Mock Camera A"),
                                                   make_device("MOCKSERIAL0002", "Mock Camera B")])
    rtsp_url: str = MOCK_RTSP_URL
    hls_host: str = "https://cmgw-online-mock.invalid:8890"
    calls: list = field(default_factory=list)
    #: memory-card recordings per serial (rows from :func:`make_record`); serials in ``no_card`` answer "no storage medium"
    records: dict = field(default_factory=dict)
    no_card: set = field(default_factory=set)
    playback_url: str = MOCK_PLAYBACK_URL

    def __post_init__(self):
        self._lock = threading.Lock()
        self._tokens: dict[str, float] = {}  # token -> expiry (epoch s)
        self._nonces: dict[str, float] = {}
        self._failures: dict[str, list[_Failure]] = {}
        self._lives: dict[str, dict] = {}
        self._token_n = 0
        self._live_n = 0
        self._url_n = 0
        self.drop_connection = False  # next post raises OSError

    # ------------------------------------------------------------------ test controls
    def fail(self, path: str, code: str, msg: str | None = None, times: int | None = 1) -> None:
        """Answer ``/openapi/<path>`` (or ``"*"``) with vendor error ``code`` ``times`` times (None = always)."""
        with self._lock:
            self._failures.setdefault(path, []).append(_Failure(code, msg or f"mock error {code}", times))

    def expire_tokens(self) -> None:
        with self._lock:
            self._tokens = {t: 0.0 for t in self._tokens}

    def paths(self) -> list[str]:
        return [c.path for c in self.calls]

    def count(self, path: str) -> int:
        return self.paths().count(path)

    def live_count(self) -> int:
        return len(self._lives)

    def last_issued_token(self) -> str | None:
        with self._lock:
            return list(self._tokens)[-1] if self._tokens else None

    def sign_for(self, time_s: int, nonce: str) -> str:
        return expected_sign(self.app_secret, time_s, nonce)

    # ------------------------------------------------------------------ transport
    def post(self, url: str, body: bytes, timeout: float = 30.0) -> tuple[int, bytes]:
        if self.drop_connection:
            self.drop_connection = False
            raise OSError("mock: connection dropped")
        parts = urlsplit(url)
        path = parts.path.rsplit("/", 1)[-1]
        req = json.loads(body.decode("utf-8"))
        system, params = req.get("system", {}), req.get("params", {})
        with self._lock:
            self.calls.append(Call(path, dict(params), dict(system), url))
            result = self._handle(path, system, params)
        return 200, json.dumps({"result": result, "id": req.get("id")}).encode("utf-8")

    # ------------------------------------------------------------------ internals
    def _err(self, code: str, msg: str | None = None) -> dict:
        return {"code": code, "msg": msg or f"mock error {code}"}

    def _handle(self, path: str, system: dict, params: dict) -> dict:
        now = self.clock()
        for key in (path, "*"):
            queue = self._failures.get(key) or []
            if queue:
                failure = queue[0]
                if failure.times is not None:
                    failure.times -= 1
                    if failure.times <= 0:
                        queue.pop(0)
                return self._err(failure.code, failure.msg)
        if system.get("appId") != self.app_id:
            return self._err("SN1004", "Illegal appId")
        try:
            sent_time = int(system["time"])
        except (KeyError, TypeError, ValueError):
            return self._err("SN1003", "bad time")
        if abs(now - sent_time) > WINDOW_S:
            return self._err("SN1002", "Signature timeout")
        nonce = str(system.get("nonce", ""))
        if system.get("sign") != self.sign_for(sent_time, nonce):
            return self._err("SN1001", "Signature exception")
        self._nonces = {n: t for n, t in self._nonces.items() if now - t < WINDOW_S}
        if nonce in self._nonces:
            return self._err("SN1005", "Duplicate nonce")
        self._nonces[nonce] = now
        if path == "accessToken":
            self._token_n += 1
            token = f"At_{self._token_n:032x}"
            self._tokens[token] = now + TOKEN_TTL_S
            return {"code": "0", "msg": "Successful operation.", "data": {"accessToken": token,
                                                                          "expireTime": TOKEN_TTL_S}}
        token = params.get("token")
        if token not in self._tokens or self._tokens[token] <= now:
            return self._err("TK1002", "Token has expired or does not exist")
        handler = self._do_invoke if path == "device.service.invoke" else getattr(self, f"_do_{path}", None)
        if handler is None:
            return self._err("OP1005", "Invalid request URL")
        return handler(token, params)

    def _device(self, serial: str):
        return next((d for d in self.devices if d["deviceId"] == serial), None)

    def _do_listDeviceDetailsByPage(self, token: str, params: dict) -> dict:
        size, page = int(params.get("pageSize", 10)), int(params.get("page", 1))
        chunk = self.devices[(page - 1) * size: page * size]
        return {"code": "0", "msg": "Successful operation.", "data": {"count": len(chunk), "deviceList": chunk}}

    def _do_createVideoDownloadStreamUrl(self, token: str, params: dict) -> dict:
        dev = self._device(params.get("deviceId", ""))
        if dev is None:
            return self._err("DV1028", "not bound")
        if dev["deviceStatus"] != "online":
            return self._err("DV1007", "Device offline")
        self._url_n += 1
        if params.get("businessType") == "localRecord":
            return self._playback_url(token, params, dev)
        url = self.rtsp_url.replace("{streamType}", str(params.get("streamType", "1"))) \
            .replace("{serial}", params["deviceId"]).replace("{n}", str(self._url_n))
        return {"code": "0", "msg": "Operation succeeded.", "data": {"url": encrypt_stream_url(url, token)}}

    def _playback_url(self, token: str, params: dict, dev: dict) -> dict:
        begin, end = str(params.get("beginTime", "")), str(params.get("endTime", ""))
        if not (_valid_wall(begin) and _valid_wall(end)) or end < begin:
            return self._err("OP1003", "The parameter value is illegal")
        serial = params["deviceId"]
        if serial in self.no_card:
            return self._err("DV1049", "The device has no storage medium")
        rows = self.records.get(serial) or []
        if not any(r["beginTime"] <= end and r["endTime"] >= begin for r in rows):
            return self._err("OP1019", "No video generated during this time period")
        url = self.playback_url.replace("{streamType}", str(params.get("streamType", "0"))) \
            .replace("{begin}", begin.replace(" ", "T")).replace("{end}", end.replace(" ", "T"))
        return {"code": "0", "msg": "Operation succeeded.", "data": {"url": encrypt_stream_url(url, token)}}

    def _do_invoke(self, token: str, params: dict) -> dict:
        """``/openapi/v2/device.service.invoke``: only the ``record.file.list`` service exists here."""
        dev = self._device(params.get("deviceId", ""))
        if dev is None:
            return self._err("DV1028", "not bound")
        if dev["deviceStatus"] != "online":
            return self._err("DV1007", "Device offline")
        if params.get("serviceId") != "record.file.list":
            return self._err("DV2003", "Capability not on device")
        if "channelId" not in params:
            return self._err("DV2007", "channelId vs scope mismatch")
        data = params.get("inputData")
        if not isinstance(data, dict):
            return self._err("OP1002", "Missing required parameter")
        begin, end, limit = str(data.get("beginTime", "")), str(data.get("endTime", "")), data.get("limit")
        if not (_valid_wall(begin) and _valid_wall(end)) or not isinstance(limit, int) or isinstance(limit, bool) \
                or not 30 <= limit <= 100 or not isinstance(data.get("type"), str):
            return self._err("OP1003", "The parameter value is illegal")
        serial = params["deviceId"]
        if serial in self.no_card:
            return self._err("DV1049", "The device has no storage medium")
        rows = sorted((r for r in self.records.get(serial, []) if begin <= r["beginTime"] <= end),
                      key=lambda r: r["beginTime"])[:limit]
        return {"code": "0", "msg": "Operation is successful",
                "data": {"outputData": {"sessionId": "s-1", "records": rows}}}

    def _live_entry(self, serial: str, channel: str, stream_id: int, live_token: str) -> dict:
        base = f"{self.hls_host}/LCO/{serial}/{channel}/{stream_id}/{live_token}.m3u8"
        return {"liveToken": live_token, "liveStatus": 1, "liveType": 1, "deviceId": serial, "channelId": channel,
                "streams": [{"streamId": stream_id, "hls": f"{base}?{MOCK_HLS_QUERY}"}]}

    def _do_bindDeviceLive(self, token: str, params: dict) -> dict:
        dev = self._device(params.get("deviceId", ""))
        if dev is None:
            return self._err("DV1028", "not bound")
        if dev["deviceStatus"] != "online":
            return self._err("DV1007", "Device offline")
        key = (params["deviceId"], str(params.get("channelId", "0")))
        if any((v["deviceId"], v["channelId"]) == key for v in self._lives.values()):
            return self._err("LV1001", "The live video already exists")
        self._live_n += 1
        live_token = f"{self._live_n:032x}"
        entry = self._live_entry(key[0], key[1], int(params.get("streamId", 1)), live_token)
        self._lives[live_token] = entry
        return {"code": "0", "msg": "Operation succeeded.", "data": entry}

    def _do_liveList(self, token: str, params: dict) -> dict:
        lives = list(self._lives.values())
        return {"code": "0", "msg": "Operation succeeded.", "data": {"count": len(lives), "lives": lives}}

    def _do_unbindLive(self, token: str, params: dict) -> dict:
        if self._lives.pop(params.get("liveToken", ""), None) is None:
            return self._err("LV1002", "The live video does not exist")
        return {"code": "0", "msg": "Operation succeeded.", "data": {}}


def _valid_wall(text: str) -> bool:
    """``YYYY-MM-DD HH:mm:ss`` (the one time format of the recording calls)."""
    try:
        time.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    return len(text) == 19


# ---------------------------------------------------------------------- independent crypto (not the app's code)
def expected_sign(secret: str, time_s: int, nonce: str) -> str:
    key = hashlib.sha256(secret.encode("utf-8")).hexdigest().encode("ascii")
    msg = f"time:{time_s},nonce:{nonce},appSecret:{secret}".encode("utf-8")
    return base64.b64encode(hmac.new(key, msg, hashlib.sha256).digest()).decode("ascii")


def encrypt_stream_url(url: str, token: str) -> str:
    key = hashlib.sha256((token + "ENCRYPTKEY").encode("utf-8")).digest()
    iv = os.urandom(12)
    return base64.b64encode(iv + AESGCM(key).encrypt(iv, url.encode("utf-8"), None)).decode("ascii")
