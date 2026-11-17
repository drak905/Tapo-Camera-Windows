"""Which camera is shown in which grid position (Phase 5; pure Python, no Qt).

The view is an ordered list of :data:`MAX_SLOTS` slots, each holding a camera id or ``None`` (empty).
A grid of size ``n`` shows the first ``n * n`` slots. The list is persisted as the JSON setting
:data:`SETTING_KEY` (``"grid_slots"``).

Rules:

* First run (setting missing or not a list): the cameras in list order, then empty slots.
* A saved list is read leniently: entries that are not strings, unknown camera ids and repeated ids
  become empty slots; the list is padded / truncated to :data:`MAX_SLOTS`. Cameras that exist but are
  not in the saved list stay out of the view (the user removed them).
* :meth:`ViewLayout.sync` (camera list changed): removed cameras leave an empty slot; cameras that were
  not known before fill the first empty slot, if there is one.
* A camera is never in two slots: placing a camera that already has a slot swaps the two slots.
"""
from __future__ import annotations

from collections.abc import Iterable

MAX_SLOTS = 25
SETTING_KEY = "grid_slots"

Slots = list[str | None]


def default_slots(camera_ids: Iterable[str]) -> Slots:
    """Cameras in list order, padded with empty slots (cameras past :data:`MAX_SLOTS` are left out)."""
    ids = list(dict.fromkeys(camera_ids))[:MAX_SLOTS]
    return ids + [None] * (MAX_SLOTS - len(ids))


def parse_slots(saved: object, camera_ids: Iterable[str]) -> Slots | None:
    """Clean a persisted ``grid_slots`` value; ``None`` if it is not a list (use the default)."""
    if not isinstance(saved, list):
        return None
    known = set(camera_ids)
    seen: set[str] = set()
    slots: Slots = []
    for item in saved[:MAX_SLOTS]:
        if isinstance(item, str) and item in known and item not in seen:
            seen.add(item)
            slots.append(item)
        else:
            slots.append(None)
    return slots + [None] * (MAX_SLOTS - len(slots))


class ViewLayout:
    """Mutable slot list; every mutator returns whether anything changed."""

    def __init__(self, camera_ids: Iterable[str], saved: object = None) -> None:
        ids = list(camera_ids)
        parsed = parse_slots(saved, ids)
        self._slots: Slots = parsed if parsed is not None else default_slots(ids)
        self._known: set[str] = set(ids)

    def __repr__(self) -> str:
        used = sum(1 for s in self._slots if s is not None)
        return f"ViewLayout(used={used}/{MAX_SLOTS})"

    # ------------------------------------------------------------------ queries
    def slots(self) -> Slots:
        """Copy of all :data:`MAX_SLOTS` slots."""
        return list(self._slots)

    def visible(self, count: int) -> Slots:
        """The first ``count`` slots (what a grid of ``count`` positions shows)."""
        return self._slots[: max(0, min(count, MAX_SLOTS))]

    def index_of(self, camera_id: str) -> int | None:
        """Slot of ``camera_id``, or ``None`` if it is not in the view."""
        try:
            return self._slots.index(camera_id)
        except ValueError:
            return None

    def to_setting(self) -> Slots:
        """JSON-ready copy for setting :data:`SETTING_KEY`."""
        return list(self._slots)

    # ------------------------------------------------------------------ mutators
    def place(self, index: int, camera_id: str) -> bool:
        """Put ``camera_id`` into slot ``index``. If it already has a slot the two slots swap; else the
        camera previously in ``index`` leaves the view. KeyError for unknown cameras, IndexError for a
        slot outside ``0..MAX_SLOTS-1``."""
        self._check_index(index)
        if camera_id not in self._known:
            raise KeyError(camera_id)
        current = self.index_of(camera_id)
        if current == index:
            return False
        if current is not None:
            self._slots[current] = self._slots[index]
        self._slots[index] = camera_id
        return True

    def swap(self, a: int, b: int) -> bool:
        """Exchange slots ``a`` and ``b``."""
        self._check_index(a)
        self._check_index(b)
        if a == b or self._slots[a] == self._slots[b]:
            return False
        self._slots[a], self._slots[b] = self._slots[b], self._slots[a]
        return True

    def clear(self, index: int) -> bool:
        """Empty slot ``index``."""
        self._check_index(index)
        if self._slots[index] is None:
            return False
        self._slots[index] = None
        return True

    def remove(self, camera_id: str) -> bool:
        """Take ``camera_id`` out of the view (its slot becomes empty)."""
        index = self.index_of(camera_id)
        return index is not None and self.clear(index)

    def add(self, camera_id: str, visible_count: int, fallback: int | None = None) -> int | None:
        """Show ``camera_id`` in one of the first ``visible_count`` slots and return that slot.

        Already visible: unchanged. Else the first empty visible slot; if there is none, slot
        ``fallback`` (e.g. the selected tile) when it is visible, else the last visible slot.
        Returns ``None`` for an unknown camera or an empty grid."""
        count = max(0, min(visible_count, MAX_SLOTS))
        if camera_id not in self._known or count == 0:
            return None
        current = self.index_of(camera_id)
        if current is not None and current < count:
            return current
        visible = self._slots[:count]
        if None in visible:
            target = visible.index(None)
        elif fallback is not None and 0 <= fallback < count:
            target = fallback
        else:
            target = count - 1
        self.place(target, camera_id)
        return target

    def sync(self, camera_ids: Iterable[str]) -> bool:
        """Apply a new camera list: drop removed cameras, put new ones into the first empty slots."""
        ids = list(dict.fromkeys(camera_ids))
        present = set(ids)
        changed = False
        for i, cid in enumerate(self._slots):
            if cid is not None and cid not in present:
                self._slots[i] = None
                changed = True
        for cid in ids:
            if cid in self._known:
                continue
            self._known.add(cid)
            if cid not in self._slots and None in self._slots:
                self._slots[self._slots.index(None)] = cid
                changed = True
        self._known &= present  # a camera removed and added again later counts as new
        return changed

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _check_index(index: int) -> None:
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < MAX_SLOTS:
            raise IndexError(f"slot index must be 0..{MAX_SLOTS - 1}, got {index!r}")
