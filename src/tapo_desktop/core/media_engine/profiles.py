"""RTSP URL building for Tapo firmware streams  [spec:stream.rtsp.hd, stream.rtsp.sd, stream.rtsp.multilens]."""
from __future__ import annotations

from urllib.parse import quote

from tapo_desktop.models import StreamQuality

__all__ = ["build_rtsp_url", "redact_url"]

_REDACTED = "***"


def _format_host(host: str) -> str:
    """Bracket IPv6 literals exactly once."""
    host = host.strip()
    if host.startswith("[") and host.endswith("]"):
        return host
    return f"[{host}]" if ":" in host else host


def build_rtsp_url(
    host: str,
    username: str,
    password: str,
    quality: StreamQuality = StreamQuality.HD,
    port: int = 554,
) -> str:
    """Return ``rtsp://<user>:<pass>@<host>:<port>/<quality.value>``.

    * ``username`` and ``password`` are percent-encoded with no safe characters
      (``urllib.parse.quote(s, safe="")``), so ``@ : / % #`` and spaces never appear raw.
    * IPv6 literal hosts are wrapped in brackets (``[fe80::1]``); an already-bracketed host is
      not bracketed twice.
    * ``quality`` selects the path: stream1 (HD), stream2 (SD), stream6/stream7 (second lens).
    """
    path = StreamQuality(quality).value
    user = quote(username, safe="")
    secret = quote(password, safe="")
    return f"rtsp://{user}:{secret}@{_format_host(host)}:{int(port)}/{path}"


def redact_url(url: str) -> str:
    """Return ``url`` with the password part of the userinfo replaced by ``***``.

    URLs without a password are returned unchanged. Safe to use in every log message.
    The last ``@`` ends the userinfo, so a password holding raw ``@ / # ?`` is still redacted.
    """
    scheme_end = url.find("://")
    if scheme_end < 0:
        return url
    start = scheme_end + 3
    at = url.rfind("@", start)
    if at < 0:
        return url
    user, sep, _password = url[start:at].partition(":")
    if not sep or any(ch in user for ch in "/?#"):
        return url
    return f"{url[:start]}{user}:{_REDACTED}{url[at:]}"
