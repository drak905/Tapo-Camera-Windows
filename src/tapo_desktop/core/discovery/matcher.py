"""Pair cloud devices with LAN-discovered cameras [spec:lan.match_rule].

The app keys both its device registry and its TDP results by ``md5(deviceId)`` (lowercase hex).
A LAN reply whose identity could not be decrypted falls back to MAC equality (normalised).
Each LAN device is assigned to at most one cloud device; an md5 match always wins over a MAC match.
"""
from __future__ import annotations

import hashlib
import re

from tapo_desktop.models import CloudDevice, LanDevice

_SEPARATORS = re.compile(r"[\s:.\-]")
_HEX12 = re.compile(r"^[0-9A-F]{12}$")


def normalise_mac(mac: str) -> str:
    """``"AA-BB-CC-DD-EE-FF"`` for any of ``aa:bb:..``, ``AA-BB-..``, ``aabbcc..``, ``aabb.ccdd.eeff``.

    Returns ``""`` when the input is not a 48-bit MAC, so invalid values never match each other.
    """
    compact = _SEPARATORS.sub("", mac or "").upper()
    if not _HEX12.match(compact):
        return ""
    return "-".join(compact[i:i + 2] for i in range(0, 12, 2))


def device_id_md5(device_id: str) -> str:
    """Lowercase hex MD5 of the cloud deviceId (UTF-8), the key TDP broadcasts."""
    return hashlib.md5(device_id.encode("utf-8")).hexdigest()


def match_devices(cloud: list[CloudDevice], lan: list[LanDevice]) -> dict[str, LanDevice]:
    """Map ``CloudDevice.device_id`` -> matching ``LanDevice`` (unmatched cloud devices are absent)."""
    result: dict[str, LanDevice] = {}
    used: set[int] = set()
    by_md5: dict[str, int] = {}
    for idx, dev in enumerate(lan):
        key = (dev.device_id_md5 or "").strip().lower()
        if key:
            by_md5.setdefault(key, idx)
    for c in cloud:
        idx = by_md5.get(device_id_md5(c.device_id)) if c.device_id else None
        if idx is not None and idx not in used:
            result[c.device_id] = lan[idx]
            used.add(idx)
    by_mac: dict[str, int] = {}
    for idx, dev in enumerate(lan):
        mac = normalise_mac(dev.mac)
        if mac and idx not in used:
            by_mac.setdefault(mac, idx)
    for c in cloud:
        if c.device_id in result:
            continue
        mac = normalise_mac(c.mac)
        idx = by_mac.get(mac) if mac else None
        if idx is not None and idx not in used:
            result[c.device_id] = lan[idx]
            used.add(idx)
    return result
