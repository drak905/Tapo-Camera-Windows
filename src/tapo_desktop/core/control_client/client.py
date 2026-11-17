"""Session-managing control client for the camera's HTTPS API (secure v3 and legacy login).

Credentials never appear in logs, exception messages or ``repr``.
"""
from __future__ import annotations

import datetime
from datetime import date

from tapo_desktop.core.control_client.transport import HttpsTransport, PinStore
from tapo_desktop.models import DayNightMode, DeviceInfo, Preset, RecordingClip


class TapoControlClient:
    """Thread-safe client. Auto-logs-in on first use; on -40401 (top-level or per-item) or HTTP 401
    it re-logs-in once and retries once. ``seq`` increments per secure request."""

    def __init__(self, host: str, password: str, *, username: str = "admin", port: int = 443,
                 transport: HttpsTransport | None = None, pin_store: PinStore | None = None,
                 timeout: float = 10.0):
        raise NotImplementedError

    def login(self) -> None:
        """Secure login (encrypt_type "3") if step 1 returns -40413 with a nonce, else legacy MD5 login.

        Bad device_confirm / bad password -> TapoAuthError; -40404 / lock codes -> TapoLockedError.
        """
        raise NotImplementedError

    @property
    def is_secure(self) -> bool:
        raise NotImplementedError

    @property
    def logged_in(self) -> bool:
        raise NotImplementedError

    def request(self, req: dict) -> dict:
        """Send one request via multipleRequest and return its ``result``.

        Non-zero item error_code -> TapoDeviceError(code, method).
        """
        raise NotImplementedError

    def multiple_request(self, reqs: list[dict]) -> list[dict]:
        """Send a batch; return raw items ``[{"method", "result", "error_code"}]``."""
        raise NotImplementedError

    def get_device_info(self) -> DeviceInfo:
        raise NotImplementedError

    def get_components(self) -> dict[str, int]:
        raise NotImplementedError

    def move(self, x: int, y: int) -> None:
        raise NotImplementedError

    def step(self, direction: int) -> None:
        raise NotImplementedError

    def stop_move(self) -> None:
        raise NotImplementedError

    def get_presets(self) -> list[Preset]:
        raise NotImplementedError

    def goto_preset(self, preset_id: str) -> None:
        raise NotImplementedError

    def add_preset(self, name: str) -> None:
        raise NotImplementedError

    def delete_preset(self, preset_id: str) -> None:
        raise NotImplementedError

    def get_privacy(self) -> bool:
        raise NotImplementedError

    def set_privacy(self, enabled: bool) -> None:
        raise NotImplementedError

    def get_day_night(self) -> DayNightMode:
        raise NotImplementedError

    def set_day_night(self, mode: DayNightMode) -> None:
        raise NotImplementedError

    def get_led(self) -> bool:
        raise NotImplementedError

    def set_led(self, enabled: bool) -> None:
        raise NotImplementedError

    def set_alarm(self, active: bool) -> None:
        raise NotImplementedError

    def get_motion_detection(self) -> tuple[bool, int | None]:
        raise NotImplementedError

    def set_motion_detection(self, enabled: bool, sensitivity: int | None = None) -> None:
        raise NotImplementedError

    def get_sd_status(self) -> list[dict]:
        raise NotImplementedError

    def days_with_recordings(self, start: date, end: date) -> list[date]:
        raise NotImplementedError

    def recordings_for_day(self, day: date, tz: datetime.tzinfo | None = None) -> list[RecordingClip]:
        """Clips of local day ``[00:00, 24:00)`` in ``tz`` (None = local timezone); pages until
        ``to_be_continued`` is 0."""
        raise NotImplementedError

    def reboot(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError
