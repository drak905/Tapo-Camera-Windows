"""discover() against MockTdpResponder over real UDP on loopback [spec:tdp.transport]."""
from __future__ import annotations

import hashlib
import socket
import time

import pytest
from cryptography.hazmat.primitives import serialization

from tapo_desktop.core.discovery import tdp
from tapo_desktop.core.discovery.tdp import discover
from tests.fixtures.phase2_fixtures import DEFAULT_CLOUD_DEVICES, tdp_device_for

pytestmark = pytest.mark.integration


def _closed_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_discover_finds_mock_cameras_deduped_by_mac(mock_tdp):
    t0 = time.monotonic()
    found = discover(timeout=1.0, targets=[("127.0.0.1", mock_tdp.port)], attempts=3, interval=0.1)
    elapsed = time.monotonic() - t0
    assert 0.9 <= elapsed < 1.8
    assert len(mock_tdp.requests) == 3  # 3 probes -> 6 replies -> 2 unique devices
    assert len(found) == 2
    by_mac = {d.mac: d for d in found}
    for cloud in DEFAULT_CLOUD_DEVICES[:2]:
        mac = "-".join(cloud["deviceMac"][i:i + 2] for i in range(0, 12, 2))
        dev = by_mac[mac]
        assert dev.ip == "127.0.0.1"
        assert dev.device_id_md5 == hashlib.md5(cloud["deviceId"].encode()).hexdigest()
        assert dev.http_port == 443 and dev.model == cloud["deviceModel"]


def test_probe_is_spec_compliant(mock_tdp):
    discover(timeout=0.5, targets=[("127.0.0.1", mock_tdp.port)], attempts=2, interval=0.1)
    assert len(mock_tdp.requests) == 2
    assert len({r["serial"] for r in mock_tdp.requests}) == 1  # one serial per scan
    assert all(0 <= r["serial"] < 2 ** 28 for r in mock_tdp.requests)
    assert all(r["flags"] == 0x11 for r in mock_tdp.requests)
    pub = serialization.load_pem_public_key(mock_tdp.requests[0]["rsa_key"].encode())
    assert pub.key_size == 2048


def test_same_camera_from_two_responders_is_reported_once(mock_tdp_factory):
    dev = tdp_device_for(DEFAULT_CLOUD_DEVICES[0])
    a = mock_tdp_factory(devices=[dev])
    b = mock_tdp_factory(devices=[dict(dev, mac=dev["mac"].upper().replace(":", ""))])
    found = discover(timeout=0.8, targets=[("127.0.0.1", a.port), ("127.0.0.1", b.port)], attempts=2,
                     interval=0.1)
    assert len(found) == 1 and a.requests and b.requests


def test_concatenated_replies_and_garbage_are_handled(mock_tdp_factory):
    devices = [tdp_device_for(d, ip=f"10.0.0.{i}") for i, d in enumerate(DEFAULT_CLOUD_DEVICES, 1)]
    r = mock_tdp_factory(devices=devices, concat=True, garbage=True)
    found = discover(timeout=0.8, targets=[("127.0.0.1", r.port)], attempts=1)
    assert sorted(d.ip for d in found) == ["10.0.0.1", "10.0.0.2", "10.0.0.3"]


def test_no_responder_returns_empty_within_timeout():
    t0 = time.monotonic()
    assert discover(timeout=0.6, targets=[("127.0.0.1", _closed_udp_port())], attempts=3, interval=0.1) == []
    assert time.monotonic() - t0 < 1.5


def test_unencrypted_camera_is_discovered(mock_tdp_factory):
    cloud = DEFAULT_CLOUD_DEVICES[0]
    r = mock_tdp_factory(devices=[tdp_device_for(cloud, encrypt=False)])
    found = discover(timeout=0.5, targets=[("127.0.0.1", r.port)], attempts=1)
    assert len(found) == 1
    assert found[0].device_id_md5 == hashlib.md5(cloud["deviceId"].encode()).hexdigest()
    assert found[0].http_port is None


def test_default_targets_cover_limited_broadcast_and_both_ports():
    targets = tdp.default_targets()
    assert ("255.255.255.255", 20002) in targets
    assert ("255.255.255.255", 20004) in targets
    assert len(targets) == len(set(targets))
    for host, port in targets:
        socket.inet_aton(host)
        assert port in (20002, 20004)
        assert not host.startswith("127.")


def test_rsa_key_is_reused_across_scans(mock_tdp):
    discover(timeout=0.3, targets=[("127.0.0.1", mock_tdp.port)], attempts=1)
    discover(timeout=0.3, targets=[("127.0.0.1", mock_tdp.port)], attempts=1)
    assert mock_tdp.requests[0]["rsa_key"] == mock_tdp.requests[1]["rsa_key"]  # app-lifetime key pair
