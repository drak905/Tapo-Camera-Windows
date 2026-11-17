"""RTSP URL building for Tapo firmware streams  [spec:stream.rtsp.hd, stream.rtsp.sd, stream.rtsp.multilens].

Wave 2 stub: signatures and docstrings only (see docs/CONTRACT_MEDIA.md).
"""
from __future__ import annotations

from tapo_desktop.models import StreamQuality

__all__ = ["build_rtsp_url", "redact_url"]


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
    raise NotImplementedError


def redact_url(url: str) -> str:
    """Return ``url`` with the password part of the userinfo replaced by ``***``.

    URLs without a password are returned unchanged. Safe to use in every log message.
    """
    raise NotImplementedError
