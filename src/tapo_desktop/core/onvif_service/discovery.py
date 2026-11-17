"""WS-Discovery (UDP 239.255.255.250:3702) and TCP subnet scan for ONVIF cameras [spec:onvif.discovery]."""
from __future__ import annotations

import ipaddress
import logging
import select
import socket
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit
from xml.sax.saxutils import escape

from tapo_desktop.core.onvif_service import _xml as X

logger = logging.getLogger(__name__)

_PROBE_ACTION = "http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe"
_DISCOVERY_TO = "urn:schemas-xmlsoap-org:ws:2005:04:discovery"
_DISCOVERY_NAMESPACES = (X.DISCOVERY_2005, X.DISCOVERY_2009)
_NAME_SCOPE = "onvif://www.onvif.org/name/"
_MAX_DATAGRAM = 65535
_MAX_SCAN_ADDRESSES = 65536
_REPEAT_AFTER_S = 0.3   # WS-Discovery recommends re-sending the multicast Probe once (UDP is lossy)


@dataclass(frozen=True)
class DiscoveredDevice:
    host: str
    xaddrs: tuple[str, ...]
    scopes: tuple[str, ...]
    name: str = ""


def build_probe(message_id: str | None = None) -> bytes:
    """SOAP 1.2 WS-Discovery Probe for ``dn:NetworkVideoTransmitter``.

    ``message_id`` defaults to a fresh ``uuid:<uuid4>``.
    """
    mid = message_id or f"uuid:{uuid.uuid4()}"
    return (f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<s:Envelope xmlns:s="{X.SOAP12}" xmlns:a="{X.WSA_2004}" xmlns:d="{X.DISCOVERY_2005}" '
            f'xmlns:dn="{X.DN}"><s:Header><a:MessageID>{escape(mid)}</a:MessageID>'
            f"<a:To>{_DISCOVERY_TO}</a:To><a:Action>{_PROBE_ACTION}</a:Action></s:Header>"
            f"<s:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></s:Body>"
            f"</s:Envelope>").encode("utf-8")


def _host_of(xaddr: str) -> str:
    try:
        return urlsplit(xaddr).hostname or ""
    except ValueError:
        return ""


def _name_of(scopes: tuple[str, ...]) -> str:
    for scope in scopes:
        if scope.startswith(_NAME_SCOPE):
            return unquote(scope[len(_NAME_SCOPE):], errors="replace")
    return ""


def parse_probe_match(data: bytes) -> list[DiscoveredDevice]:
    """Parse a ProbeMatches datagram. Tolerant of garbage: returns ``[]``.

    ``host`` is the hostname of the first XAddr; ``name`` comes from the
    ``onvif://www.onvif.org/name/<name>`` scope (URL-decoded) if present.
    Matches without a usable XAddr are skipped.
    """
    root = X.safe_parse(data)
    body = X.soap_body(root) if root is not None else None
    if body is None:
        return []
    devices: list[DiscoveredDevice] = []
    for match in X.iter_local(body, "ProbeMatch"):
        if X.namespace(match.tag) not in _DISCOVERY_NAMESPACES:
            continue
        xaddrs = tuple(X.text_local(match, "XAddrs").split())
        host = _host_of(xaddrs[0]) if xaddrs else ""
        if not host:
            continue
        scopes = tuple(X.text_local(match, "Scopes").split())
        devices.append(DiscoveredDevice(host=host, xaddrs=xaddrs, scopes=scopes, name=_name_of(scopes)))
    return devices


def _local_ipv4_addresses() -> list[str]:
    """Non-loopback IPv4 addresses of this host (one multicast sender per interface)."""
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return []
    return sorted({str(info[4][0]) for info in infos if not str(info[4][0]).startswith("127.")})


def _open_senders() -> list[socket.socket]:
    """UDP sockets bound per local interface (falls back to one default-route socket)."""
    socks: list[socket.socket] = []
    for addr in _local_ipv4_addresses() or [""]:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
            if addr:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(addr))
            s.bind((addr, 0))
            s.setblocking(False)
            socks.append(s)
        except OSError as exc:
            logger.debug("WS-Discovery: cannot use interface %s: %s", addr or "default", exc)
            s.close()
    return socks


def _send_all(socks: list[socket.socket], probe: bytes, dest: tuple[str, int]) -> None:
    for s in socks:
        try:
            s.sendto(probe, dest)
        except OSError as exc:
            logger.debug("WS-Discovery: probe send failed on %s: %s", s.getsockname(), exc)


def _collect(socks: list[socket.socket], probe: bytes, dest: tuple[str, int], timeout: float) -> list[DiscoveredDevice]:
    """Send the Probe (once more after ``_REPEAT_AFTER_S``) and gather ProbeMatches until ``timeout``."""
    start = time.monotonic()
    deadline = start + max(0.0, timeout)
    found: dict[str, DiscoveredDevice] = {}
    _send_all(socks, probe, dest)
    repeated = False
    while socks and (left := deadline - time.monotonic()) > 0:
        if not repeated and time.monotonic() - start >= _REPEAT_AFTER_S:
            repeated = True
            _send_all(socks, probe, dest)
        readable, _w, _x = select.select(socks, [], [], min(left, 0.1))
        for s in readable:
            try:
                data, _peer = s.recvfrom(_MAX_DATAGRAM)
            except OSError:  # e.g. WSAECONNRESET from an ICMP port-unreachable
                continue
            for dev in parse_probe_match(data):
                found.setdefault(dev.host, dev)
    return list(found.values())


def discover(timeout: float = 3.0, *, multicast_addr: str = "239.255.255.250", port: int = 3702) -> list[DiscoveredDevice]:
    """Send a Probe and collect ProbeMatches until ``timeout``; de-duplicated by host."""
    socks = _open_senders()
    try:
        found = _collect(socks, build_probe(), (multicast_addr, port), timeout)
    finally:
        for s in socks:
            s.close()
    logger.debug("WS-Discovery found %d device(s)", len(found))
    return found

def _port_open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def scan_subnet(cidr: str, port: int = 2020, timeout: float = 0.3, max_workers: int = 64) -> list[str]:
    """TCP-connect scan of every host address in ``cidr``; return hosts with ``port`` open (sorted).

    Raises ValueError for an invalid CIDR or a network larger than 65536 addresses.
    """
    network = ipaddress.ip_network(cidr.strip(), strict=False)
    if network.num_addresses > _MAX_SCAN_ADDRESSES:
        raise ValueError(f"network {network} is too large to scan")
    hosts = [str(h) for h in network.hosts()]
    if not hosts:
        return []
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(hosts))),
                            thread_name_prefix="onvif-scan") as pool:
        results = list(pool.map(lambda h: (h, _port_open(h, port, timeout)), hosts))
    return sorted((h for h, ok in results if ok), key=ipaddress.ip_address)
