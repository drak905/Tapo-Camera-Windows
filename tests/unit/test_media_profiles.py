"""Tier 1: RTSP URL building and redaction  [spec:stream.rtsp.hd, stream.rtsp.sd, stream.rtsp.multilens]."""
from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tapo_desktop.core.media_engine.profiles import build_rtsp_url, redact_url
from tapo_desktop.models import StreamQuality

pytestmark = pytest.mark.unit


def test_default_is_hd_stream1_port_554():
    assert build_rtsp_url("192.168.1.10", "camuser", "campass") == "rtsp://camuser:campass@192.168.1.10:554/stream1"


@pytest.mark.parametrize(
    ("quality", "path"),
    [
        (StreamQuality.HD, "/stream1"),
        (StreamQuality.SD, "/stream2"),
        (StreamQuality.TELE_HD, "/stream6"),
        (StreamQuality.TELE_SD, "/stream7"),
    ],
)
def test_every_quality_maps_to_its_path(quality, path):
    url = build_rtsp_url("10.0.0.5", "u", "p", quality)
    assert url == f"rtsp://u:p@10.0.0.5:554{path}"


def test_custom_port():
    assert build_rtsp_url("127.0.0.1", "u", "p", StreamQuality.SD, port=8554) == "rtsp://u:p@127.0.0.1:8554/stream2"


def test_special_characters_are_percent_encoded_exact():
    url = build_rtsp_url("192.168.1.10", "admin", "p@ss:w/rd#1%")
    assert url == "rtsp://admin:p%40ss%3Aw%2Frd%231%25@192.168.1.10:554/stream1"


def test_special_characters_in_username_exact():
    url = build_rtsp_url("192.168.1.10", "me@home:x", "pw", StreamQuality.SD)
    assert url == "rtsp://me%40home%3Ax:pw@192.168.1.10:554/stream2"


def test_space_is_encoded():
    url = build_rtsp_url("h", "a b", "c d")
    assert " " not in url
    parts = urlsplit(url)
    assert unquote(parts.username) == "a b"
    assert unquote(parts.password) == "c d"


@pytest.mark.parametrize(
    ("host", "expected_netloc"),
    [
        ("fe80::1", "[fe80::1]:554"),
        ("::1", "[::1]:554"),
        ("[::1]", "[::1]:554"),
        ("2001:db8::abcd", "[2001:db8::abcd]:554"),
    ],
)
def test_ipv6_hosts_are_bracketed_once(host, expected_netloc):
    url = build_rtsp_url(host, "u", "p", StreamQuality.SD)
    assert url == f"rtsp://u:p@{expected_netloc}/stream2"
    assert urlsplit(url).port == 554


_cred = st.text(
    alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x2FF, exclude_characters="\x7f"),
    min_size=1,
    max_size=24,
)


@settings(max_examples=200, deadline=None)
@given(user=_cred, password=_cred, quality=st.sampled_from(list(StreamQuality)))
def test_credentials_round_trip_through_urlsplit(user, password, quality):
    url = build_rtsp_url("192.168.0.2", user, password, quality, port=554)
    parts = urlsplit(url)
    assert parts.scheme == "rtsp"
    assert parts.hostname == "192.168.0.2"
    assert parts.port == 554
    assert parts.path == "/" + quality.value
    assert unquote(parts.username) == user
    assert unquote(parts.password) == password
    userinfo = url[len("rtsp://"):].rsplit("@", 1)[0]
    # nothing in the userinfo may terminate/confuse the authority
    for ch in "@/#? ":
        assert ch not in userinfo
    assert re.search(r"%(?![0-9A-Fa-f]{2})", userinfo) is None  # '%' only as an escape
    assert url.count("@") == 1
    assert userinfo.count(":") == 1


def test_redact_replaces_password():
    assert redact_url("rtsp://camuser:s3cret@192.168.1.10:554/stream1") == "rtsp://camuser:***@192.168.1.10:554/stream1"


def test_redact_encoded_password():
    url = build_rtsp_url("10.1.1.1", "admin", "p@ss:w/rd#1%")
    red = redact_url(url)
    assert red == "rtsp://admin:***@10.1.1.1:554/stream1"
    assert "p%40ss" not in red


def test_redact_ipv6():
    assert redact_url("rtsp://u:pw@[fe80::1]:554/stream2") == "rtsp://u:***@[fe80::1]:554/stream2"


@pytest.mark.parametrize(
    "url",
    [
        "rtsp://192.168.1.10:554/stream1",
        "rtsp://onlyuser@192.168.1.10:554/stream1",
        "tcp://127.0.0.1:5000?listen",
        "pipe:1",
    ],
)
def test_redact_leaves_password_less_urls_unchanged(url):
    assert redact_url(url) == url


@settings(max_examples=100, deadline=None)
@given(password=_cred)
def test_redact_never_contains_password(password):
    url = build_rtsp_url("192.168.0.2", "user", password)
    assert redact_url(url) == "rtsp://user:***@192.168.0.2:554/stream1"
