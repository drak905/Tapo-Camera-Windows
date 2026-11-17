"""HTTPS transport to the camera's :443 API with TOFU certificate pinning [spec:auth.tls.trust]."""
from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import logging
import pathlib
import select
import socket
import ssl
import threading

from tapo_desktop.core.auth._atomic import atomic_write_text, read_json_object
from tapo_desktop.core.errors import CertificateMismatchError, TapoConnectionError

log = logging.getLogger(__name__)

# Errors on a *reused* keep-alive socket that mean "the peer dropped it": reconnect and resend once.
# Timeouts are deliberately excluded (a blackholed camera must fail within one timeout).
_STALE_ERRORS = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, ssl.SSLEOFError,
                 ssl.SSLZeroReturnError, http.client.RemoteDisconnected, http.client.BadStatusLine,
                 http.client.IncompleteRead, http.client.CannotSendRequest, http.client.ResponseNotReady)


def _key(host: str, port: int) -> str:
    return f"{host}:{int(port)}"


def _peer_closed(sock: socket.socket) -> bool:
    """True if an idle socket is readable (EOF / RST / close_notify pending) or unusable."""
    try:
        readable, _, _ = select.select([sock], [], [], 0)
    except (OSError, ValueError):
        return True
    return bool(readable)


class PinStore:
    """Trust-on-first-use certificate pins keyed by (host, port).

    Fingerprints are lowercase sha256 hex of the DER certificate. ``path=None`` keeps pins in memory;
    otherwise they persist in a JSON file (``{"pins": {"host:port": fingerprint}}``, atomic writes).
    Thread-safe; one instance may be shared by many transports.
    """

    def __init__(self, path: pathlib.Path | None = None):
        self._path = pathlib.Path(path) if path is not None else None
        self._lock = threading.Lock()
        self._pins: dict[str, str] = {}
        if self._path is not None:
            pins = read_json_object(self._path).get("pins")
            if isinstance(pins, dict):
                self._pins = {k: v.lower() for k, v in pins.items() if isinstance(k, str) and isinstance(v, str)}

    def __repr__(self) -> str:
        return f"PinStore(path={None if self._path is None else str(self._path)!r}, pins={len(self._pins)})"

    def get(self, host: str, port: int) -> str | None:
        """Pinned fingerprint for ``host:port`` or ``None``."""
        with self._lock:
            return self._pins.get(_key(host, port))

    def set(self, host: str, port: int, fingerprint: str) -> None:
        """Pin ``fingerprint`` for ``host:port`` (replaces any previous pin)."""
        with self._lock:
            self._pins[_key(host, port)] = fingerprint.strip().lower()
            self._persist()

    def forget(self, host: str, port: int) -> None:
        """Remove the pin for ``host:port`` (no error if absent)."""
        with self._lock:
            if self._pins.pop(_key(host, port), None) is not None:
                self._persist()

    def _persist(self) -> None:
        if self._path is not None:
            atomic_write_text(self._path, json.dumps({"pins": self._pins}, indent=2, sort_keys=True))


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection that runs the TOFU check right after every (re)connect."""

    def __init__(self, host: str, port: int, *, timeout: float, context: ssl.SSLContext, on_connect):
        super().__init__(host, port, timeout=timeout, context=context)
        self._on_connect = on_connect

    def connect(self) -> None:
        super().connect()
        try:
            self._on_connect(self.sock.getpeercert(binary_form=True))
        except BaseException:
            self.close()
            raise


class HttpsTransport:
    """Keep-alive HTTPS client for one camera.

    The certificate chain is not validated (cameras are self-signed); instead the leaf certificate
    fingerprint is pinned on first contact and must match afterwards. Thread-safe: an internal lock
    serialises requests on the single keep-alive connection.
    """

    def __init__(self, host: str, port: int = 443, *, pin_store: PinStore | None = None, timeout: float = 10.0):
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)
        self._pins = pin_store if pin_store is not None else PinStore()
        self._ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self._ctx.check_hostname = False
        self._ctx.verify_mode = ssl.CERT_NONE  # self-signed camera cert; trust comes from the pin
        self._conn: _PinnedHTTPSConnection | None = None
        self._fingerprint: str | None = None
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"HttpsTransport(host={self.host!r}, port={self.port})"

    @property
    def fingerprint(self) -> str | None:
        """Fingerprint (lowercase sha256 hex of DER) of the certificate seen on the last connection."""
        return self._fingerprint

    def _check_pin(self, der: bytes | None) -> None:
        if not der:
            raise TapoConnectionError(f"{self.host}:{self.port} presented no TLS certificate")
        actual = hashlib.sha256(der).hexdigest()
        self._fingerprint = actual
        pinned = self._pins.get(self.host, self.port)
        if pinned is None:
            log.info("pinning TLS certificate of %s:%s (sha256 %s...)", self.host, self.port, actual[:16])
            self._pins.set(self.host, self.port, actual)
        elif pinned != actual:
            raise CertificateMismatchError(self.host, pinned, actual)

    def _connection(self) -> tuple[_PinnedHTTPSConnection, bool]:
        """Return ``(connection, reused)``; ``reused`` is True if its socket is already open.

        An idle keep-alive socket that is readable has been closed or reset by the peer (no
        response is outstanding), so it is dropped up front instead of writing into a dead socket.
        """
        if self._conn is not None and self._conn.sock is not None and _peer_closed(self._conn.sock):
            log.debug("keep-alive connection to %s:%s was closed by the peer", self.host, self.port)
            self._drop()
        if self._conn is None:
            self._conn = _PinnedHTTPSConnection(self.host, self.port, timeout=self.timeout, context=self._ctx,
                                                on_connect=self._check_pin)
        return self._conn, self._conn.sock is not None

    def _drop(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            with contextlib.suppress(OSError):
                conn.close()

    def _once(self, conn: _PinnedHTTPSConnection, path: str, body: bytes,
              headers: dict[str, str]) -> tuple[int, dict[str, str], bytes, str]:
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        if resp.will_close:
            self._drop()
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.status, {k: v for k, v in resp.getheaders()}, data, charset

    def post(self, path: str, body: str, headers: dict[str, str]) -> tuple[int, dict[str, str], str]:
        """POST ``body`` to ``path``; return ``(status, response_headers, response_text)``.

        Reuses the keep-alive connection; reconnects once on a stale socket. Raises
        TapoConnectionError on network/TLS failure or timeout, CertificateMismatchError when the
        certificate differs from the pinned one (first contact pins automatically).
        """
        hdrs = {"Content-Type": "application/json; charset=UTF-8", "Connection": "keep-alive", **(headers or {})}
        status, resp_headers, data, charset = self._request(path, body.encode("utf-8"), hdrs)
        try:
            text = data.decode(charset, errors="replace")
        except LookupError:
            text = data.decode("utf-8", errors="replace")
        return status, resp_headers, text

    def post_bytes(self, path: str, body: bytes, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
        """Binary POST (TPAP ``/ds``): ``application/octet-stream`` unless ``headers`` override it.

        Same connection reuse, pinning, timeout and error behaviour as :meth:`post`; returns
        ``(status, response_headers, response_bytes)``.
        """
        hdrs = {"Content-Type": "application/octet-stream", "Accept": "application/octet-stream",
                "Connection": "keep-alive", **(headers or {})}
        status, resp_headers, data, _charset = self._request(path, bytes(body), hdrs)
        return status, resp_headers, data

    def _request(self, path: str, payload: bytes,
                 hdrs: dict[str, str]) -> tuple[int, dict[str, str], bytes, str]:
        with self._lock:
            for attempt in (1, 2):
                conn, reused = self._connection()
                try:
                    return self._once(conn, path, payload, hdrs)
                except TapoConnectionError:  # pin mismatch / no certificate, raised from connect()
                    self._drop()
                    raise
                except _STALE_ERRORS as exc:
                    self._drop()
                    if reused and attempt == 1:
                        log.debug("stale keep-alive connection to %s:%s (%s); reconnecting",
                                  self.host, self.port, type(exc).__name__)
                        continue
                    raise TapoConnectionError(self._describe(exc)) from exc
                except (OSError, http.client.HTTPException) as exc:
                    self._drop()
                    raise TapoConnectionError(self._describe(exc)) from exc
        raise AssertionError("unreachable")  # pragma: no cover


    def _describe(self, exc: BaseException) -> str:
        kind = "timed out" if isinstance(exc, TimeoutError) else f"failed ({type(exc).__name__})"
        return f"HTTPS request to {self.host}:{self.port} {kind}"

    def close(self) -> None:
        """Close the connection (idempotent). Safe to call from another thread; an in-flight
        request then fails with TapoConnectionError."""
        self._drop()
