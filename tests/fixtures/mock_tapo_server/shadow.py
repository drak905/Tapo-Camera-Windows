"""DeviceShadow: the live, mutable, thread-safe state of the mock camera."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from tapo_desktop.models import RecordingClip

PAN_LIMIT = 180   # ptz_x in [-PAN_LIMIT, PAN_LIMIT]
TILT_LIMIT = 90   # ptz_y in [-TILT_LIMIT, TILT_LIMIT]
MAX_PRESETS = 8
STEP = 10         # singalMove / ONVIF unit velocity step in ptz units


def _utc_midnight(d: datetime) -> datetime:
    return d.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def default_recordings(now: datetime | None = None) -> list[RecordingClip]:
    """3 clips today (UTC) and 1 clip yesterday, sorted by start time."""
    today = _utc_midnight(now or datetime.now(timezone.utc))
    yday = today - timedelta(days=1)

    def clip(base: datetime, h: int, m: int, dur: int, ev: int) -> RecordingClip:
        start = base + timedelta(hours=h, minutes=m)
        return RecordingClip(start=start, end=start + timedelta(seconds=dur), event_type=ev)

    return [clip(yday, 12, 0, 60, 2), clip(today, 1, 0, 60, 2), clip(today, 2, 0, 120, 1), clip(today, 3, 0, 30, 6)]


def default_components(dual_lens: bool = False) -> dict[str, int]:
    comps = {"ptz": 1, "lensMask": 2, "led": 1, "dayNightMode": 1, "nightVisionMode": 1, "detection": 3,
             "playback": 2, "sdCard": 1, "manualAlarm": 1, "system": 3, "reboot": 1, "video": 2,
             "audio": 1, "timezone": 1}
    if dual_lens:
        comps["dualCam"] = 1
        comps["multiLensCam"] = 1
    return comps


@dataclass
class DeviceShadow:
    """Device state. Mutate fields directly from tests; servers take ``lock`` while reading/writing."""

    privacy: bool = False
    led: bool = True
    inf_type: str = "auto"                       # auto | on | off
    night_vision_mode: str = "inf_night_vision"
    motion_enabled: bool = True
    motion_sensitivity: int = 60                 # digital_sensitivity 0..100 (UI uses 20/60/80)
    alarm_active: bool = False
    ptz_x: int = 0
    ptz_y: int = 0
    presets: dict[str, dict] = field(default_factory=lambda: {
        "1": {"name": "Door", "x": 20, "y": 0, "read_only": False},
        "2": {"name": "Gate", "x": -60, "y": 10, "read_only": False},
    })                                            # id -> {"name", "x", "y", "read_only"} (ptz units)
    recordings: list[RecordingClip] = field(default_factory=default_recordings)
    components: dict[str, int] = field(default_factory=default_components)
    # identity / extras (not in the contract list, but used by getDeviceInfo etc.)
    model: str = "C210"
    alias: str = "Mock Camera"
    firmware: str = "1.3.11 Build 231019 Rel.59330n"
    hardware: str = "2.0"
    mac: str = "AA-BB-CC-DD-EE-FF"
    device_id: str = "80231A1F3B9E2C4D5E6F708192A3B4C5D6E7F801"
    sd_status: str = "normal"
    ptz_moving: bool = False
    reboot_count: int = 0
    last_alarm_time: int = 0
    last_alarm_type: str = "motion"
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)

    def clamp_move(self, dx: int, dy: int) -> bool:
        """Move by (dx, dy) with clamping. Returns False if no axis could move (at the boundary)."""
        with self.lock:
            nx = max(-PAN_LIMIT, min(PAN_LIMIT, self.ptz_x + dx))
            ny = max(-TILT_LIMIT, min(TILT_LIMIT, self.ptz_y + dy))
            moved = (nx, ny) != (self.ptz_x, self.ptz_y)
            if (dx or dy) and not moved:
                return False
            self.ptz_x, self.ptz_y = nx, ny
            return True

    def next_preset_id(self) -> str:
        with self.lock:
            i = 1
            while str(i) in self.presets:
                i += 1
            return str(i)
