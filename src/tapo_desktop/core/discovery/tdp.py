"""TDP v2 discovery (stub)."""
from __future__ import annotations

from tapo_desktop.models import LanDevice

TDP_PORT = 20002


def build_probe(public_key_pem: str, serial: int) -> bytes:
    raise NotImplementedError


def parse_response(data: bytes, private_key, *, serial: int | None = None) -> LanDevice | None:
    raise NotImplementedError


def discover(timeout: float = 3.0, *, targets: list[tuple[str, int]] | None = None, attempts: int = 3,
             interval: float = 0.3) -> list[LanDevice]:
    raise NotImplementedError


def default_targets() -> list[tuple[str, int]]:
    raise NotImplementedError
