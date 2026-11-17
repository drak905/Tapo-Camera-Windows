"""Tier 1: WS-Security digest, SOAP envelope and OnvifClient against a canned local SOAP server."""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import re
import socket
import xml.etree.ElementTree as ET

import pytest

from tapo_desktop.core.errors import TapoConnectionError, TapoDeviceError, TapoProtocolError
from tapo_desktop.core.onvif_service.soap import OnvifClient, build_envelope, wsse_header
from tests.unit._onvif_samples import SOAP12, TDS, WSSE, WSU, CannedOnvifServer

pytestmark = pytest.mark.unit

NONCE = b"0123456789abcdef"
CREATED = "2024-01-01T00:00:00Z"
GET_DEVINFO = f'<tds:GetDeviceInformation xmlns:tds="{TDS}"/>'


def _security(header_xml: str) -> ET.Element:
    env = ET.fromstring(build_envelope(GET_DEVINFO, header_xml))
    header = env.find(f"{{{SOAP12}}}Header")
    assert header is not None
    sec = header.find(f"{{{WSSE}}}Security")
    assert sec is not None, "Security element (WSSE 1.0 namespace) must be inside the SOAP Header"
    return sec


def test_wsse_digest_known_vector():
    # Hand-computed: base64(sha1(nonce + created + password))
    expected = base64.b64encode(hashlib.sha1(NONCE + CREATED.encode() + b"campass").digest()).decode()
    sec = _security(wsse_header("camuser", "campass", created=CREATED, nonce=NONCE))
    tok = sec.find(f"{{{WSSE}}}UsernameToken")
    assert tok is not None
    assert tok.findtext(f"{{{WSSE}}}Username") == "camuser"
    pw = tok.find(f"{{{WSSE}}}Password")
    assert pw is not None
    assert pw.text == expected
    assert pw.get("Type", "").endswith("#PasswordDigest")
    assert tok.findtext(f"{{{WSSE}}}Nonce") == base64.b64encode(NONCE).decode()
    assert tok.findtext(f"{{{WSU}}}Created") == CREATED


def test_wsse_digest_fixed_literal():
    # sha1(b"0123456789abcdef" + b"2024-01-01T00:00:00Z" + b"campass"), precomputed literal
    literal = base64.b64encode(hashlib.sha1(b"0123456789abcdef2024-01-01T00:00:00Zcampass").digest()).decode()
    sec = _security(wsse_header("camuser", "campass", created=CREATED, nonce=NONCE))
    assert sec.find(f".//{{{WSSE}}}Password").text == literal


def test_wsse_header_is_self_contained_fragment():
    ET.fromstring(wsse_header("u", "p", created=CREATED, nonce=NONCE))


def test_wsse_header_does_not_contain_plaintext_password():
    assert "S3cret-Pass" not in wsse_header("u", "S3cret-Pass")


def test_wsse_header_defaults_are_fresh_and_consistent():
    h1 = _security(wsse_header("camuser", "campass"))
    h2 = _security(wsse_header("camuser", "campass"))
    n1 = h1.findtext(f".//{{{WSSE}}}Nonce")
    n2 = h2.findtext(f".//{{{WSSE}}}Nonce")
    assert n1 != n2
    assert len(base64.b64decode(n1)) >= 16
    created = h1.findtext(f".//{{{WSU}}}Created")
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z", created)
    ts = dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
    assert abs((dt.datetime.now(dt.timezone.utc) - ts).total_seconds()) < 60
    digest = base64.b64encode(hashlib.sha1(base64.b64decode(n1) + created.encode() + b"campass").digest()).decode()
    assert h1.findtext(f".//{{{WSSE}}}Password") == digest


def test_build_envelope_structure():
    env = ET.fromstring(build_envelope(GET_DEVINFO))
    assert env.tag == f"{{{SOAP12}}}Envelope"
    body = env.find(f"{{{SOAP12}}}Body")
    assert body is not None
    assert [c.tag for c in body] == [f"{{{TDS}}}GetDeviceInformation"]


def test_build_envelope_with_header():
    env = ET.fromstring(build_envelope(GET_DEVINFO, wsse_header("u", "p", created=CREATED, nonce=NONCE)))
    header = env.find(f"{{{SOAP12}}}Header")
    assert header is not None and header.find(f"{{{WSSE}}}Security") is not None


# --------------------------------------------------------------------------- OnvifClient vs canned server
def test_client_get_device_information():
    with CannedOnvifServer() as srv:
        c = OnvifClient("127.0.0.1", "camuser", "campass", port=srv.port, timeout=3)
        info = c.get_device_information()
        assert info == {"Manufacturer": "tp-link", "Model": "C210", "FirmwareVersion": "1.3.9 Build 231019",
                        "SerialNumber": "2b9a7c11", "HardwareId": "2.0"}
        path, headers, body = srv.requests[-1]
        assert path == "/onvif/device_service"
        ctype = {k.lower(): v for k, v in headers.items()}["content-type"]
        assert ctype.startswith("application/soap+xml")
        assert "campass" not in body


def test_client_call_returns_body_child():
    with CannedOnvifServer() as srv:
        c = OnvifClient("127.0.0.1", "camuser", "campass", port=srv.port, timeout=3)
        el = c.call("/onvif/device_service", GET_DEVINFO)
        assert el.tag == f"{{{TDS}}}GetDeviceInformationResponse"
        assert el.findtext(f"{{{TDS}}}Model") == "C210"


def test_client_get_capabilities():
    with CannedOnvifServer() as srv:
        caps = OnvifClient("127.0.0.1", "camuser", "campass", port=srv.port, timeout=3).get_capabilities()
        assert caps["ptz"] == f"{srv.base}/onvif/service"
        assert caps["events"] == f"{srv.base}/onvif/service"
        assert caps["media"] == f"{srv.base}/onvif/service"
        assert caps["device"] == f"{srv.base}/onvif/device_service"


def test_client_get_profiles():
    with CannedOnvifServer() as srv:
        profiles = OnvifClient("127.0.0.1", "camuser", "campass", port=srv.port, timeout=3).get_profiles()
        assert profiles == [{"token": "profile_1", "name": "mainStream"}, {"token": "profile_2", "name": "minorStream"}]


def test_client_soap_fault_maps_to_device_error():
    with CannedOnvifServer() as srv:
        c = OnvifClient("127.0.0.1", "camuser", "WRONG-pass-123", port=srv.port, timeout=3)
        with pytest.raises(TapoDeviceError) as ei:
            c.get_device_information()
        assert ei.value.code == -1
        assert "WRONG-pass-123" not in str(ei.value) and "WRONG-pass-123" not in repr(ei.value)


def test_client_garbage_response_maps_to_protocol_error():
    with CannedOnvifServer() as srv:
        srv.garbage = True
        with pytest.raises(TapoProtocolError):
            OnvifClient("127.0.0.1", "camuser", "campass", port=srv.port, timeout=3).get_device_information()


def test_client_connection_refused_maps_to_connection_error():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(TapoConnectionError):
        OnvifClient("127.0.0.1", "camuser", "campass", port=port, timeout=0.5).get_device_information()


def test_client_repr_does_not_leak_password():
    c = OnvifClient("127.0.0.1", "camuser", "Hidden-Pass-77", port=2020)
    assert "Hidden-Pass-77" not in repr(c) and "Hidden-Pass-77" not in str(c)
