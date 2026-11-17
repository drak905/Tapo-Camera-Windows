"""Minimal SOAP 1.2 / WS-Security client for the camera's ONVIF service (port 2020) [spec:onvif.port]."""
from __future__ import annotations

import base64
import hashlib
import http.client
import logging
import secrets
import select
import threading
import time
import xml.etree.ElementTree
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from tapo_desktop.core.errors import TapoAuthError, TapoConnectionError, TapoDeviceError, TapoProtocolError
from tapo_desktop.core.onvif_service import _xml as X

logger = logging.getLogger(__name__)

_PASSWORD_DIGEST = ("http://docs.oasis-open.org/wss/2004/01/"
                    "oasis-200401-wss-username-token-profile-1.0#PasswordDigest")
_BASE64_BINARY = ("http://docs.oasis-open.org/wss/2004/01/"
                  "oasis-200401-wss-soap-message-security-1.0#Base64Binary")
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_POLL_INTERVAL_S = 0.2
_DEVICE_INFO_KEYS = ("Manufacturer", "Model", "FirmwareVersion", "SerialNumber", "HardwareId")


def wsse_header(username: str, password: str, *, created: str | None = None, nonce: bytes | None = None) -> str:
    """WS-Security UsernameToken header XML.

    PasswordDigest = base64(sha1(nonce + created + password)); ``created`` defaults to the current UTC
    time (``YYYY-MM-DDTHH:MM:SSZ``), ``nonce`` to 16 random bytes. Namespaces are declared on the
    header element itself so the fragment is self-contained.
    """
    if created is None:
        created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if nonce is None:
        nonce = secrets.token_bytes(16)
    digest = base64.b64encode(
        hashlib.sha1(nonce + created.encode("utf-8") + password.encode("utf-8")).digest()).decode("ascii")
    return (f'<wsse:Security xmlns:wsse="{X.WSSE}" xmlns:wsu="{X.WSU}" xmlns:s="{X.SOAP12}" s:mustUnderstand="1">'
            f"<wsse:UsernameToken><wsse:Username>{escape(username)}</wsse:Username>"
            f'<wsse:Password Type="{_PASSWORD_DIGEST}">{digest}</wsse:Password>'
            f'<wsse:Nonce EncodingType="{_BASE64_BINARY}">{base64.b64encode(nonce).decode("ascii")}</wsse:Nonce>'
            f"<wsu:Created>{escape(created)}</wsu:Created></wsse:UsernameToken></wsse:Security>")


def build_envelope(body_xml: str, header_xml: str = "") -> str:
    """SOAP 1.2 Envelope (``http://www.w3.org/2003/05/soap-envelope``) with Header and Body."""
    return (f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="{X.SOAP12}">'
            f"<s:Header>{header_xml}</s:Header><s:Body>{body_xml}</s:Body></s:Envelope>")


def _fault_message(fault: xml.etree.ElementTree.Element, op: str) -> str:
    """Human-readable summary of a SOAP 1.2 / 1.1 Fault (subcode + reason, truncated)."""
    codes = [(v.text or "").strip() for v in X.iter_local(fault, "Value") if (v.text or "").strip()]
    code = codes[-1] if codes else X.text_local(fault, "faultcode")
    reason = X.text_local(fault, "Text") or X.text_local(fault, "faultstring")
    detail = ": ".join(p for p in (code, reason) if p)[:200]
    return f"ONVIF SOAP fault in {op or 'request'}" + (f": {detail}" if detail else "")


class OnvifClient:
    """Authenticated ONVIF SOAP client (camera account credentials).

    Every call opens its own short-lived HTTP connection, so one client may be shared by
    several threads (e.g. PTZ commands while a PullPoint subscriber long-polls).
    """

    def __init__(self, host: str, username: str, password: str, *, port: int = 2020, timeout: float = 5.0):
        self._host = host
        self._port = int(port)
        self._username = username
        self._password = password
        self._timeout = float(timeout)
        self._lock = threading.Lock()
        self._capabilities: dict[str, str] | None = None

    def __repr__(self) -> str:
        return f"OnvifClient(host={self._host!r}, port={self._port}, username={self._username!r})"

    __str__ = __repr__

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def username(self) -> str:
        return self._username

    @property
    def timeout(self) -> float:
        """Network timeout (connect and per-read) in seconds."""
        return self._timeout

    # ------------------------------------------------------------------------------- transport
    def call(self, path: str, body_xml: str, *, header_xml: str = "", timeout: float | None = None,
             cancel: threading.Event | None = None) -> xml.etree.ElementTree.Element:
        """POST a SOAP request to ``http://host:port{path}`` and return the first child of Body.

        SOAP Fault -> TapoDeviceError(code=-1); network failure -> TapoConnectionError;
        unparsable response -> TapoProtocolError.

        Optional extras: ``header_xml`` is appended to the SOAP Header after the WS-Security
        token; ``timeout`` is the total time allowed for the response to start (default: the
        client timeout; long polls pass a larger value); setting ``cancel`` aborts the wait with
        TapoConnectionError within ~0.2 s.
        """
        if not path.startswith("/"):
            path = "/" + path
        op = X.operation_name(body_xml)
        payload = build_envelope(body_xml, wsse_header(self._username, self._password) + header_xml).encode("utf-8")
        status, data = self._post(path, payload, op, self._timeout if timeout is None else timeout, cancel)
        return self._parse_response(status, data, op)

    def _post(self, path: str, payload: bytes, op: str, wait_s: float,
              cancel: threading.Event | None) -> tuple[int, bytes]:
        conn = http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)
        try:
            conn.request("POST", path, body=payload, headers={
                "Content-Type": "application/soap+xml; charset=utf-8", "Connection": "close"})
            self._await_response(conn, time.monotonic() + max(0.0, wait_s), cancel)
            resp = conn.getresponse()
            data = resp.read(_MAX_RESPONSE_BYTES + 1)
            return resp.status, data
        except (OSError, http.client.HTTPException) as exc:
            raise TapoConnectionError(
                f"ONVIF {op or 'request'} to {self._host}:{self._port} failed: {type(exc).__name__}: {exc}") from exc
        finally:
            conn.close()

    def _await_response(self, conn: http.client.HTTPConnection, deadline: float,
                        cancel: threading.Event | None) -> None:
        """Wait until response bytes are readable, honouring ``deadline`` and ``cancel``."""
        sock = conn.sock
        if sock is None:
            raise TapoConnectionError("ONVIF connection closed before the response")
        while True:
            if cancel is not None and cancel.is_set():
                raise TapoConnectionError("ONVIF request cancelled")
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("timed out waiting for the ONVIF response")
            readable, _w, _x = select.select([sock], [], [], min(_POLL_INTERVAL_S, left))
            if readable:
                return

    def _parse_response(self, status: int, data: bytes, op: str) -> xml.etree.ElementTree.Element:
        if len(data) > _MAX_RESPONSE_BYTES:
            raise TapoProtocolError(f"ONVIF {op} response exceeds {_MAX_RESPONSE_BYTES} bytes")
        root = X.safe_parse(data)
        body = X.soap_body(root) if root is not None else None
        if body is None:
            if status == 401:
                raise TapoAuthError(f"ONVIF {op}: HTTP 401 unauthorized")
            raise TapoProtocolError(f"ONVIF {op}: unparsable SOAP response (HTTP {status})")
        child = next(iter(body), None)
        if child is not None and X.local(child.tag) == "Fault":
            raise TapoDeviceError(-1, op or None, _fault_message(child, op))
        if not 200 <= status < 300:
            raise TapoProtocolError(f"ONVIF {op}: HTTP {status} without a SOAP Fault")
        if child is None:
            raise TapoProtocolError(f"ONVIF {op}: empty SOAP Body")
        return child

    # ------------------------------------------------------------------------------- device / media
    def get_device_information(self) -> dict:
        """Manufacturer, Model, FirmwareVersion, SerialNumber, HardwareId."""
        resp = self.call(X.DEVICE_SERVICE_PATH, f'<tds:GetDeviceInformation xmlns:tds="{X.TDS}"/>')
        return {key: X.text_local(resp, key) for key in _DEVICE_INFO_KEYS}

    def get_capabilities(self) -> dict[str, str]:
        """Service XAddrs by lowercase category: ``{"ptz": url, "events": url, "media": url, ...}``."""
        resp = self.call(X.DEVICE_SERVICE_PATH, f'<tds:GetCapabilities xmlns:tds="{X.TDS}">'
                                                "<tds:Category>All</tds:Category></tds:GetCapabilities>")
        caps_el = X.find_local(resp, "Capabilities")
        caps: dict[str, str] = {}
        if caps_el is not None:
            for el in caps_el.iter():
                if el is caps_el or X.local(el.tag) in ("Capabilities", "Extension"):
                    continue
                xaddr = X.child_local(el, "XAddr")
                if xaddr is not None and (xaddr.text or "").strip():
                    caps.setdefault(X.local(el.tag).lower(), xaddr.text.strip())
        with self._lock:
            self._capabilities = dict(caps)
        return caps

    def get_profiles(self) -> list[dict]:
        """Media profiles ``[{"token", "name"}]``."""
        resp = self.call(self._service_path("media"), f'<trt:GetProfiles xmlns:trt="{X.TRT}"/>')
        profiles: list[dict] = []
        for el in X.iter_local(resp, "Profiles"):
            token = (el.get("token") or "").strip()
            if token:
                name = X.child_local(el, "Name")
                profiles.append({"token": token, "name": (name.text or "").strip() if name is not None else ""})
        return profiles

    def _service_path(self, category: str) -> str:
        """Path of a service from previously fetched capabilities, else the Tapo default."""
        with self._lock:
            url = (self._capabilities or {}).get(category, "")
        return X.url_path(url) if url else X.DEFAULT_SERVICE_PATH


def service_path(client: object, category: str) -> str:
    """Resolve the request path of service ``category`` through ``client.get_capabilities()``.

    Only the path (+ query) of the advertised XAddr is used; requests always go to the
    client's own host/port (works through NAT / port forwarding / proxies). Falls back to
    ``/onvif/service`` when the camera does not advertise the service. Connection errors propagate.
    """
    try:
        caps = client.get_capabilities()  # type: ignore[attr-defined]
    except (TapoDeviceError, TapoProtocolError) as exc:
        logger.debug("GetCapabilities failed (%s); using %s", exc, X.DEFAULT_SERVICE_PATH)
        return X.DEFAULT_SERVICE_PATH
    url = caps.get(category, "") if isinstance(caps, dict) else ""
    return X.url_path(url) if url else X.DEFAULT_SERVICE_PATH
