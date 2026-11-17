"""ITU-T G.711 A-law codec (camera audio is PCMA, 8 kHz mono).

Wave 2 stub: signatures and docstrings only (see docs/CONTRACT_MEDIA.md).
"""
from __future__ import annotations

__all__ = ["alaw_to_pcm16", "pcm16_to_alaw"]


def alaw_to_pcm16(data: bytes) -> bytes:
    """Decode A-law bytes to signed 16-bit little-endian PCM (2 output bytes per input byte)."""
    raise NotImplementedError


def pcm16_to_alaw(data: bytes) -> bytes:
    """Encode s16le PCM to A-law (1 byte per sample). An odd trailing byte is ignored."""
    raise NotImplementedError
