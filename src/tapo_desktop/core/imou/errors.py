"""iMou error types and the vendor-code map.

Every message starts with ``iMou: `` (the camera tile uses that to tell iMou texts from others). Messages never
carry a URL: vendor text is cleaned of anything that looks like one (a live address is a secret).
"""
from __future__ import annotations

import re

from tapo_desktop.core.errors import TapoError

PREFIX = "iMou: "
QUOTA_MESSAGE = "free quota used. iMou calls stop until tomorrow (UTC)."
AUTH_MESSAGE = "check the App ID and App secret."
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")


def clean(text: str) -> str:
    """Vendor text without URLs, on one line, at most 200 characters."""
    return " ".join(_URL.sub("<url>", str(text or "")).split())[:200]


class ImouError(TapoError):
    """Base of every iMou error. ``code`` is the vendor ``result.code`` (``""`` when there is none)."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message if message.startswith(PREFIX) else PREFIX + message)
        self.code = code


class ImouConnectionError(ImouError):
    """The iMou server could not be reached (or answered with an HTTP error)."""


class ImouProtocolError(ImouError):
    """A reply that does not have the documented shape."""


class ImouNotAllowedError(ImouError):
    """The code tried to build a request outside the strict allowlist (a bug, never a user error)."""


class ImouBlockedError(ImouError):
    """Calls are stopped on purpose (quota used, or a signature error): nothing was sent."""

    def __init__(self, message: str, until: float = 0.0, code: str = ""):
        super().__init__(message, code)
        self.until = until


class ImouAuthError(ImouError):
    """SN1001 and friends: the App ID / App secret are not accepted. Never retried."""


class ImouTokenExpiredError(ImouError):
    """TK1002 after one refresh and one retry."""


class ImouOfflineError(ImouError):
    """DV1007: the camera is offline. Never retried."""


class ImouQuotaError(ImouError):
    """FL1002/FL1004/FL1005, OP1013/OP1014: the free flow or call quota is used."""


class ImouVendorError(ImouError):
    """LV1008 / OP1009 and other refusals: the vendor's own text is shown."""


class ImouLiveExistsError(ImouVendorError):
    """LV1001: a live address already exists for this camera."""


#: Codes after which the RTSP call counts as "refused" (the HLS live address is tried instead).
RTSP_REFUSED_CODES = frozenset({"OP1005", "OP1012", "OP1015", "DV1019", "DV1026"})
QUOTA_CODES = frozenset({"FL1002", "FL1004", "FL1005", "OP1013", "OP1014"})
AUTH_CODES = frozenset({"SN1001", "SN1003", "SN1004"})


def error_for(code: str, msg: str = "") -> ImouError:
    """The exception for a vendor ``code`` (never ``"0"``)."""
    code, text = str(code), clean(msg)
    if code in QUOTA_CODES:
        return ImouQuotaError(f"{QUOTA_MESSAGE} ({code})", code)
    if code in AUTH_CODES:
        return ImouAuthError(f"{AUTH_MESSAGE} ({code})", code)
    if code == "SN1002":
        return ImouAuthError("the PC clock is more than 5 minutes off. Fix the Windows time (SN1002).", code)
    if code == "SN1005":
        return ImouProtocolError("duplicate request id, try again (SN1005).", code)
    if code == "TK1002":
        return ImouTokenExpiredError("the access token is no longer valid (TK1002).", code)
    if code == "DV1007":
        return ImouOfflineError("the camera is offline (DV1007).", code)
    if code == "LV1001":
        return ImouLiveExistsError(f"a live address already exists (LV1001). {text}".strip(), code)
    return ImouVendorError(f"{text or 'the request was refused'} ({code})", code)
