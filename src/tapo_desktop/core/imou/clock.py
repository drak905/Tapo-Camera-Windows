"""The camera's wall clock for iMou recordings (pure code, no network).

The vendor's time fields (``beginTime``, ``endTime``, ``localBeginTime``...) are "wall-clock ``YYYY-MM-DD HH:mm:ss``"
with no zone. Which clock that is, is **not documented** (unverified). So:

* the vendor's own strings are kept and sent back unchanged; UTC is only for the timeline;
* :class:`ImouClock` turns a UTC epoch into such a string and back. Default: the PC's own zone. A camera can pin a
  fixed offset (``vendor_data["utc_offset_min"]``, minutes east of UTC);
* a position inside a recording is the recording's own string plus whole seconds (:meth:`ImouClock.shift`), never UTC math;
* formatting is done with plain integers: nothing here depends on the locale.

shortcut: a pinned offset is fixed (no daylight saving of the camera's zone); re-pin twice a year, or upgrade to a zone name.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

_WALL = re.compile(r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})")
MAX_OFFSET_MIN = 14 * 60


def parse_wall(text: Any) -> datetime | None:
    """A naive datetime from exactly ``YYYY-MM-DD HH:mm:ss``, else ``None``."""
    if not isinstance(text, str):
        return None
    m = _WALL.fullmatch(text)
    if m is None:
        return None
    try:
        return datetime(*(int(g) for g in m.groups()))
    except ValueError:
        return None


def format_wall(dt: datetime) -> str:
    return f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d} {dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"


class ImouClock:
    def __init__(self, offset_minutes: int | None = None):
        self._tz = None if offset_minutes is None else timezone(timedelta(minutes=int(offset_minutes)))

    @property
    def pinned(self) -> bool:
        return self._tz is not None

    def from_utc(self, epoch: float) -> str:
        dt = datetime.fromtimestamp(epoch, self._tz) if self._tz is not None else datetime.fromtimestamp(epoch)
        return format_wall(dt)

    def to_utc(self, text: Any) -> float | None:
        dt = parse_wall(text)
        if dt is None:
            return None
        return dt.replace(tzinfo=self._tz).timestamp() if self._tz is not None else dt.timestamp()

    @staticmethod
    def shift(text: str, seconds: float) -> str:
        """``text`` plus whole seconds (floored), on the camera's own clock."""
        dt = parse_wall(text)
        if dt is None:
            raise ValueError("not a vendor time text")
        return format_wall(dt + timedelta(seconds=int(seconds // 1)))

    def day_range(self, day: date) -> tuple[str, str]:
        """The PC's calendar ``day`` as ``(begin, end)`` texts on the camera's clock (end = last second of the day)."""
        start, end = pc_day_bounds(day)
        return self.from_utc(start), self.from_utc(end - 1)


def pc_day_bounds(day: date) -> tuple[float, float]:
    """Epoch seconds of the PC's local midnight of ``day`` and of the next day (23 or 25 h on daylight-saving days)."""
    start = datetime(day.year, day.month, day.day).astimezone()
    nxt = day + timedelta(days=1)
    end = datetime(nxt.year, nxt.month, nxt.day).astimezone()
    return start.timestamp(), end.timestamp()


def clock_for(config: Any) -> ImouClock:
    """The clock of a camera config: the pinned offset when ``vendor_data["utc_offset_min"]`` is a sane int."""
    value = (getattr(config, "vendor_data", None) or {}).get("utc_offset_min")
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) <= MAX_OFFSET_MIN:
        return ImouClock(value)
    return ImouClock()
