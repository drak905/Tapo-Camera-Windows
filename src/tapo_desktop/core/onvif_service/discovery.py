"""WS-Discovery (UDP 239.255.255.250:3702) and TCP subnet scan for ONVIF cameras [spec:onvif.discovery]."""
from __future__ import annotations

from dataclasses import dataclass


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
    raise NotImplementedError


def parse_probe_match(data: bytes) -> list[DiscoveredDevice]:
    """Parse a ProbeMatches datagram. Tolerant of garbage: returns ``[]``.

    ``host`` is the hostname of the first XAddr; ``name`` comes from the
    ``onvif://www.onvif.org/name/<name>`` scope (URL-decoded) if present.
    """
    raise NotImplementedError


def discover(timeout: float = 3.0, *, multicast_addr: str = "239.255.255.250", port: int = 3702) -> list[DiscoveredDevice]:
    """Send a Probe and collect ProbeMatches until ``timeout``; de-duplicated by host."""
    raise NotImplementedError


def scan_subnet(cidr: str, port: int = 2020, timeout: float = 0.3, max_workers: int = 64) -> list[str]:
    """TCP-connect scan of every host address in ``cidr``; return hosts with ``port`` open (sorted)."""
    raise NotImplementedError
