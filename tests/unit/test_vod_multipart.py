"""Tier 1: incremental parser of the device multipart stream  [spec:stream.local8800.framing]."""
from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tapo_desktop.core.errors import TapoError, TapoProtocolError
from tapo_desktop.core.vod.session import MediaPart, MultipartReader
from tests.unit._media_helpers import DEVICE_PART_PREFIX, device_part

pytestmark = pytest.mark.unit

JSON_HDR = {"Content-Type": "application/json"}
TS_HDR = {"Content-Type": "video/mp2t", "X-Data-PTS": "90000", "X-If-IFrame": "1", "X-Data-Sequence": "1"}


def test_single_part():
    r = MultipartReader()
    parts = r.feed(device_part(JSON_HDR, b'{"a":1}'))
    assert len(parts) == 1
    assert isinstance(parts[0], MediaPart)
    assert parts[0].body == b'{"a":1}'
    assert parts[0].headers["Content-Type"] == "application/json"
    assert parts[0].headers["Content-Length"] == "7"
    assert r.buffered == 0


def test_headers_preserved_and_stripped():
    r = MultipartReader()
    [p] = r.feed(DEVICE_PART_PREFIX + b"Content-Type:   video/mp2t  \r\nX-Data-PTS: 12\r\nContent-Length: 3\r\n\r\nabc")
    assert p.headers == {"Content-Type": "video/mp2t", "X-Data-PTS": "12", "Content-Length": "3"}
    assert p.body == b"abc"


def test_two_parts_in_one_feed_with_and_without_trailing_crlf():
    data = device_part(JSON_HDR, b"one", trailing_crlf=False) + device_part(TS_HDR, b"\x47" * 376)
    parts = MultipartReader().feed(data)
    assert [p.body for p in parts] == [b"one", b"\x47" * 376]
    assert parts[1].headers["X-Data-Sequence"] == "1"


def test_body_may_contain_boundary_and_crlf_bytes():
    nasty = b"\r\n----device-stream-boundary--\r\nContent-Length: 1\r\n\r\nX\r\n"
    parts = MultipartReader().feed(device_part(TS_HDR, nasty) + device_part(JSON_HDR, b"{}"))
    assert [p.body for p in parts] == [nasty, b"{}"]


def test_byte_at_a_time():
    data = device_part(JSON_HDR, b"hello") + device_part(TS_HDR, bytes(range(256)))
    r = MultipartReader()
    out = []
    for i in range(len(data)):
        out += r.feed(data[i:i + 1])
    assert [p.body for p in out] == [b"hello", bytes(range(256))]
    assert r.buffered == 0


def test_incomplete_part_is_buffered():
    data = device_part(TS_HDR, b"x" * 100)
    r = MultipartReader()
    assert r.feed(data[:-30]) == []
    assert 0 < r.buffered <= len(data) - 30
    [p] = r.feed(data[-30:])
    assert p.body == b"x" * 100


def test_junk_and_blank_lines_between_parts_are_skipped():
    data = b"\r\n\r\nsome junk line\r\n" + device_part(JSON_HDR, b"a") + b"\r\n\r\n" + device_part(JSON_HDR, b"b")
    assert [p.body for p in MultipartReader().feed(data)] == [b"a", b"b"]


def test_closing_boundary_is_ignored():
    data = device_part(JSON_HDR, b"a") + b"----device-stream-boundary----\r\n"
    assert [p.body for p in MultipartReader().feed(data)] == [b"a"]


def test_empty_body():
    [p] = MultipartReader().feed(device_part(JSON_HDR, b""))
    assert p.body == b""


def test_custom_boundary():
    r = MultipartReader(boundary="--client-stream-boundary--")
    [p] = r.feed(b"----client-stream-boundary--\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}\r\n")
    assert p.body == b"{}"


@pytest.mark.parametrize(
    "header_block",
    [
        b"Content-Type: video/mp2t\r\n",  # missing Content-Length
        b"Content-Length: abc\r\n",
        b"Content-Length: -5\r\n",
        b"Content-Length: 99999999999\r\n",  # > max_part_size
    ],
)
def test_invalid_content_length_raises_protocol_error(header_block):
    with pytest.raises(TapoProtocolError):
        MultipartReader().feed(DEVICE_PART_PREFIX + header_block + b"\r\nabc")


def test_max_part_size_is_enforced():
    r = MultipartReader(max_part_size=1000)
    r.feed(device_part(TS_HDR, b"x" * 1000))
    with pytest.raises(TapoProtocolError):
        r.feed(DEVICE_PART_PREFIX + b"Content-Length: 1001\r\n\r\n")


_header_value = st.text(alphabet=st.characters(min_codepoint=0x21, max_codepoint=0x7E), min_size=1, max_size=12)
_parts = st.lists(
    st.tuples(
        st.dictionaries(st.sampled_from(["Content-Type", "X-Data-PTS", "X-Data-Sequence", "X-Session-Id"]),
                        _header_value, max_size=4),
        st.binary(max_size=600),
        st.booleans(),
    ),
    min_size=1,
    max_size=8,
)


@settings(max_examples=200, deadline=None)
@given(parts=_parts, cuts=st.lists(st.integers(min_value=0, max_value=10_000), max_size=12))
def test_arbitrary_chunking_yields_same_parts(parts, cuts):
    data = b"".join(device_part(h, b, crlf) for h, b, crlf in parts)
    points = sorted({c % (len(data) + 1) for c in cuts})
    chunks, prev = [], 0
    for p in points + [len(data)]:
        chunks.append(data[prev:p])
        prev = p
    r = MultipartReader()
    out = []
    for c in chunks:
        out += r.feed(c)
    assert [p.body for p in out] == [b for _, b, _ in parts]
    for got, (hdrs, body, _) in zip(out, parts):
        for k, v in hdrs.items():
            assert got.headers[k] == v
        assert got.headers["Content-Length"] == str(len(body))


@settings(max_examples=300, deadline=None)
@given(st.lists(st.binary(max_size=300), max_size=6))
def test_garbage_never_raises_foreign_errors(chunks):
    r = MultipartReader(max_part_size=4096)
    try:
        for c in chunks:
            r.feed(DEVICE_PART_PREFIX + c if c[:1] == b"\x00" else c)
    except TapoError:
        pass
