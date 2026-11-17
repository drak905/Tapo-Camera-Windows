"""ITU-T G.711 A-law codec (camera audio is PCMA, 8 kHz mono).

Table driven: decoding uses two ``bytes.translate`` tables (low/high byte of each sample) and
encoding uses one 64 Ki entry table indexed by the unsigned 16-bit sample, so both directions run
in C loops with no per-sample Python arithmetic.
"""
from __future__ import annotations

import sys
from array import array

__all__ = ["alaw_to_pcm16", "pcm16_to_alaw"]

_SEG_END = (0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF)


def _alaw_to_linear(code: int) -> int:
    """Reference G.711 A-law expansion of one code to a signed 16-bit sample."""
    code ^= 0x55
    mantissa = (code & 0x0F) << 4
    segment = (code & 0x70) >> 4
    if segment == 0:
        mantissa += 8
    elif segment == 1:
        mantissa += 0x108
    else:
        mantissa = (mantissa + 0x108) << (segment - 1)
    return mantissa if code & 0x80 else -mantissa


def _linear_to_alaw(sample: int) -> int:
    """Reference G.711 A-law compression of one signed 16-bit sample."""
    pcm = sample >> 3  # 13-bit magnitude domain (arithmetic shift)
    if pcm >= 0:
        mask = 0xD5
    else:
        mask = 0x55
        pcm = -pcm - 1
    for segment, end in enumerate(_SEG_END):
        if pcm <= end:
            break
    else:
        return 0x7F ^ mask
    value = segment << 4
    value |= ((pcm >> 1) if segment < 2 else (pcm >> segment)) & 0x0F
    return value ^ mask


def _build_tables() -> tuple[bytes, bytes, bytes]:
    decoded = [_alaw_to_linear(c) & 0xFFFF for c in range(256)]
    low = bytes(v & 0xFF for v in decoded)
    high = bytes(v >> 8 for v in decoded)
    # index = unsigned 16-bit view of the sample
    encode = bytes(_linear_to_alaw(u - 0x10000 if u >= 0x8000 else u) for u in range(0x10000))
    return low, high, encode


_DECODE_LOW, _DECODE_HIGH, _ENCODE = _build_tables()
_BIG_ENDIAN_HOST = sys.byteorder == "big"


def alaw_to_pcm16(data: bytes) -> bytes:
    """Decode A-law bytes to signed 16-bit little-endian PCM (2 output bytes per input byte)."""
    raw = bytes(data)
    out = bytearray(2 * len(raw))
    out[0::2] = raw.translate(_DECODE_LOW)
    out[1::2] = raw.translate(_DECODE_HIGH)
    return bytes(out)


def pcm16_to_alaw(data: bytes) -> bytes:
    """Encode s16le PCM to A-law (1 byte per sample). An odd trailing byte is ignored."""
    raw = bytes(data)
    samples = array("H")
    samples.frombytes(raw[: len(raw) & ~1])
    if _BIG_ENDIAN_HOST:
        samples.byteswap()
    return bytes(map(_ENCODE.__getitem__, samples))
