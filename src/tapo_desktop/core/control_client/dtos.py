"""Request builders and response parsers for the camera control API (pure, no I/O).

Each ``*_req`` returns one request dict ``{"method": ..., "params": {...}}`` exactly matching the
spec's params template; each ``parse_*`` takes that request's ``result`` dict. Parsers raise
TapoProtocolError (never KeyError/TypeError) on unusable data.
Spec: [spec:envelope.*], [spec:control.*], [spec:capability.component_list], [spec:vod.search.*].
"""
from __future__ import annotations

import datetime

from tapo_desktop.models import DayNightMode, DeviceInfo, Preset, RecordingClip


def multiple_request(requests: list[dict]) -> dict:
    """``{"method":"multipleRequest","params":{"requests":[...]}}`` [spec:envelope.multiple_request]."""
    raise NotImplementedError


def get_device_info_req() -> dict:
    """getDeviceInfo [spec:control.device_info.get]."""
    raise NotImplementedError


def parse_device_info(result: dict) -> DeviceInfo:
    """Parse ``result.device_info.basic_info``."""
    raise NotImplementedError


def get_component_list_req() -> dict:
    """getAppComponentList [spec:capability.component_list]."""
    raise NotImplementedError


def parse_component_list(result: dict) -> dict[str, int]:
    """``{component_name: version}`` from ``result.app_component.app_component_list``."""
    raise NotImplementedError


def motor_move_req(x: int, y: int) -> dict:
    """motorMove with x_coord/y_coord as integer strings [spec:control.ptz.move]."""
    raise NotImplementedError


def motor_step_req(direction: int) -> dict:
    """singalMove; ``direction`` in {0, 90, 180, 270} else ValueError [spec:control.ptz.move_step]."""
    raise NotImplementedError


def motor_stop_req() -> dict:
    """stopMove [spec:control.ptz.stop]."""
    raise NotImplementedError


def get_presets_req() -> dict:
    """getPresetConfig [spec:control.preset.list]."""
    raise NotImplementedError


def parse_presets(result: dict) -> list[Preset]:
    """Parallel arrays in ``result.preset.preset`` -> presets."""
    raise NotImplementedError


def goto_preset_req(preset_id: str) -> dict:
    """motorMoveToPreset [spec:control.preset.goto]."""
    raise NotImplementedError


def add_preset_req(name: str) -> dict:
    """addMotorPostion with save_ptz "1", no id [spec:control.preset.add]."""
    raise NotImplementedError


def delete_preset_req(preset_id: str) -> dict:
    """deletePreset [spec:control.preset.delete]."""
    raise NotImplementedError


def get_privacy_req() -> dict:
    """getLensMaskConfig [spec:control.privacy.get]."""
    raise NotImplementedError


def parse_privacy(result: dict) -> bool:
    """``result.lens_mask.lens_mask_info.enabled == "on"``."""
    raise NotImplementedError


def set_privacy_req(enabled: bool) -> dict:
    """setLensMaskConfig [spec:control.privacy.set]."""
    raise NotImplementedError


def get_day_night_req() -> dict:
    """getDayNightModeConfig [spec:control.day_night.get]."""
    raise NotImplementedError


def parse_day_night(result: dict) -> DayNightMode:
    """``result.image.common.inf_type`` -> DayNightMode."""
    raise NotImplementedError


def set_day_night_req(mode: DayNightMode) -> dict:
    """setDayNightModeConfig [spec:control.day_night.set]."""
    raise NotImplementedError


def get_led_req() -> dict:
    """getLedStatus [spec:control.led.get]."""
    raise NotImplementedError


def parse_led(result: dict) -> bool:
    """``result.led.config.enabled == "on"``."""
    raise NotImplementedError


def set_led_req(enabled: bool) -> dict:
    """setLedStatus [spec:control.led.set]."""
    raise NotImplementedError


def manual_alarm_req(start: bool) -> dict:
    """doManualAlarm action start/stop [spec:control.alarm.manual]."""
    raise NotImplementedError


def get_motion_detection_req() -> dict:
    """getDetectionConfig [spec:control.motion_detection.get]."""
    raise NotImplementedError


def parse_motion_detection(result: dict) -> tuple[bool, int | None]:
    """``(enabled, digital_sensitivity or None)`` from ``result.motion_detection.motion_det``."""
    raise NotImplementedError


def set_motion_detection_req(enabled: bool, sensitivity: int | None = None) -> dict:
    """setDetectionConfig; ``sensitivity`` in {20, 60, 80} else ValueError [spec:control.motion_detection.set]."""
    raise NotImplementedError


def get_sd_status_req() -> dict:
    """getSdCardStatus [spec:control.sd.get_status]."""
    raise NotImplementedError


def parse_sd_status(result: dict) -> list[dict]:
    """Unwrap ``result.harddisk_manage.hd_info`` single-key maps into a list of disk dicts."""
    raise NotImplementedError


def search_days_req(start: datetime.date, end: datetime.date) -> dict:
    """searchDateWithVideo, dates yyyyMMdd, channel [0] [spec:vod.search.days]."""
    raise NotImplementedError


def parse_days(result: dict) -> list[datetime.date]:
    """Dates from ``result.playback.search_results`` [spec:vod.search.days.response]."""
    raise NotImplementedError


def search_day_clips_req(day_start_utc: int, day_end_utc: int, start_index: int = 0, end_index: int = 99) -> dict:
    """searchVideoWithUTC (legacy-UTC shape with id 1, channel 0) [spec:vod.search.day_clips_utc]."""
    raise NotImplementedError


def parse_day_clips(result: dict) -> list[RecordingClip]:
    """Clips from ``result.playback.search_video_results`` [spec:vod.search.day_clips.response]."""
    raise NotImplementedError


def reboot_req() -> dict:
    """rebootDevice [spec:control.system.reboot]."""
    raise NotImplementedError
