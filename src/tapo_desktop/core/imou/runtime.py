"""The iMou runtime: what every iMou camera service shares, built by the account manager once connected.

* ``client`` / ``guard``: the strict API client and its call guard;
* ``limits()``: read on every start, so a changed setting applies to the next click (cap, daily seconds, idle seconds,
  first-frame timeout);
* ``meter``: the local "estimate" of live seconds per camera per UTC day (the vendor's own meter is "My Resources" in
  the Open Platform console);
* the live-slot set: iMou's own cap, separate from every other camera type;
* ``supervisor_factory``: creates the stream supervisor (a fake in tests).
Pure Python, thread-safe.
"""
from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class ImouLimits:
    max_live: int = 2             # iMou live streams at once (own cap)
    daily_seconds: float = 1200.0  # per camera per UTC day (20 minutes)
    idle_seconds: float = 600.0   # a tile stops after this long and asks for a click (10 minutes)
    stall_timeout: float = 30.0   # first frame / frame gap before a stream counts as failed


class UsageMeter:
    """Live seconds per camera for the current UTC day. ``load`` / ``save`` plug in a settings key."""

    def __init__(self, clock: Callable[[], float] = time.time, load: Callable[[], Any] | None = None,
                 save: Callable[[dict], None] | None = None):
        self._clock = clock
        self._save = save
        self._lock = threading.Lock()
        self._day = ""
        self._seconds: dict[str, float] = {}
        raw = load() if load is not None else None
        if isinstance(raw, dict) and isinstance(raw.get("day"), str) and isinstance(raw.get("seconds"), dict):
            self._day = raw["day"]
            self._seconds = {k: float(v) for k, v in raw["seconds"].items() if isinstance(k, str)
                             and isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0}

    def _today(self) -> str:
        return datetime.fromtimestamp(self._clock(), tz=timezone.utc).strftime("%Y-%m-%d")

    def _roll_locked(self) -> None:
        today = self._today()
        if self._day != today:
            self._day, self._seconds = today, {}

    def used(self, camera_id: str) -> float:
        with self._lock:
            self._roll_locked()
            return self._seconds.get(camera_id, 0.0)

    def remaining(self, camera_id: str, limit_seconds: float) -> float:
        return max(0.0, limit_seconds - self.used(camera_id))

    def add(self, camera_id: str, seconds: float) -> None:
        if not (isinstance(seconds, (int, float)) and math.isfinite(seconds) and seconds > 0):
            return
        with self._lock:
            self._roll_locked()
            self._seconds[camera_id] = self._seconds.get(camera_id, 0.0) + float(seconds)
            state = {"day": self._day, "seconds": dict(self._seconds)}
        if self._save is not None:
            self._save(state)


class ImouRuntime:
    def __init__(self, *, client: Any, guard: Any, clock: Callable[[], float] = time.time,
                 limits: Callable[[], ImouLimits] | None = None, meter: UsageMeter | None = None,
                 supervisor_factory: Callable[..., Any] | None = None):
        self.client = client
        self.guard = guard
        self.clock = clock
        self.limits = limits or ImouLimits
        self.meter = meter or UsageMeter(clock)
        if supervisor_factory is None:
            from tapo_desktop.core.imou.stream import make_supervisor

            supervisor_factory = make_supervisor
        self.supervisor_factory = supervisor_factory
        self._lock = threading.Lock()
        self._live: set[str] = set()

    def live_ids(self) -> set[str]:
        with self._lock:
            return set(self._live)

    def try_acquire(self, camera_id: str, cap: int) -> bool:
        """Take a live slot for ``camera_id`` (idempotent for a camera that already has one)."""
        with self._lock:
            if camera_id in self._live:
                return True
            if len(self._live) >= max(1, int(cap)):
                return False
            self._live.add(camera_id)
            return True

    def release(self, camera_id: str) -> None:
        with self._lock:
            self._live.discard(camera_id)
