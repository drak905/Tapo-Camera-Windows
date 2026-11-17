"""Cloud <-> LAN matching by md5(deviceId) with MAC fallback [spec:lan.match_rule]."""
from __future__ import annotations

import hashlib

import pytest

from tapo_desktop.core.discovery.matcher import match_devices, normalise_mac
from tapo_desktop.models import CloudDevice, LanDevice

pytestmark = pytest.mark.unit

GOLDEN_ID = "802200000000000000000000000000000000ABCD"
GOLDEN_MD5 = "e4247a931858b38594790fc6606441aa"


def _cloud(device_id: str, mac: str) -> CloudDevice:
    return CloudDevice(device_id=device_id, mac=mac, model="C210", alias=device_id[-4:])


def _lan(ip: str, mac: str, md5: str = "") -> LanDevice:
    return LanDevice(ip=ip, mac=mac, device_id_md5=md5)


@pytest.mark.parametrize("raw", ["a0:b1:c2:00:00:01", "A0-B1-C2-00-00-01", "a0b1c2000001", "A0B1.C200.0001",
                                 " A0:B1:C2:00:00:01 "])
def test_normalise_mac_formats(raw):
    assert normalise_mac(raw) == "A0-B1-C2-00-00-01"


@pytest.mark.parametrize("raw", ["", "xyz", "A0:B1:C2", "A0B1C2000001FF", "G0:B1:C2:00:00:01"])
def test_normalise_mac_invalid_is_empty(raw):
    assert normalise_mac(raw) == ""


def test_match_by_md5_golden_vector():
    assert hashlib.md5(GOLDEN_ID.encode()).hexdigest() == GOLDEN_MD5
    cloud = [_cloud(GOLDEN_ID, "AABBCCDDEEFF")]
    lan = [_lan("192.168.1.20", "11-22-33-44-55-66", GOLDEN_MD5)]
    assert match_devices(cloud, lan) == {GOLDEN_ID: lan[0]}


def test_match_md5_is_case_insensitive():
    lan = [_lan("192.168.1.20", "", GOLDEN_MD5.upper())]
    assert match_devices([_cloud(GOLDEN_ID, "")], lan) == {GOLDEN_ID: lan[0]}


def test_match_falls_back_to_mac():
    cloud = [_cloud("A" * 40, "A0-B1-C2-00-00-01")]
    lan = [_lan("192.168.1.21", "a0:b1:c2:00:00:01")]
    assert match_devices(cloud, lan) == {"A" * 40: lan[0]}


def test_unmatched_devices_are_absent():
    cloud = [_cloud("A" * 40, "A0-B1-C2-00-00-01"), _cloud("B" * 40, "")]
    lan = [_lan("192.168.1.22", "FF-FF-FF-FF-FF-FF", "0" * 32), _lan("192.168.1.23", "")]
    assert match_devices(cloud, lan) == {}
    assert match_devices([], lan) == {} and match_devices(cloud, []) == {}


def test_md5_match_wins_and_each_lan_device_is_used_once():
    x = _cloud(GOLDEN_ID, "A0-B1-C2-00-00-01")
    y = _cloud("B" * 40, "A0-B1-C2-00-00-02")
    # LAN device 1 carries x's md5 but y's MAC: it belongs to x (md5 is authoritative)
    lan1 = _lan("192.168.1.30", "A0:B1:C2:00:00:02", GOLDEN_MD5)
    result = match_devices([x, y], [lan1])
    assert result == {GOLDEN_ID: lan1}


def test_many_devices():
    clouds = [_cloud(f"{i:040X}", f"A0B1C2{i:06X}") for i in range(30)]
    lans = [_lan(f"10.0.0.{i}", "", hashlib.md5(c.device_id.encode()).hexdigest()) for i, c in enumerate(clouds)
            if i % 2 == 0]
    lans += [_lan(f"10.0.1.{i}", c.mac.lower()) for i, c in enumerate(clouds) if i % 3 == 1]
    result = match_devices(clouds, lans)
    expected = {c.device_id for i, c in enumerate(clouds) if i % 2 == 0 or i % 3 == 1}
    assert set(result) == expected
    for i, c in enumerate(clouds):
        if i % 2 == 0:
            assert result[c.device_id].ip == f"10.0.0.{i}"
