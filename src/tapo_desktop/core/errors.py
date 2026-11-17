"""Exception hierarchy shared by all core subsystems (contract; see docs/ARCHITECTURE.md)."""
from __future__ import annotations


class TapoError(Exception):
    """Base class for every error raised by tapo_desktop.core."""


class TapoConnectionError(TapoError):
    """Network-level failure: refused, reset, timeout, TLS failure, malformed HTTP."""


class CertificateMismatchError(TapoConnectionError):
    """The camera's TLS certificate fingerprint differs from the TOFU-pinned one."""

    def __init__(self, host: str, expected: str, actual: str):
        super().__init__(f"TLS certificate for {host} changed (pinned {expected[:16]}..., got {actual[:16]}...)")
        self.host = host
        self.expected = expected
        self.actual = actual


class TapoAuthError(TapoError):
    """Bad credentials or failed device_confirm check."""


class TapoLockedError(TapoAuthError):
    """Camera temporarily blocks logins after too many failures (e.g. -40404)."""


class TapoProtocolError(TapoError):
    """Response could not be parsed / violates the protocol spec."""


class TapoDeviceError(TapoError):
    """Camera returned a non-zero error_code for a command."""

    def __init__(self, code: int, method: str | None = None, message: str | None = None):
        super().__init__(message or f"camera error {code}" + (f" in {method}" if method else ""))
        self.code = code
        self.method = method


class TapoSessionExpired(TapoDeviceError):
    """-40401: stok expired. The control client handles this internally by re-login."""

    def __init__(self, method: str | None = None):
        super().__init__(-40401, method, "session expired (-40401)")


class StreamLimitError(TapoError):
    """Refused to open another stream: per-camera concurrency cap reached (camera allows 2)."""
