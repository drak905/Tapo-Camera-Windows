"""Mock TDP v2 responder (Tapo camera LAN discovery) for Phase 2 tests.

Listens on UDP ``127.0.0.1:<port>`` and answers every valid discovery probe like a Tapo camera
[spec:tdp.request.packet] [spec:tdp.response.validation] [spec:tdp.response.fields]:
16-byte big-endian header (version 2, opcode 1, reply flag, request serial, CRC32 seeded with
0x5A6B7C8D) + JSON ``{"error_code":0,"result":{...}}``. Probes with a bad CRC are ignored.

Each device dict accepts: ``ip`` (default ``127.0.0.1``), ``mac`` (any format), ``device_id``
(full cloud deviceId), ``device_model``, ``device_type`` (default ``SMART.IPCAMERA``),
``http_port`` (default 443), ``encrypt`` (default True) and ``extra`` (merged into ``result``).
With ``encrypt=True`` the reply carries ``encrypt_info`` [spec:tdp.encrypt_info.decrypt]: a random
32-byte key||iv encrypted with RSA-OAEP-SHA1 to the probe's ``rsa_key`` and the JSON
``{"device_id", "http_port", ...}`` encrypted with AES-128-CBC/PKCS7. Like real C200 firmware
1.5.7 the top-level ``device_id`` is MD5(deviceId) in UPPER case (clients must lowercase it), also
next to ``encrypt_info``; set ``top_level_id=False`` to omit it (identity only via decryption).

The packet/crypto code here is implemented independently of ``tapo_desktop.core.discovery``.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import select
import socket
import struct
import threading
import zlib
from typing import Any

from cryptography.hazmat.primitives import hashes, padding, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_HDR = struct.Struct(">BBHHBBII")
_CRC_SEED = 0x5A6B7C8D


def _crc(packet: bytes) -> int:
    return zlib.crc32(packet[:12] + struct.pack(">I", _CRC_SEED) + packet[16:]) & 0xFFFFFFFF


def build_packet(payload: bytes, serial: int, *, flags: int, opcode: int = 1, version: int = 2,
                 ret: int = 0) -> bytes:
    """Header + payload with a valid TDP checksum."""
    hdr = _HDR.pack(version, 0, opcode, len(payload), flags, ret, serial, _CRC_SEED)
    pkt = hdr + payload
    return pkt[:12] + struct.pack(">I", _crc(pkt)) + pkt[16:]


def build_reply(device: dict, serial: int, rsa_key_pem: str | None) -> bytes:
    """Reply packet for one mock device (see module docstring)."""
    full_id = str(device.get("device_id", ""))
    result: dict[str, Any] = {
        "ip": device.get("ip", "127.0.0.1"),
        "mac": device.get("mac", ""),
        "device_type": device.get("device_type", "SMART.IPCAMERA"),
        "device_model": device.get("device_model", "C210"),
        "device_name": device.get("device_model", "C210"),
        "factory_default": False,
        "hardware_version": "2.0",
        "firmware_version": "1.3.11 Build 231121 Rel.41203n",
        "owner": "0" * 32,
        "is_support_iot_cloud": True,
        "mgt_encrypt_schm": {"is_support_https": True},
    }
    if device.get("encrypt", True) and rsa_key_pem:
        pub = serialization.load_pem_public_key(rsa_key_pem.encode("ascii"))
        key_iv = os.urandom(32)
        enc_key = pub.encrypt(key_iv, asym_padding.OAEP(mgf=asym_padding.MGF1(algorithm=hashes.SHA1()),
                                                        algorithm=hashes.SHA1(), label=None))
        inner = json.dumps({"device_id": full_id, "http_port": int(device.get("http_port", 443)),
                            "connect_ssid": "", "connect_type": "wireless", "owner": "0" * 32}).encode("utf-8")
        padder = padding.PKCS7(128).padder()
        padded = padder.update(inner) + padder.finalize()
        enc = Cipher(algorithms.AES(key_iv[:16]), modes.CBC(key_iv[16:32])).encryptor()
        data = enc.update(padded) + enc.finalize()
        result["encrypt_info"] = {"sym_schm": "AES", "key": base64.b64encode(enc_key).decode("ascii"),
                                  "data": base64.b64encode(data).decode("ascii")}
    if device.get("top_level_id", True) or not (device.get("encrypt", True) and rsa_key_pem):
        result["device_id"] = hashlib.md5(full_id.encode("utf-8")).hexdigest().upper()
    result.update(device.get("extra", {}))
    payload = json.dumps({"error_code": 0, "result": result}).encode("utf-8")
    return build_packet(payload, serial, flags=0x02)


class MockTdpResponder:
    """UDP responder emulating one or more Tapo cameras answering TDP discovery.

    ``concat=True`` packs all device replies into ONE datagram (allowed by the spec);
    ``garbage=True`` sends a junk datagram before the real replies. ``requests`` logs every valid
    probe as ``{"serial", "rsa_key", "flags", "addr"}``; ``bad_probes`` counts rejected datagrams.
    """

    def __init__(self, devices: list[dict] | None = None, port: int = 0, *, host: str = "127.0.0.1",
                 concat: bool = False, garbage: bool = False):
        self.devices: list[dict] = list(devices or [])
        self.concat = concat
        self.garbage = garbage
        self.requests: list[dict] = []
        self.bad_probes = 0
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind((host, port))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._sock.getsockname()[1]

    def start(self) -> "MockTdpResponder":
        self._thread = threading.Thread(target=self._run, name="MockTdpResponder", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        self._sock.close()

    def __enter__(self) -> "MockTdpResponder":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([self._sock], [], [], 0.1)
                if not ready:
                    continue
                data, addr = self._sock.recvfrom(65535)
            except (OSError, ValueError):  # ConnectionResetError (ICMP) on Windows, or closed
                if self._stop.is_set():
                    return
                continue
            try:
                self._answer(data, addr)
            except Exception:  # noqa: BLE001 - a mock must never die on a bad packet
                self.bad_probes += 1

    def _answer(self, data: bytes, addr: tuple[str, int]) -> None:
        if len(data) < 16:
            self.bad_probes += 1
            return
        version, _, opcode, length, flags, _, serial, checksum = _HDR.unpack_from(data)
        if version != 2 or opcode != 1 or len(data) < 16 + length or _crc(data[:16 + length]) != checksum:
            self.bad_probes += 1
            return
        params = json.loads(data[16:16 + length].decode("utf-8")).get("params", {})
        rsa_key = params.get("rsa_key")
        self.requests.append({"serial": serial, "rsa_key": rsa_key, "flags": flags, "addr": addr})
        if self.garbage:
            self._sock.sendto(b"\x02\x00garbage-not-a-tdp-packet", addr)
        replies = [build_reply(d, serial, rsa_key) for d in self.devices]
        if self.concat and replies:
            self._sock.sendto(b"".join(replies), addr)
        else:
            for r in replies:
                self._sock.sendto(r, addr)
