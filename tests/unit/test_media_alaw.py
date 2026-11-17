"""Tier 1: G.711 A-law codec against the ITU-T G.711 reference algorithm (Sun g711.c formulation)."""
from __future__ import annotations

import struct

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tapo_desktop.core.media_engine.alaw import alaw_to_pcm16, pcm16_to_alaw

pytestmark = pytest.mark.unit


# ----------------------------------------------------------------- reference (ITU-T G.711)
def ref_alaw2linear(a: int) -> int:
    a ^= 0x55
    t = (a & 0x0F) << 4
    seg = (a & 0x70) >> 4
    if seg == 0:
        t += 8
    elif seg == 1:
        t += 0x108
    else:
        t += 0x108
        t <<= seg - 1
    return t if (a & 0x80) else -t


_SEG_AEND = (0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF)


def ref_linear2alaw(pcm: int) -> int:
    pcm >>= 3  # 13-bit magnitude domain (arithmetic shift)
    if pcm >= 0:
        mask = 0xD5
    else:
        mask = 0x55
        pcm = -pcm - 1
    seg = 8
    for i, end in enumerate(_SEG_AEND):
        if pcm <= end:
            seg = i
            break
    if seg >= 8:
        return 0x7F ^ mask
    aval = seg << 4
    aval |= ((pcm >> 1) if seg < 2 else (pcm >> seg)) & 0x0F
    return aval ^ mask


def _s16(data: bytes) -> list[int]:
    return list(struct.unpack(f"<{len(data) // 2}h", data))


# ----------------------------------------------------------------- golden values
@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (0xD5, 8),  # +0 step (A-law "silence", positive)
        (0x55, -8),  # -0 step
        (0xAA, 32256),  # positive full scale
        (0x2A, -32256),  # negative full scale
        (0x80, 5504),
        (0x00, -5504),
        (0xD4, 24),
        (0xFF, 848),
    ],
)
def test_decode_golden_values(code, expected):
    assert ref_alaw2linear(code) == expected  # sanity of the in-test reference
    assert alaw_to_pcm16(bytes([code])) == struct.pack("<h", expected)


def test_decode_all_256_codes_match_reference():
    out = alaw_to_pcm16(bytes(range(256)))
    assert len(out) == 512
    assert _s16(out) == [ref_alaw2linear(a) for a in range(256)]


def test_encode_all_int16_match_reference():
    pcm = struct.pack("<65536h", *range(-32768, 32768))
    out = pcm16_to_alaw(pcm)
    assert len(out) == 65536
    expected = bytes(ref_linear2alaw(x) for x in range(-32768, 32768))
    assert out == expected


@pytest.mark.parametrize(
    ("sample", "code"),
    [(0, 0xD5), (-1, 0x55), (32767, 0xAA), (-32768, 0x2A), (8, 0xD5), (5504, 0x80)],
)
def test_encode_golden_values(sample, code):
    assert ref_linear2alaw(sample) == code
    assert pcm16_to_alaw(struct.pack("<h", sample)) == bytes([code])


def test_empty_input():
    assert alaw_to_pcm16(b"") == b""
    assert pcm16_to_alaw(b"") == b""


def test_odd_trailing_byte_is_ignored():
    pcm = struct.pack("<3h", 100, -100, 3000) + b"\x7f"
    out = pcm16_to_alaw(pcm)
    assert out == pcm16_to_alaw(pcm[:-1])
    assert len(out) == 3
    assert pcm16_to_alaw(b"\x01") == b""


def test_truncated_alaw_any_length():
    for n in (1, 2, 3, 159, 160, 161, 1023):
        data = bytes((i * 37) & 0xFF for i in range(n))
        out = alaw_to_pcm16(data)
        assert len(out) == 2 * n
        assert _s16(out) == [ref_alaw2linear(b) for b in data]


def test_accepts_bytearray_and_memoryview():
    data = bytes(range(0, 256, 7))
    assert alaw_to_pcm16(bytearray(data)) == alaw_to_pcm16(data)
    assert alaw_to_pcm16(memoryview(data)) == alaw_to_pcm16(data)
    pcm = alaw_to_pcm16(data)
    assert pcm16_to_alaw(bytearray(pcm)) == pcm16_to_alaw(pcm)


@settings(max_examples=300, deadline=None)
@given(st.binary(max_size=2048))
def test_alaw_decode_then_encode_is_identity(data):
    pcm = alaw_to_pcm16(data)
    assert len(pcm) == 2 * len(data)
    assert pcm16_to_alaw(pcm) == data


@settings(max_examples=300, deadline=None)
@given(st.lists(st.integers(min_value=-32768, max_value=32767), max_size=512))
def test_pcm_round_trip_error_bounded_by_segment_step(samples):
    pcm = struct.pack(f"<{len(samples)}h", *samples)
    back = _s16(alaw_to_pcm16(pcm16_to_alaw(pcm)))
    assert len(back) == len(samples)
    for x, y in zip(samples, back):
        mag = abs(x)
        # quantisation step of the A-law segment containing x (16 .. 1024), half-step rounding
        step = 16 if mag < 512 else 16 << (mag.bit_length() - 9)
        assert abs(x - y) <= step, (x, y)
        assert (x >= 0) == (y > 0) or abs(x) <= 8


def test_encode_is_monotonic_in_decoded_domain():
    xs = list(range(-32768, 32768, 61))
    pcm = struct.pack(f"<{len(xs)}h", *xs)
    decoded = _s16(alaw_to_pcm16(pcm16_to_alaw(pcm)))
    assert decoded == sorted(decoded)


def test_speed_one_second_of_audio_is_fast():
    import time

    data = bytes(range(256)) * 32  # ~1 s at 8 kHz
    t0 = time.perf_counter()
    for _ in range(10):
        pcm16_to_alaw(alaw_to_pcm16(data))
    assert time.perf_counter() - t0 < 0.5  # 10 s of audio both ways in < 0.5 s
