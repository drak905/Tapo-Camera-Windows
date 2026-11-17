"""Cloud<->LAN matching (stub)."""
from __future__ import annotations

from tapo_desktop.models import CloudDevice, LanDevice


def normalise_mac(mac: str) -> str:
    raise NotImplementedError


def match_devices(cloud: list[CloudDevice], lan: list[LanDevice]) -> dict[str, LanDevice]:
    raise NotImplementedError
