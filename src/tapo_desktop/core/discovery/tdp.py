"""TDP v2 (TP-Link Discovery Protocol) LAN discovery of Tapo cameras [spec:tdp.*].

Packet = 16-byte big-endian header + UTF-8 JSON [spec:tdp.request.packet]::

    0 version(2) | 1 reserved | 2..3 opcode(1 = discovery) | 4..5 payload length | 6 flags
    7 ret | 8..11 serial (random < 2**28) | 12..15 CRC32 (computed with 0x5A6B7C8D in this slot)

The probe payload is ``{"params":{"rsa_key":"<PEM RSA-2048 public key>"}}``. Cameras answer with
``{"error_code":0,"result":{ip, mac, device_type, device_model, ..., encrypt_info}}``; the
``encrypt_info`` block holds the full deviceId and the local HTTPS port, encrypted with
RSA-OAEP-SHA1 (key||iv) + AES-128-CBC [spec:tdp.encrypt_info.decrypt]. Like the app, one RSA key
pair is generated per process and reused for every scan.
"""
from __future__ import annotations

import base64
import binascii
import ctypes
import hashlib
import ipaddress
import json
import logging
import secrets
import socket
import struct
import sys
import threading
import time
import zlib
from collections.abc import Iterator
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from tapo_desktop.core.discovery.matcher import normalise_mac
from tapo_desktop.models import LanDevice

log = logging.getLogger(__name__)

TDP_PORT = 20002
TDP_ALT_PORT = 20004  # camera scans in the app also probe this port [spec:tdp.transport]
TDP_VERSION = 2
OP_DISCOVERY = 0x0001
FLAG_REQUEST = 0x01
FLAG_REPLY = 0x02
FLAG_BROADCAST = 0x10
FLAG_UNICAST = 0x20
CRC_SEED = 0x5A6B7C8D
HEADER = struct.Struct(">BBHHBBII")
_MAX_DATAGRAM = 65535

_key_lock = threading.Lock()
_key_pair: tuple[rsa.RSAPrivateKey, str] | None = None


def _checksum(packet: bytes) -> int:
    """CRC-32 of ``packet`` with bytes 12..15 replaced by the seed 0x5A6B7C8D."""
    return zlib.crc32(packet[:12] + struct.pack(">I", CRC_SEED) + packet[16:]) & 0xFFFFFFFF


def build_probe(public_key_pem: str, serial: int) -> bytes:
    """Broadcast discovery request (flags 0x11) carrying ``public_key_pem``."""
    if not 0 <= serial < 2 ** 32:
        raise ValueError("serial must fit in 32 bits")
    payload = json.dumps({"params": {"rsa_key": public_key_pem}}, separators=(",", ":")).encode("utf-8")
    header = HEADER.pack(TDP_VERSION, 0, OP_DISCOVERY, len(payload), FLAG_BROADCAST | FLAG_REQUEST, 0,
                         serial, CRC_SEED)
    packet = header + payload
    return packet[:12] + struct.pack(">I", _checksum(packet)) + packet[16:]


def _iter_payloads(data: bytes, serial: int | None) -> Iterator[bytes]:
    """Yield the JSON payload of every valid packet in ``data`` (packets may be concatenated)."""
    offset = 0
    while len(data) - offset >= HEADER.size:
        version, _, _, length, _, ret, pkt_serial, checksum = HEADER.unpack_from(data, offset)
        end = offset + HEADER.size + length
        if length == 0 or end > len(data):
            return
        packet = data[offset:end]
        offset = end
        if _checksum(packet) != checksum:
            return  # framing is unreliable after a corrupt packet
        if version != TDP_VERSION or ret not in (0, 1) or (serial is not None and pkt_serial != serial):
            continue
        yield packet[HEADER.size:]


def _decrypt_info(info: Any, private_key: rsa.RSAPrivateKey) -> dict:
    """Decrypt ``encrypt_info`` -> dict, or ``{}`` if absent / undecryptable."""
    if not isinstance(info, dict) or not info.get("key") or not info.get("data"):
        return {}
    try:
        key_iv = private_key.decrypt(base64.b64decode(info["key"]),
                                     asym_padding.OAEP(mgf=asym_padding.MGF1(algorithm=hashes.SHA1()),
                                                       algorithm=hashes.SHA1(), label=None))
        if len(key_iv) < 32:
            return {}
        decryptor = Cipher(algorithms.AES(key_iv[:16]), modes.CBC(key_iv[16:32])).decryptor()
        padded = decryptor.update(base64.b64decode(info["data"])) + decryptor.finalize()
        unpadder = sym_padding.PKCS7(128).unpadder()
        plain = json.loads((unpadder.update(padded) + unpadder.finalize()).decode("utf-8"))
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError):
        return {}
    return plain if isinstance(plain, dict) else {}


def _device_from_payload(payload: bytes, private_key: rsa.RSAPrivateKey, sender_ip: str | None) -> LanDevice | None:
    try:
        doc = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict) or doc.get("error_code", 0) != 0 or not isinstance(doc.get("result"), dict):
        return None
    result: dict = doc["result"]
    ip = result.get("ip") if isinstance(result.get("ip"), str) else sender_ip
    try:
        ip = str(ipaddress.IPv4Address(ip)) if ip else None
    except ValueError:
        ip = sender_ip
    if not ip:
        return None
    secret = _decrypt_info(result.get("encrypt_info"), private_key)
    if secret.get("device_id"):
        md5 = hashlib.md5(str(secret["device_id"]).encode("utf-8")).hexdigest()
    elif "encrypt_info" not in result and isinstance(result.get("device_id"), str):
        md5 = result["device_id"].strip().lower()  # unencrypted replies carry MD5(deviceId) already
    else:
        md5 = ""
    try:
        port = int(secret.get("http_port") or 0)
    except (TypeError, ValueError):
        port = 0
    raw_mac = str(result.get("mac") or "")
    raw = {k: v for k, v in result.items() if k != "encrypt_info"}
    raw.update(secret)
    return LanDevice(ip=ip, mac=normalise_mac(raw_mac) or raw_mac.upper(), model=str(result.get("device_model") or ""),
                     device_type=str(result.get("device_type") or ""), device_id_md5=md5,
                     http_port=port if 0 < port < 65536 else None, source="tdp", raw=raw)


def _parse_all(data: bytes, private_key: rsa.RSAPrivateKey, serial: int | None,
               sender_ip: str | None = None) -> list[LanDevice]:
    devices = []
    for payload in _iter_payloads(data, serial):
        try:
            dev = _device_from_payload(payload, private_key, sender_ip)
        except Exception:  # noqa: BLE001 - a hostile datagram must never break discovery
            log.debug("TDP: unparsable reply", exc_info=True)
            continue
        if dev is not None:
            devices.append(dev)
    return devices


def parse_response(data: bytes, private_key: rsa.RSAPrivateKey, *, serial: int | None = None) -> LanDevice | None:
    """First camera in a TDP reply datagram, or ``None`` for garbage, bad CRC, wrong serial/version
    or a non-zero ``error_code``. Undecryptable ``encrypt_info`` keeps the device without identity."""
    try:
        devices = _parse_all(bytes(data), private_key, serial)
    except Exception:  # noqa: BLE001 - tolerant by contract: a hostile datagram must never raise
        log.debug("TDP: unparsable datagram", exc_info=True)
        return None
    return devices[0] if devices else None


def _get_key_pair() -> tuple[rsa.RSAPrivateKey, str]:
    """Process-lifetime RSA-2048 key pair and its PEM (SubjectPublicKeyInfo) [spec:tdp.request.packet]."""
    global _key_pair
    with _key_lock:
        if _key_pair is None:
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            pem = key.public_key().public_bytes(serialization.Encoding.PEM,
                                                serialization.PublicFormat.SubjectPublicKeyInfo).decode("ascii")
            _key_pair = (key, pem)
        return _key_pair


def _windows_ipv4_interfaces() -> list[tuple[str, str]]:
    """(address, netmask) of every up IPv4 interface via iphlpapi.GetIpAddrTable."""

    class _Row(ctypes.Structure):
        _fields_ = [("dwAddr", ctypes.c_uint32), ("dwIndex", ctypes.c_uint32), ("dwMask", ctypes.c_uint32),
                    ("dwBCastAddr", ctypes.c_uint32), ("dwReasmSize", ctypes.c_uint32),
                    ("unused1", ctypes.c_ushort), ("wType", ctypes.c_ushort)]

    get_table = ctypes.windll.iphlpapi.GetIpAddrTable  # type: ignore[attr-defined]
    size = ctypes.c_ulong(0)
    get_table(None, ctypes.byref(size), False)
    for _ in range(3):
        buf = ctypes.create_string_buffer(max(size.value, 4))
        rc = get_table(buf, ctypes.byref(size), False)
        if rc == 0:
            break
        if rc != 122:  # ERROR_INSUFFICIENT_BUFFER
            return []
    else:
        return []
    count = struct.unpack_from("<I", buf.raw, 0)[0]
    rows = (_Row * count).from_buffer_copy(buf.raw, 4)
    out = []
    for row in rows:
        if row.wType & (0x0008 | 0x0040):  # MIB_IPADDR_DISCONNECTED | MIB_IPADDR_DELETED
            continue
        out.append((socket.inet_ntoa(struct.pack("<I", row.dwAddr)), socket.inet_ntoa(struct.pack("<I", row.dwMask))))
    return out


def _fallback_ipv4_interfaces() -> list[tuple[str, str]]:
    """Local IPv4 addresses (assumed /24) when the interface table is unavailable."""
    addrs: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.add(str(info[4][0]))
    except OSError:
        pass
    return [(a, "255.255.255.0") for a in sorted(addrs)]


def default_targets() -> list[tuple[str, int]]:
    """255.255.255.255 plus every local interface's directed broadcast, on ports 20002 and 20004."""
    hosts = ["255.255.255.255"]
    try:
        interfaces = _windows_ipv4_interfaces() if sys.platform == "win32" else _fallback_ipv4_interfaces()
    except (OSError, AttributeError, ValueError):
        interfaces = []
    if not interfaces:
        interfaces = _fallback_ipv4_interfaces()
    for addr, mask in interfaces:
        try:
            net = ipaddress.IPv4Network(f"{addr}/{mask}", strict=False)
        except ValueError:
            continue
        if net.is_loopback or net.prefixlen >= 31 or ipaddress.IPv4Address(addr).is_link_local:
            continue
        bcast = str(net.broadcast_address)
        if bcast not in hosts:
            hosts.append(bcast)
    return [(h, p) for h in hosts for p in (TDP_PORT, TDP_ALT_PORT)]


def discover(timeout: float = 3.0, *, targets: list[tuple[str, int]] | None = None, attempts: int = 3,
             interval: float = 0.3) -> list[LanDevice]:
    """Probe ``targets`` ``attempts`` times ``interval`` s apart and collect replies for ``timeout`` s.

    ``targets=None`` uses :func:`default_targets`. Results are deduplicated by MAC (first reply wins)
    and sorted by IP. Never raises for network problems; returns what was found.
    """
    private_key, pem = _get_key_pair()
    serial = secrets.randbelow(1 << 28)
    probe = build_probe(pem, serial)
    dests = list(targets) if targets is not None else default_targets()
    found: dict[str, LanDevice] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", 0))
        deadline = time.monotonic() + max(0.0, timeout)
        sent, next_send = 0, time.monotonic()
        while True:
            now = time.monotonic()
            if now >= deadline:
                break
            if sent < max(1, attempts) and now >= next_send:
                for dest in dests:
                    try:
                        sock.sendto(probe, dest)
                    except OSError as exc:  # unreachable network / no route: skip this target
                        log.debug("TDP send to %s failed: %s", dest, exc)
                sent += 1
                next_send = now + max(0.0, interval)
            wake = deadline if sent >= max(1, attempts) else min(deadline, next_send)
            sock.settimeout(max(0.005, wake - time.monotonic()))
            try:
                data, addr = sock.recvfrom(_MAX_DATAGRAM)
            except TimeoutError:
                continue
            except OSError:  # e.g. WSAECONNRESET after ICMP port unreachable on Windows
                continue
            for dev in _parse_all(data, private_key, serial, addr[0]):
                key = normalise_mac(dev.mac) or dev.mac or dev.ip
                if key not in found:
                    found[key] = dev
                    log.debug("TDP: found %s %s at %s", dev.device_type, dev.model, dev.ip)
    finally:
        sock.close()
    log.info("TDP discovery: %d device(s)", len(found))
    return sorted(found.values(), key=lambda d: tuple(int(p) for p in d.ip.split(".")))
