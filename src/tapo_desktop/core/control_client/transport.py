"""HTTPS transport to the camera's :443 API with TOFU certificate pinning [spec:auth.tls.trust]."""
from __future__ import annotations

import pathlib


class PinStore:
    """Trust-on-first-use certificate pins keyed by (host, port).

    Fingerprints are lowercase sha256 hex of the DER certificate. ``path=None`` keeps pins in memory;
    otherwise they persist in a JSON file.
    """

    def __init__(self, path: pathlib.Path | None = None):
        raise NotImplementedError

    def get(self, host: str, port: int) -> str | None:
        """Pinned fingerprint for ``host:port`` or ``None``."""
        raise NotImplementedError

    def set(self, host: str, port: int, fingerprint: str) -> None:
        """Pin ``fingerprint`` for ``host:port`` (replaces any previous pin)."""
        raise NotImplementedError

    def forget(self, host: str, port: int) -> None:
        """Remove the pin for ``host:port`` (no error if absent)."""
        raise NotImplementedError


class HttpsTransport:
    """Keep-alive HTTPS client for one camera.

    The certificate chain is not validated (cameras are self-signed); instead the leaf certificate
    fingerprint is pinned on first contact and must match afterwards.
    """

    def __init__(self, host: str, port: int = 443, *, pin_store: PinStore | None = None, timeout: float = 10.0):
        raise NotImplementedError

    def post(self, path: str, body: str, headers: dict[str, str]) -> tuple[int, dict[str, str], str]:
        """POST ``body`` to ``path``; return ``(status, response_headers, response_text)``.

        Reuses the keep-alive connection; reconnects once on a stale socket. Raises
        TapoConnectionError on network/TLS failure or timeout, CertificateMismatchError when the
        certificate differs from the pinned one (first contact pins automatically).
        """
        raise NotImplementedError

    @property
    def fingerprint(self) -> str | None:
        """Fingerprint (lowercase sha256 hex of DER) of the certificate seen on the last connection."""
        raise NotImplementedError

    def close(self) -> None:
        """Close the connection (idempotent)."""
        raise NotImplementedError
