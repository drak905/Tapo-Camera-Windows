"""Which camera is shown in which grid position (Phase 5, stub)."""
from __future__ import annotations

from collections.abc import Iterable

MAX_SLOTS = 25
SETTING_KEY = "grid_slots"


def parse_slots(saved: object, camera_ids: Iterable[str]) -> list[str | None] | None:
    raise NotImplementedError


def default_slots(camera_ids: Iterable[str]) -> list[str | None]:
    raise NotImplementedError


class ViewLayout:
    def __init__(self, camera_ids: Iterable[str], saved: object = None) -> None:
        raise NotImplementedError

    def slots(self) -> list[str | None]:
        raise NotImplementedError

    def visible(self, count: int) -> list[str | None]:
        raise NotImplementedError

    def index_of(self, camera_id: str) -> int | None:
        raise NotImplementedError

    def place(self, index: int, camera_id: str) -> bool:
        raise NotImplementedError

    def swap(self, a: int, b: int) -> bool:
        raise NotImplementedError

    def clear(self, index: int) -> bool:
        raise NotImplementedError

    def remove(self, camera_id: str) -> bool:
        raise NotImplementedError

    def add(self, camera_id: str, visible_count: int, fallback: int | None = None) -> int | None:
        raise NotImplementedError

    def sync(self, camera_ids: Iterable[str]) -> bool:
        raise NotImplementedError

    def to_setting(self) -> list[str | None]:
        raise NotImplementedError
