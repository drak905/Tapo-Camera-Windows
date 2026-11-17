"""Tier 1: WS-Discovery probe build/parse and TCP subnet scan."""
from __future__ import annotations

import socket
import time
import xml.etree.ElementTree as ET

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tapo_desktop.core.onvif_service.discovery import (
    DiscoveredDevice, build_probe, discover, parse_probe_match, scan_subnet,
)
from tests.unit._onvif_samples import DISC, SOAP12, probe_matches

pytestmark = pytest.mark.unit
PROBE_ACTION = "http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe"
DISCOVERY_TO = "urn:schemas-xmlsoap-org:ws:2005:04:discovery"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _header_values(root: ET.Element) -> dict[str, str]:
    header = root.find(f"{{{SOAP12}}}Header")
    assert header is not None
    return {_local(c.tag): (c.text or "").strip() for c in header}


def test_build_probe_structure():
    data = build_probe("uuid:11111111-2222-3333-4444-555555555555")
    assert isinstance(data, bytes)
    root = ET.fromstring(data)
    assert root.tag == f"{{{SOAP12}}}Envelope"
    hv = _header_values(root)
    assert hv["MessageID"] == "uuid:11111111-2222-3333-4444-555555555555"
    assert hv["Action"] == PROBE_ACTION
    assert hv["To"] == DISCOVERY_TO
    probe = root.find(f"{{{SOAP12}}}Body/{{{DISC}}}Probe")
    assert probe is not None
    types = probe.findtext(f"{{{DISC}}}Types")
    assert types is not None and types.split(":")[-1] == "NetworkVideoTransmitter"


def test_build_probe_default_message_id_unique():
    ids = {_header_values(ET.fromstring(build_probe()))["MessageID"] for _ in range(5)}
    assert len(ids) == 5
    assert all(i.startswith("uuid:") for i in ids)


def test_build_probe_fits_in_udp_datagram():
    assert len(build_probe()) < 1400


def test_parse_probe_match_single():
    data = probe_matches([(
        "http://192.168.1.50:2020/onvif/device_service",
        "onvif://www.onvif.org/type/video_encoder onvif://www.onvif.org/name/Living%20Room "
        "onvif://www.onvif.org/hardware/C210 onvif://www.onvif.org/location/",
        "3fa1fe68-b915-4053-a3e1-c006c3afec0e")])
    devices = parse_probe_match(data)
    assert devices == [DiscoveredDevice(
        host="192.168.1.50",
        xaddrs=("http://192.168.1.50:2020/onvif/device_service",),
        scopes=("onvif://www.onvif.org/type/video_encoder", "onvif://www.onvif.org/name/Living%20Room",
                "onvif://www.onvif.org/hardware/C210", "onvif://www.onvif.org/location/"),
        name="Living Room")]


def test_parse_probe_match_multiple_matches_and_xaddrs():
    data = probe_matches([
        ("http://10.0.0.7:2020/onvif/device_service http://[fe80::1]:2020/onvif/device_service",
         "onvif://www.onvif.org/name/C200", "a"),
        ("http://10.0.0.8:2020/onvif/device_service", "onvif://www.onvif.org/hardware/C310", "b"),
    ])
    devices = parse_probe_match(data)
    assert [d.host for d in devices] == ["10.0.0.7", "10.0.0.8"]
    assert devices[0].xaddrs == ("http://10.0.0.7:2020/onvif/device_service",
                                 "http://[fe80::1]:2020/onvif/device_service")
    assert devices[0].name == "C200"
    assert devices[1].name == ""


@pytest.mark.parametrize("garbage", [b"", b"\x00\xff\xfe", b"<not-closed", b"<a>hello</a>",
                                     b'<?xml version="1.0"?><x:Envelope xmlns:x="urn:x"/>',
                                     probe_matches([])])
def test_parse_probe_match_garbage(garbage):
    assert parse_probe_match(garbage) == []


@given(st.binary(max_size=600))
@settings(max_examples=150, deadline=None)
def test_parse_probe_match_never_raises(data):
    assert isinstance(parse_probe_match(data), list)


@given(st.text(max_size=300))
@settings(max_examples=100, deadline=None)
def test_parse_probe_match_never_raises_on_xmlish_text(s):
    data = probe_matches([(s.replace("<", "").replace("&", ""), "onvif://www.onvif.org/name/x", "u")])
    assert isinstance(parse_probe_match(data), list)


# --------------------------------------------------------------------------- subnet scan
@pytest.fixture
def listener():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    yield srv.getsockname()[1]
    srv.close()


def test_scan_subnet_finds_open_port(listener):
    assert scan_subnet("127.0.0.1/32", port=listener, timeout=0.3) == ["127.0.0.1"]


def test_scan_subnet_slash30(listener):
    t0 = time.monotonic()
    assert scan_subnet("127.0.0.0/30", port=listener, timeout=0.3) == ["127.0.0.1"]
    assert time.monotonic() - t0 < 3.0


def test_scan_subnet_closed_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert scan_subnet("127.0.0.1/32", port=port, timeout=0.3) == []


def test_scan_subnet_is_parallel(listener):
    # 62 hosts * 0.3 s sequentially would be ~19 s; parallel must be far quicker
    t0 = time.monotonic()
    found = scan_subnet("127.0.0.0/26", port=listener, timeout=0.3, max_workers=64)
    assert "127.0.0.1" in found
    assert time.monotonic() - t0 < 6.0


def test_scan_subnet_invalid_cidr():
    with pytest.raises(ValueError):
        scan_subnet("not-a-cidr", port=2020)


def test_discover_returns_list_quickly():
    t0 = time.monotonic()
    found = discover(timeout=0.5)
    assert isinstance(found, list)
    assert all(isinstance(d, DiscoveredDevice) for d in found)
    assert time.monotonic() - t0 < 3.0
