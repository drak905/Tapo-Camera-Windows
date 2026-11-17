"""Remote (off-LAN) live view through the TP-Link cloud relay, like the Tapo phone app away from home
[spec:remote.relay.*].

Flow (proven against a real remote camera with tools/diag_relay.py):

1. CIPC host: signed ``getAppServiceUrl`` ``{"serviceIds": ["cipc.api.cloud"]}`` through the signed-in
   :class:`~tapo_desktop.core.cloud.client.TapoCloudClient`; fallback ``aps1-cipc-api.i.tplinkcloud.com``
   [spec:remote.relay.cipc_host.discovery].
2. Slot: ``POST https://<cipc>/v2/relay/request`` with the raw account token in ``Authorization`` (no
   ``ut|``, not HMAC-signed) and the app headers [spec:remote.relay.cipc.http_client,
   remote.relay.cipc.request_relay_url]. ``cloudType`` must be 2 for IoT-cloud cameras (1 gives a slot
   whose tunnel answers 500 -1007), so 2 is the default; 1 only for devices the device list marks as
   legacy non-IoT (:func:`cloud_type_for`). -1102 / HTTP 401 -> one silent re-login, then once more.
3. Tunnel: TLS to ``relayIp:443`` with SNI / hostname check = the relay host from ``relayUrl``, trust =
   TP-Link roots + system (:func:`~tapo_desktop.core.cloud.client.make_ssl_context`), then one never-ending
   ``POST <relayPath>&retryTime=0 HTTP/1.1`` with the headers in the exact app order
   [spec:remote.relay.tunnel.request]. 200 -> raw tunnel; 304 -> RequestUrlResult body, reconnect with
   ``X-Redirect-Times`` + 1 (max :data:`MAX_REDIRECTS`); anything else -> JSON ``errorCode`` / ``closeReason``
   [spec:remote.relay.tunnel.response].
4. Inside the tunnel the client talks the LAN :8800 multipart protocol directly (no inner ``POST /stream``,
   no digest, plaintext TS) [spec:remote.relay.stream_over_tunnel] and writes a heartbeat part every 15 s
   [spec:remote.relay.heartbeat]: :class:`RelayMediaSession`.

The account token, relay token and ELB cookie never appear in logs, reprs or exception messages.
"""
from __future__ import annotations

import http.client
import json
import logging
import secrets
import socket
import ssl
import string
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from tapo_desktop.core.cloud.client import CloudError, make_ssl_context
from tapo_desktop.core.errors import TapoConnectionError, TapoError, TapoProtocolError
from tapo_desktop.core.vod.session import CLIENT_BOUNDARY, MediaPart, MediaSession, encode_client_part

__all__ = ["CIPC_SERVICE_ID", "DEFAULT_CIPC_HOST", "HEARTBEAT_BODY", "HEARTBEAT_INTERVAL_S", "MAX_REDIRECTS",
           "MSG_CAMERA_OFFLINE", "RelayClient", "RelayError", "RelayMediaSession", "RelaySlot", "cloud_type_for"]

log = logging.getLogger(__name__)

CIPC_SERVICE_ID = "cipc.api.cloud"
DEFAULT_CIPC_HOST = "aps1-cipc-api.i.tplinkcloud.com"
RELAY_PORT = 443
HEARTBEAT_INTERVAL_S = 15.0
#: Heartbeat JSON exactly as the app writes it (with spaces; Content-Length 63) [spec:remote.relay.heartbeat].
HEARTBEAT_BODY = b'{"type": "notification", "params": {"event_type": "heartbeat"}}'
MAX_REDIRECTS = 3
CONTENT_LENGTH_SENTINEL = "9223372036854775807"
APP_TYPE = "TP-Link_Tapo_Android"
ERR_DEVICE_OFFLINE = -1007
ERR_TOKEN_INVALID = -1102
ERR_ILLEGAL_REQUEST = 3
MSG_CAMERA_OFFLINE = "Camera is offline (the TP-Link cloud relay cannot reach it)."
#: Relay errors retrying cannot fix [spec:remote.overview.selection fatal_errors_no_retry].
_FATAL_MESSAGES: dict[int, str] = {
    ERR_DEVICE_OFFLINE: MSG_CAMERA_OFFLINE,
    -1103: "This camera is not linked to the signed-in TP-Link ID.",
    -52407: "stream limit reached: the camera refused another viewer.",
    -52212: "The camera's cloud relay data limit is reached.",
    -100001: "Too many relay requests; try again in a minute.",
    -20675: "The TP-Link ID signed in elsewhere; sign in again.",
    ERR_ILLEGAL_REQUEST: "The TP-Link relay rejected the request (illegal_request).",
}
_MAX_HEAD = 16 * 1024
_MAX_BODY = 64 * 1024
_SIGNATURE_CHARS = string.ascii_letters + string.digits


class RelayError(CloudError):
    """The CIPC relay API or a relay node refused (``code``: errorCode or HTTP status).

    ``fatal`` = retrying the same request cannot help (camera offline, not bound, stream limit ...)."""

    def __init__(self, message: str, code: int | str = "", *, fatal: bool = False, result: dict | None = None):
        super().__init__(message, code, result)
        self.fatal = fatal


def _error(code: int | str, detail: str = "", what: str = "relay") -> RelayError:
    """User-facing RelayError for an errorCode (never contains tokens: ``detail`` is a server reason)."""
    if isinstance(code, int) and code in _FATAL_MESSAGES:
        text = _FATAL_MESSAGES[code]
        if code == ERR_ILLEGAL_REQUEST and detail and detail not in text:
            text = f"{text[:-1]}: {detail})."
        return RelayError(text, code, fatal=True)
    suffix = f": {detail}" if detail else ""
    return RelayError(f"TP-Link cloud {what} error {code}{suffix}", code)


def cloud_type_for(raw: dict | None) -> int:
    """``cloudType`` for a device-list entry: 1 only when it explicitly says it is NOT an IoT-cloud device
    (``isSupportIoTCloud`` / ``is_support_iot_cloud`` false), else 2 (real-world finding: 1 fails)."""
    if not isinstance(raw, dict):
        return 2
    for key in ("isSupportIoTCloud", "is_support_iot_cloud"):
        if raw.get(key) is False:
            return 1
    return 2


def _int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class RelaySlot:
    """One relay slot from ``/v2/relay/request`` (or a 304 redirect) [spec:remote.relay.cipc.response].

    ``relay_path`` is ``/relayservice?...`` (no ``retryTime``). ``token`` (X-token) and ``elb_cookie``
    are secrets: excluded from ``repr``."""

    device_id: str
    relay_host: str
    relay_port: int
    relay_path: str
    relay_ip: str
    track_id: str
    resolution: str
    token: str = field(repr=False)
    elb_cookie: str = field(default="", repr=False)

    @classmethod
    def from_result(cls, result: dict, *, device_id: str, track_id: str, resolution: str) -> "RelaySlot":
        """Parse a RequestUrlResult; raises RelayError when it carries no usable relay address/token."""
        url = str(result.get("relayUrl") or "").strip()
        token = result.get("relayToken") or result.get("sessionToken")
        if not url or not isinstance(token, str) or not token:
            raise RelayError("The TP-Link cloud returned no relay address.", "no_relay")
        parts = urlsplit(url if "//" in url else "https://" + url)
        if not parts.hostname:
            raise RelayError("The TP-Link cloud returned an invalid relay address.", "bad_relay_url")
        path = parts.path or "/relayservice"
        if parts.query:
            path = f"{path}?{parts.query}"
        try:
            port = parts.port or RELAY_PORT
        except ValueError:
            raise RelayError("The TP-Link cloud returned an invalid relay address.", "bad_relay_url") from None
        cookie = result.get("elbCookie")
        return cls(device_id=device_id, relay_host=parts.hostname, relay_port=port, relay_path=path,
                   relay_ip=str(result.get("relayIp") or "").strip(), track_id=track_id, resolution=resolution,
                   token=token, elb_cookie=cookie if isinstance(cookie, str) else "")


class RelayClient:
    """CIPC slot requests and relay tunnels for the account of ``cloud_client`` (thread-safe).

    ``cloud_client`` is a signed-in :class:`~tapo_desktop.core.cloud.client.TapoCloudClient` (duck-typed:
    ``token``, ``get_service_urls``, ``refresh_session``, ``terminal_uuid``, ``base_url``).
    ``ssl_context`` (default: TP-Link roots + system, hostname checked) is used for the CIPC API and the
    relay node; tests inject one that trusts their mock relay. Every network call is bounded by ``timeout``.
    """

    def __init__(self, cloud_client: Any, *, terminal_uuid: str | None = None, timeout: float = 15.0,
                 ssl_context: ssl.SSLContext | None = None, app_version: str = "3.22.806",
                 client_model: str = "Windows"):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._cloud = cloud_client
        self.terminal_uuid = terminal_uuid or str(getattr(cloud_client, "terminal_uuid", "") or "")
        self.timeout = float(timeout)
        self.app_version = app_version
        self.client_model = client_model
        self._ssl = ssl_context
        self._lock = threading.Lock()
        self._cipc: str | None = None

    def __repr__(self) -> str:
        return f"RelayClient(cipc={self._cipc!r}, timeout={self.timeout:g})"

    # ------------------------------------------------------------------ CIPC host
    def _context(self) -> ssl.SSLContext:
        with self._lock:
            if self._ssl is None:
                self._ssl = make_ssl_context(True)
            return self._ssl

    def cipc_base(self) -> str:
        """``https://<cipc host>`` from ``getAppServiceUrl`` (cached), or the aps1 fallback when the
        cloud lists none [spec:remote.relay.cipc_host.discovery]. Raises CloudError / TapoConnectionError."""
        with self._lock:
            if self._cipc is not None:
                return self._cipc
        urls = self._cloud.get_service_urls([CIPC_SERVICE_ID])
        value = str(urls.get(CIPC_SERVICE_ID) or "").strip().rstrip("/") if isinstance(urls, dict) else ""
        base = value if "//" in value else f"https://{value or DEFAULT_CIPC_HOST}"
        parts = urlsplit(base)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise CloudError("The TP-Link cloud returned an invalid relay API address.", "bad_url")
        if parts.scheme != "https" and urlsplit(str(getattr(self._cloud, "base_url", ""))).scheme == "https":
            raise CloudError("The TP-Link cloud returned an insecure relay API address.", "bad_url")
        with self._lock:
            self._cipc = base.rstrip("/")
            log.info("relay API host: %s", parts.hostname)
            return self._cipc

    # ------------------------------------------------------------------ slot
    def _cipc_headers(self, token: str, model: str) -> dict[str, str]:
        """[spec:remote.relay.cipc.http_client] (never logged: carries the account token)."""
        return {"Authorization": token.split(";")[0], "Content-Type": "application/json; charset=UTF-8",
                "Accept": "*/*", "User-Agent": f"Tapo_APP/{model or self.client_model}/{self.app_version}_Android/Android 14",
                "X-Request-Signature": "".join(secrets.choice(_SIGNATURE_CHARS) for _ in range(10)),
                "X-Client-Id": self.terminal_uuid, "X-Source": "tapo-app", "X-Brand": "TPLINK",
                "X-Ca-Type": "cloud-self", "Connection": "close"}

    def _post_json(self, base: str, path: str, body: dict, headers: dict[str, str]) -> tuple[int, dict]:
        parts = urlsplit(base)
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        conn: http.client.HTTPConnection
        if parts.scheme == "https":
            conn = http.client.HTTPSConnection(parts.hostname or "", parts.port, timeout=self.timeout,
                                               context=self._context())
        else:
            conn = http.client.HTTPConnection(parts.hostname or "", parts.port, timeout=self.timeout)
        try:
            conn.request("POST", parts.path.rstrip("/") + path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read(_MAX_BODY + 1)
            status = resp.status
        except (OSError, http.client.HTTPException) as exc:
            log.info("relay request: connection failed (%s)", type(exc).__name__)
            raise TapoConnectionError(f"Cannot reach the TP-Link relay service ({type(exc).__name__}: {exc})") from None
        finally:
            conn.close()
        try:
            reply = json.loads(raw.decode("utf-8")) if raw and len(raw) <= _MAX_BODY else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            reply = {}
        return status, reply if isinstance(reply, dict) else {}

    def _refresh_session(self) -> None:
        refresh = getattr(self._cloud, "refresh_session", None)
        if refresh is None:
            raise RelayError("The TP-Link cloud session expired; sign in again.", ERR_TOKEN_INVALID, fatal=True)
        refresh()

    def request_slot(self, device_id: str, device_type: str = "SMART.IPCAMERA", model: str = "",
                     resolution: str = "VGA", *, cloud_type: int = 2) -> RelaySlot:
        """``POST <cipc>/v2/relay/request`` for a live preview slot [spec:remote.relay.cipc.request_relay_url].

        -1102 / HTTP 401: one silent re-login, then one retry. errorCode != 0 -> RelayError (``fatal`` for
        the no-retry codes; -1007 says "Camera is offline"). Network problems -> TapoConnectionError.
        """
        if resolution not in ("HD", "VGA"):
            raise ValueError("resolution must be 'HD' or 'VGA'")
        if cloud_type not in (1, 2):
            raise ValueError("cloud_type must be 1 or 2")
        if not device_id:
            raise ValueError("device_id is required")
        if not getattr(self._cloud, "token", None):
            raise RelayError("Not signed in to a TP-Link ID.", "not_signed_in")
        track_id = str(uuid.uuid4())
        body = {"deviceId": device_id, "dataTimeoutCount": 0, "cloudType": cloud_type,
                "deviceType": device_type or "SMART.IPCAMERA", "playerId": self.terminal_uuid, "rootCaVer": "1",
                "preConnection": 0, "resolution": resolution, "streamType": 0, "customParams": "{}",
                "trackId": track_id}
        base = self.cipc_base()
        for attempt in range(2):
            token = getattr(self._cloud, "token", None)
            if not token:
                raise RelayError("Not signed in to a TP-Link ID.", "not_signed_in")
            status, reply = self._post_json(base, "/v2/relay/request", body, self._cipc_headers(token, model))
            code = _int(reply.get("errorCode")) if "errorCode" in reply else None
            if status == 401 or code == ERR_TOKEN_INVALID:
                if attempt == 0:
                    log.info("relay request: account token rejected; signing in again silently")
                    self._refresh_session()
                    continue
                raise RelayError("The TP-Link cloud session expired; sign in again.", ERR_TOKEN_INVALID, fatal=True)
            if code is None:
                if status >= 400:
                    raise _error(status, what="relay request HTTP")
                code = 0
            if code != 0:
                log.info("relay request for %s: errorCode %s", device_id, code)
                raise _error(code, str(reply.get("msg") or "")[:120], "relay request")
            result = reply.get("result") if isinstance(reply.get("result"), dict) else {}
            slot = RelaySlot.from_result(result, device_id=device_id, track_id=track_id, resolution=resolution)
            log.info("relay slot for %s on node %s", device_id, slot.relay_host)
            return slot
        raise RelayError("The TP-Link cloud session expired; sign in again.", ERR_TOKEN_INVALID, fatal=True)

    # ------------------------------------------------------------------ tunnel
    def tunnel_request(self, slot: RelaySlot, redirect_times: int = 0, arrive_latency_ms: int = 0) -> bytes:
        """The tunnel request head, headers in the exact app order [spec:remote.relay.tunnel.request]."""
        sep = "&" if "?" in slot.relay_path else "?"
        headers = [("User-Agent", f"Client={APP_TYPE} Android {self.app_version}/1.3"), ("Keep-Relay", "3600"),
                   ("Accept", "*/*"), ("Content-Type", f"multipart/mixed;boundary={CLIENT_BOUNDARY}"),
                   ("Host", slot.relay_host), ("Content-Length", CONTENT_LENGTH_SENTINEL), ("X-token", slot.token),
                   ("X-Client-Model", self.client_model), ("X-Client-UUID", self.terminal_uuid),
                   ("X-Track-Id", slot.track_id), ("X-Redirect-Times", str(int(redirect_times))),
                   ("X-Pull-Mode", "2")]
        if slot.elb_cookie:
            headers.append(("Cookie", slot.elb_cookie))
        headers += [("X-Version", "2.0"), ("X-Arrive-Latency", str(max(0, int(arrive_latency_ms))))]
        lines = [f"POST {slot.relay_path}{sep}retryTime=0 HTTP/1.1", *(f"{k}: {v}" for k, v in headers)]
        if any(c in line for line in lines for c in "\r\n\x00"):
            raise RelayError("The TP-Link cloud returned an invalid relay slot.", "bad_slot")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")

    def _connect(self, slot: RelaySlot) -> tuple[ssl.SSLSocket, int]:
        """TCP to relayIp (or DNS of the host) + TLS with SNI / hostname check = relay host."""
        started = time.monotonic()
        try:
            raw = socket.create_connection((slot.relay_ip or slot.relay_host, slot.relay_port), timeout=self.timeout)
        except OSError as exc:
            raise TapoConnectionError(f"Cannot reach the TP-Link relay ({type(exc).__name__}: {exc})") from None
        latency = int((time.monotonic() - started) * 1000)
        try:
            raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            tls = self._context().wrap_socket(raw, server_hostname=slot.relay_host)
        except ssl.SSLCertVerificationError as exc:
            raw.close()
            raise TapoConnectionError(f"The TP-Link relay certificate is not trusted ({exc.verify_message})") from None
        except OSError as exc:
            raw.close()
            raise TapoConnectionError(f"TLS to the TP-Link relay failed ({type(exc).__name__}: {exc})") from None
        return tls, latency

    @staticmethod
    def _read_response(sock: ssl.SSLSocket) -> tuple[int, dict[str, str], bytes]:
        """Read the response head byte-wise (nothing after a 200 head is consumed) and, for other
        statuses, the (bounded) body."""
        head = bytearray()
        while not head.endswith(b"\r\n\r\n"):
            chunk = sock.recv(1)
            if not chunk:
                raise TapoConnectionError("The TP-Link relay closed the connection.")
            head += chunk
            if len(head) > _MAX_HEAD:
                raise TapoProtocolError("relay response head too large")
        status_line, *lines = bytes(head[:-4]).decode("latin-1").split("\r\n")
        fields = status_line.split(" ", 2)
        if len(fields) < 2 or not fields[0].startswith("HTTP/") or not fields[1].isdigit():
            raise TapoProtocolError("malformed status line from the TP-Link relay")
        headers: dict[str, str] = {}
        for line in lines:
            name, sep, value = line.partition(":")
            if sep:
                headers[name.strip().lower()] = value.strip()
        status = int(fields[1])
        if status == 200:
            return status, headers, b""
        body = bytearray()
        length = headers.get("content-length", "")
        want = min(int(length), _MAX_BODY) if length.isdigit() else _MAX_BODY
        try:
            while len(body) < want:
                chunk = sock.recv(want - len(body))
                if not chunk:
                    break
                body += chunk
                if not length.isdigit():
                    break  # unknown length: take what arrived with the head
        except OSError:
            pass
        return status, headers, bytes(body)

    def open_tunnel(self, slot: RelaySlot) -> ssl.SSLSocket:
        """Open the relay tunnel for ``slot`` and return the TLS socket positioned right after the 200
        head [spec:remote.relay.tunnel.*]. 304 -> follow the RequestUrlResult (max :data:`MAX_REDIRECTS`).
        Refusals -> RelayError (-1007 fatal "Camera is offline"); network/TLS -> TapoConnectionError."""
        current = slot
        for redirects in range(MAX_REDIRECTS + 1):
            sock, latency = self._connect(current)
            try:
                sock.sendall(self.tunnel_request(current, redirects, latency))
                status, _headers, body = self._read_response(sock)
            except socket.timeout:
                sock.close()
                raise TapoConnectionError("The TP-Link relay did not answer in time.") from None
            except (TapoError, ValueError):
                sock.close()
                raise
            except OSError as exc:
                sock.close()
                raise TapoConnectionError(f"Relay connection lost ({type(exc).__name__}: {exc})") from None
            if status == 200:
                log.info("relay tunnel open for %s via %s", slot.device_id, current.relay_host)
                return sock
            sock.close()
            try:
                reply = json.loads(body.decode("utf-8")) if body else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                reply = {}
            reply = reply if isinstance(reply, dict) else {}
            if status == 304:
                result = reply.get("result") if isinstance(reply.get("result"), dict) else reply
                current = RelaySlot.from_result(result, device_id=slot.device_id, track_id=slot.track_id,
                                                resolution=slot.resolution)
                log.info("relay tunnel for %s redirected to %s", slot.device_id, current.relay_host)
                continue
            code = _int(reply.get("errorCode"))
            reason = str(reply.get("closeReason") or reply.get("msg") or "")[:120]
            log.info("relay tunnel for %s refused: HTTP %s, errorCode %s (%s)", slot.device_id, status, code, reason)
            raise _error(code if code is not None else status, reason, "relay")
        raise RelayError(f"The TP-Link relay redirected more than {MAX_REDIRECTS} times.", "too_many_redirects")


class RelayMediaSession(MediaSession):
    """The :class:`MediaSession` surface (``connect`` / ``send_json`` / ``send_ack`` / ``parts`` /
    ``session_id`` / ``close``) over a relay tunnel, for :class:`LocalStreamSupervisor`.

    ``connect()`` requests a fresh slot and opens the tunnel (no inner ``POST /stream``, no digest) and
    starts the heartbeat thread (``heartbeat_interval`` s, default 15). Media parts are plaintext; a part
    flagged ``X-If-Encrypt: 1`` raises TapoProtocolError. One-shot: after ``close()`` it cannot connect.
    """

    def __init__(self, relay: RelayClient, device_id: str, *, device_type: str = "SMART.IPCAMERA",
                 model: str = "", resolution: str = "VGA", cloud_type: int = 2, timeout: float = 10.0,
                 heartbeat_interval: float = HEARTBEAT_INTERVAL_S):
        if heartbeat_interval <= 0:
            raise ValueError("heartbeat_interval must be positive")
        super().__init__(device_id, "", port=RELAY_PORT, timeout=timeout)
        self._relay = relay
        self._device_id = device_id
        self._device_type = device_type
        self._model = model
        self._resolution = resolution
        self._cloud_type = cloud_type
        self._hb_interval = float(heartbeat_interval)
        self._hb_stop = threading.Event()
        self._hb_thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._aborted = False

    def __repr__(self) -> str:
        return (f"RelayMediaSession(device={self._device_id!r}, resolution={self._resolution}, "
                f"connected={self._sock is not None})")

    def connect(self) -> None:
        """Slot request + tunnel; raises RelayError / TapoConnectionError (closed meanwhile -> TapoConnectionError)."""
        if self._sock is not None:
            raise TapoProtocolError("relay session is already connected")
        self._check_open()
        self._session_id = None
        self._keys = None
        self._hkdf_keys = None
        slot = self._relay.request_slot(self._device_id, self._device_type, self._model, self._resolution,
                                        cloud_type=self._cloud_type)
        self._check_open()
        sock = self._relay.open_tunnel(slot)
        with self._state_lock:
            if self._aborted:
                sock.close()
                raise TapoConnectionError("relay session closed")
            sock.settimeout(self._timeout)
            self._rx.clear()
            self._sock = sock
            self._hb_thread = threading.Thread(target=self._heartbeat_loop, name=f"relay-heartbeat-{self._device_id}",
                                               daemon=True)
        self._hb_thread.start()

    def _check_open(self) -> None:
        with self._state_lock:
            if self._aborted:
                raise TapoConnectionError("relay session closed")

    def send_heartbeat(self) -> None:
        """Write one heartbeat part [spec:remote.relay.heartbeat]."""
        try:
            self._sendall(encode_client_part(HEARTBEAT_BODY))
        except OSError as exc:
            raise TapoConnectionError(f"relay connection lost: {type(exc).__name__}") from None

    def _heartbeat_loop(self) -> None:
        while not self._hb_stop.wait(self._hb_interval):
            try:
                self.send_heartbeat()
            except TapoError:
                return

    def _decode(self, part: MediaPart) -> MediaPart | None:
        if part.headers.get("X-If-Encrypt") == "1":
            raise TapoProtocolError("The relay sent an encrypted media part; encrypted relay streams are not supported.")
        return super()._decode(part)

    def close(self) -> None:
        """Stop the heartbeat and close the tunnel. Idempotent."""
        with self._state_lock:
            self._aborted = True
            thread = self._hb_thread
        self._hb_stop.set()
        super().close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(1.0)
