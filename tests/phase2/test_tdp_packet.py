"""TDP v2 packet building / parsing [spec:tdp.request.packet] [spec:tdp.response.validation]."""
from __future__ import annotations

import hashlib
import json
import struct
import zlib
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from hypothesis import given, settings
from hypothesis import strategies as st

from tapo_desktop.core.discovery.tdp import TDP_PORT, build_probe, parse_response
from tapo_desktop.models import LanDevice
from tests.fixtures.mock_tdp import build_packet, build_reply

pytestmark = pytest.mark.unit

SPEC = Path(__file__).resolve().parents[2] / "specs" / "cloud_account_spec.json"
DEVICE_ID = "802200000000000000000000000000000000ABCD"
DEVICE_MD5 = "e4247a931858b38594790fc6606441aa"


def _spec_item(item_id: str) -> dict:
    items = json.loads(SPEC.read_text(encoding="utf-8"))["items"]
    return next(i for i in items if i["id"] == item_id)["value"]


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.public_key().public_bytes(serialization.Encoding.PEM,
                                        serialization.PublicFormat.SubjectPublicKeyInfo).decode("ascii")
    return key, pem


def _device(**kw) -> dict:
    d = {"ip": "192.168.1.50", "mac": "a0:b1:c2:00:00:01", "device_id": DEVICE_ID, "device_model": "C210",
         "device_type": "SMART.IPCAMERA", "http_port": 4433}
    d.update(kw)
    return d


def test_port_constant():
    assert TDP_PORT == 20002


def test_probe_golden_vector_byte_exact():
    gv = _spec_item("tdp.request.packet")["golden_vector"]
    assert gv["payload"] == '{"params":{"rsa_key":"TEST"}}'
    assert build_probe("TEST", int(gv["serial"], 16)) == bytes.fromhex(gv["packet_hex"])


def test_probe_with_real_key_layout_and_crc(keypair):
    _, pem = keypair
    pkt = build_probe(pem, 0x0ABCDEF)
    version, reserved, opcode, length, flags, ret, serial, checksum = struct.unpack(">BBHHBBII", pkt[:16])
    assert (version, reserved, opcode, flags, ret, serial) == (2, 0, 1, 0x11, 0, 0x0ABCDEF)
    assert length == len(pkt) - 16
    assert json.loads(pkt[16:]) == {"params": {"rsa_key": pem}}
    assert pem.startswith("-----BEGIN PUBLIC KEY-----\n") and pem.endswith("-----END PUBLIC KEY-----\n")
    seeded = pkt[:12] + struct.pack(">I", 0x5A6B7C8D) + pkt[16:]
    assert zlib.crc32(seeded) & 0xFFFFFFFF == checksum


@pytest.mark.parametrize("serial", [-1, 2 ** 32])
def test_probe_rejects_out_of_range_serial(serial):
    with pytest.raises(ValueError):
        build_probe("TEST", serial)


def test_parse_encrypted_reply(keypair):
    key, pem = keypair
    dev = parse_response(build_reply(_device(), 77, pem), key, serial=77)
    assert isinstance(dev, LanDevice)
    assert dev.ip == "192.168.1.50"
    assert dev.mac == "A0-B1-C2-00-00-01"
    assert dev.model == "C210" and dev.device_type == "SMART.IPCAMERA"
    assert dev.device_id_md5 == DEVICE_MD5 == hashlib.md5(DEVICE_ID.encode()).hexdigest()
    assert dev.http_port == 4433
    assert dev.source == "tdp"
    assert dev.raw["device_id"] == DEVICE_ID  # decrypted encrypt_info fields are kept in raw


def test_parse_plain_reply_lowercases_md5_and_has_no_port(keypair):
    key, _ = keypair
    dev = parse_response(build_reply(_device(encrypt=False), 5, None), key, serial=5)
    assert dev is not None
    assert dev.device_id_md5 == DEVICE_MD5
    assert dev.http_port is None


def test_parse_http_port_zero_means_default(keypair):
    key, pem = keypair
    dev = parse_response(build_reply(_device(http_port=0), 5, pem), key)
    assert dev is not None and dev.http_port is None


def test_parse_with_wrong_private_key_keeps_device_without_identity(keypair):
    _, pem = keypair
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    dev = parse_response(build_reply(_device(), 5, pem), other)
    assert dev is not None and dev.ip == "192.168.1.50" and dev.device_id_md5 == ""


def test_parse_rejects_bad_crc(keypair):
    key, pem = keypair
    pkt = bytearray(build_reply(_device(), 9, pem))
    pkt[-3] ^= 0x01
    assert parse_response(bytes(pkt), key) is None
    pkt2 = bytearray(build_reply(_device(), 9, pem))
    pkt2[13] ^= 0xFF
    assert parse_response(bytes(pkt2), key) is None


def test_parse_rejects_serial_and_version_mismatch(keypair):
    key, pem = keypair
    assert parse_response(build_reply(_device(), 9, pem), key, serial=10) is None
    payload = json.dumps({"error_code": 0, "result": {"ip": "1.2.3.4", "mac": "AABBCCDDEEFF"}}).encode()
    assert parse_response(build_packet(payload, 9, flags=2, version=1), key) is None
    assert parse_response(build_packet(payload, 9, flags=2, ret=2), key) is None
    assert parse_response(build_packet(payload, 9, flags=2, ret=1), key) is not None


def test_parse_rejects_error_code_and_non_json(keypair):
    key, _ = keypair
    err = json.dumps({"error_code": -1, "result": {"ip": "1.2.3.4", "mac": "AABBCCDDEEFF"}}).encode()
    assert parse_response(build_packet(err, 1, flags=2), key) is None
    assert parse_response(build_packet(b"\xff\xfenot json", 1, flags=2), key) is None
    assert parse_response(build_packet(b"[1,2,3]", 1, flags=2), key) is None
    no_ip = json.dumps({"error_code": 0, "result": {"mac": "AABBCCDDEEFF"}}).encode()
    assert parse_response(build_packet(no_ip, 1, flags=2), key) is None


@pytest.mark.parametrize("data", [b"", b"\x02", b"\x02\x00\x00\x01" + b"\x00" * 12, b"garbage" * 10,
                                  b"\x02\x00\x00\x01\xff\xff\x02\x00" + b"\x00" * 8])
def test_parse_garbage_returns_none(keypair, data):
    key, _ = keypair
    assert parse_response(data, key) is None


def test_parse_first_of_concatenated_packets(keypair):
    key, pem = keypair
    a = build_reply(_device(), 3, pem)
    b = build_reply(_device(mac="AA-AA-AA-AA-AA-AA", ip="192.168.1.51"), 3, pem)
    dev = parse_response(a + b, key, serial=3)
    assert dev is not None and dev.ip == "192.168.1.50"


@settings(max_examples=300, deadline=None)
@given(st.binary(max_size=600))
def test_parse_never_raises_on_random_bytes(keypair, data):
    key, _ = keypair
    assert parse_response(data, key) is None or isinstance(parse_response(data, key), LanDevice)
