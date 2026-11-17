"""Request builders and response parsers for the camera control API (pure, no I/O).

Each ``*_req`` returns one request dict ``{"method": ..., "params": {...}}`` exactly matching the
spec's params template; each ``parse_*`` takes that request's ``result`` dict. Parsers raise
TapoProtocolError (never KeyError/TypeError) on unusable data.
Spec: [spec:envelope.*], [spec:control.*], [spec:capability.component_list], [spec:vod.search.*].
"""
from __future__ import annotations

import datetime
from typing import Any

from tapo_desktop.core.errors import TapoProtocolError
from tapo_desktop.models import DayNightMode, DeviceInfo, Preset, RecordingClip

_STEP_DIRECTIONS = frozenset({0, 90, 180, 270})
_SENSITIVITY_LEVELS = frozenset({20, 60, 80})
_ON_OFF = {"on": True, "off": False}
_DATE_FMT = "%Y%m%d"


# --------------------------------------------------------------------------- private helpers
def _req(method: str, params: dict) -> dict:
    return {"method": method, "params": params}


def _dig(obj: Any, *keys: str) -> Any:
    """Follow ``keys`` through nested dicts; TapoProtocolError if a level is missing or not a dict."""
    for key in keys:
        if not isinstance(obj, dict) or key not in obj:
            raise TapoProtocolError(f"response is missing '{key}'")
        obj = obj[key]
    return obj


def _dig_dict(obj: Any, *keys: str) -> dict:
    value = _dig(obj, *keys)
    if not isinstance(value, dict):
        raise TapoProtocolError(f"'{keys[-1]}' is not an object")
    return value


def _dig_list(obj: Any, *keys: str) -> list:
    value = _dig(obj, *keys)
    if not isinstance(value, list):
        raise TapoProtocolError(f"'{keys[-1]}' is not a list")
    return value


def _on_off(value: Any, what: str) -> bool:
    if isinstance(value, str) and value in _ON_OFF:
        return _ON_OFF[value]
    raise TapoProtocolError(f"{what} must be 'on' or 'off'")


def _to_int(value: Any, what: str) -> int:
    """int from an int or an integer string (the camera string-encodes most numbers)."""
    if isinstance(value, bool):
        raise TapoProtocolError(f"{what} is not an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            pass
    raise TapoProtocolError(f"{what} is not an integer")


def _to_float(value: Any, what: str) -> float:
    if isinstance(value, (int, float, str)) and not isinstance(value, bool):
        try:
            return float(value)
        except ValueError:
            pass
    raise TapoProtocolError(f"{what} is not a number")


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _unwrap_items(items: list, what: str) -> list[dict]:
    """``[{"x_1": {...}}, {"x_2": {...}}]`` -> ``[{...}, {...}]``; flat dicts are kept as they are."""
    out = []
    for item in items:
        if not isinstance(item, dict):
            raise TapoProtocolError(f"{what} item is not an object")
        if len(item) == 1:
            (inner,) = item.values()
            if isinstance(inner, dict):
                out.append(inner)
                continue
        out.append(item)
    return out


def _utc(ts: int) -> datetime.datetime:
    try:
        return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise TapoProtocolError(f"timestamp {ts} is out of range") from exc


# --------------------------------------------------------------------------- envelope
def multiple_request(requests: list[dict]) -> dict:
    """``{"method":"multipleRequest","params":{"requests":[...]}}`` [spec:envelope.multiple_request]."""
    return _req("multipleRequest", {"requests": list(requests)})


# --------------------------------------------------------------------------- device / capabilities
def get_device_info_req() -> dict:
    """getDeviceInfo [spec:control.device_info.get]."""
    return _req("getDeviceInfo", {"device_info": {"name": ["basic_info"]}})


def parse_device_info(result: dict) -> DeviceInfo:
    """Parse ``result.device_info.basic_info``."""
    info = _dig_dict(result, "device_info", "basic_info")
    model = info.get("device_model")
    if not isinstance(model, str) or not model:
        raise TapoProtocolError("basic_info.device_model is missing")
    return DeviceInfo(model=model, alias=_text(info.get("device_alias")), firmware=_text(info.get("sw_version")),
                      hardware=_text(info.get("hw_version")), mac=_text(info.get("mac")),
                      device_id=_text(info.get("dev_id")), raw=dict(info))


def get_component_list_req() -> dict:
    """getAppComponentList [spec:capability.component_list]."""
    return _req("getAppComponentList", {"app_component": {"name": ["app_component_list"]}})


def parse_component_list(result: dict) -> dict[str, int]:
    """``{component_name: version}`` from ``result.app_component.app_component_list``."""
    out: dict[str, int] = {}
    for item in _dig_list(result, "app_component", "app_component_list"):
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise TapoProtocolError("app_component_list item has no name")
        out[item["name"]] = _to_int(item.get("version"), f"version of {item['name']}")
    return out


# --------------------------------------------------------------------------- PTZ
def motor_move_req(x: int, y: int) -> dict:
    """motorMove with x_coord/y_coord as integer strings [spec:control.ptz.move]."""
    return _req("motorMove", {"motor": {"move": {"x_coord": str(int(x)), "y_coord": str(int(y))}}})


def motor_step_req(direction: int) -> dict:
    """singalMove; ``direction`` in {0, 90, 180, 270} else ValueError [spec:control.ptz.move_step]."""
    if isinstance(direction, bool) or direction not in _STEP_DIRECTIONS:
        raise ValueError(f"direction must be one of {sorted(_STEP_DIRECTIONS)}, got {direction!r}")
    return _req("singalMove", {"motor": {"movestep": {"direction": str(int(direction))}}})


def motor_stop_req() -> dict:
    """stopMove [spec:control.ptz.stop]."""
    return _req("stopMove", {"motor": {"stop": "null"}})


# --------------------------------------------------------------------------- presets
def get_presets_req() -> dict:
    """getPresetConfig [spec:control.preset.list]."""
    return _req("getPresetConfig", {"preset": {"name": ["preset"]}})


def parse_presets(result: dict) -> list[Preset]:
    """Parallel arrays in ``result.preset.preset`` -> presets."""
    table = _dig_dict(result, "preset", "preset")
    ids = _dig_list(table, "id")
    names = _dig_list(table, "name")
    columns: dict[str, list] = {}
    for key in ("position_pan", "position_tilt", "read_only"):
        col = table.get(key)
        if col is None:
            continue
        if not isinstance(col, list) or len(col) != len(ids):
            raise TapoProtocolError(f"preset.{key} does not match preset.id")
        columns[key] = col
    if len(names) != len(ids):
        raise TapoProtocolError("preset.name does not match preset.id")
    presets = []
    for i, (pid, name) in enumerate(zip(ids, names)):
        if not isinstance(pid, (str, int)) or isinstance(pid, bool) or not isinstance(name, str):
            raise TapoProtocolError("preset id/name has the wrong type")

        def col(key: str, default: Any) -> Any:
            return columns[key][i] if key in columns else default

        presets.append(Preset(id=str(pid), name=name,
                              pan=_to_float(col("position_pan", 0.0), "position_pan"),
                              tilt=_to_float(col("position_tilt", 0.0), "position_tilt"),
                              read_only=_to_int(col("read_only", 0), "read_only") != 0))
    return presets


def goto_preset_req(preset_id: str) -> dict:
    """motorMoveToPreset [spec:control.preset.goto]."""
    return _req("motorMoveToPreset", {"preset": {"goto_preset": {"id": str(preset_id)}}})


def add_preset_req(name: str) -> dict:
    """addMotorPostion with save_ptz "1", no id [spec:control.preset.add]."""
    return _req("addMotorPostion", {"preset": {"set_preset": {"name": name, "save_ptz": "1"}}})


def delete_preset_req(preset_id: str) -> dict:
    """deletePreset [spec:control.preset.delete]."""
    return _req("deletePreset", {"preset": {"remove_preset": {"id": [str(preset_id)]}}})


# --------------------------------------------------------------------------- toggles
def get_privacy_req() -> dict:
    """getLensMaskConfig [spec:control.privacy.get]."""
    return _req("getLensMaskConfig", {"lens_mask": {"name": ["lens_mask_info"]}})


def parse_privacy(result: dict) -> bool:
    """``result.lens_mask.lens_mask_info.enabled == "on"``."""
    return _on_off(_dig(result, "lens_mask", "lens_mask_info", "enabled"), "lens_mask_info.enabled")


def set_privacy_req(enabled: bool) -> dict:
    """setLensMaskConfig [spec:control.privacy.set]."""
    return _req("setLensMaskConfig", {"lens_mask": {"lens_mask_info": {"enabled": "on" if enabled else "off"}}})


def get_day_night_req() -> dict:
    """getDayNightModeConfig [spec:control.day_night.get]."""
    return _req("getDayNightModeConfig", {"image": {"name": "common"}})


def parse_day_night(result: dict) -> DayNightMode:
    """``result.image.common.inf_type`` -> DayNightMode."""
    value = _dig(result, "image", "common", "inf_type")
    try:
        return DayNightMode(value)
    except ValueError as exc:
        raise TapoProtocolError("image.common.inf_type has an unknown value") from exc


def set_day_night_req(mode: DayNightMode) -> dict:
    """setDayNightModeConfig [spec:control.day_night.set]."""
    return _req("setDayNightModeConfig", {"image": {"common": {"inf_type": DayNightMode(mode).value}}})


def get_led_req() -> dict:
    """getLedStatus [spec:control.led.get]."""
    return _req("getLedStatus", {"led": {"name": ["config"]}})


def parse_led(result: dict) -> bool:
    """``result.led.config.enabled == "on"``."""
    return _on_off(_dig(result, "led", "config", "enabled"), "led.config.enabled")


def set_led_req(enabled: bool) -> dict:
    """setLedStatus [spec:control.led.set]."""
    return _req("setLedStatus", {"led": {"config": {"enabled": "on" if enabled else "off"}}})


def manual_alarm_req(start: bool) -> dict:
    """doManualAlarm action start/stop [spec:control.alarm.manual]."""
    return _req("doManualAlarm", {"msg_alarm": {"manual_alarm": {"action": "start" if start else "stop"}}})


# --------------------------------------------------------------------------- motion detection
def get_motion_detection_req() -> dict:
    """getDetectionConfig [spec:control.motion_detection.get]."""
    return _req("getDetectionConfig", {"motion_detection": {"name": ["motion_det"], "table": ["region_info"]}})


def parse_motion_detection(result: dict) -> tuple[bool, int | None]:
    """``(enabled, digital_sensitivity or None)`` from ``result.motion_detection.motion_det``."""
    det = _dig_dict(result, "motion_detection", "motion_det")
    enabled = _on_off(det.get("enabled"), "motion_det.enabled")
    raw = det.get("digital_sensitivity")
    return enabled, (None if raw is None else _to_int(raw, "digital_sensitivity"))


def set_motion_detection_req(enabled: bool, sensitivity: int | None = None) -> dict:
    """setDetectionConfig; ``sensitivity`` in {20, 60, 80} else ValueError [spec:control.motion_detection.set]."""
    det: dict[str, str] = {"enabled": "on" if enabled else "off"}
    if sensitivity is not None:
        if isinstance(sensitivity, bool) or sensitivity not in _SENSITIVITY_LEVELS:
            raise ValueError(f"sensitivity must be one of {sorted(_SENSITIVITY_LEVELS)}, got {sensitivity!r}")
        det["digital_sensitivity"] = str(int(sensitivity))
    return _req("setDetectionConfig", {"motion_detection": {"motion_det": det}})


# --------------------------------------------------------------------------- SD card
def get_sd_status_req() -> dict:
    """getSdCardStatus [spec:control.sd.get_status]."""
    return _req("getSdCardStatus", {"harddisk_manage": {"table": ["hd_info"]}})


def parse_sd_status(result: dict) -> list[dict]:
    """Unwrap ``result.harddisk_manage.hd_info`` single-key maps into a list of disk dicts."""
    return [dict(d) for d in _unwrap_items(_dig_list(result, "harddisk_manage", "hd_info"), "hd_info")]


# --------------------------------------------------------------------------- recordings search
def search_days_req(start: datetime.date, end: datetime.date) -> dict:
    """searchDateWithVideo, dates yyyyMMdd, channel [0] [spec:vod.search.days]."""
    return _req("searchDateWithVideo", {"playback": {"search_year_utility": {
        "channel": [0], "start_date": start.strftime(_DATE_FMT), "end_date": end.strftime(_DATE_FMT)}}})


def parse_days(result: dict) -> list[datetime.date]:
    """Dates from ``result.playback.search_results`` [spec:vod.search.days.response]."""
    playback = _dig_dict(result, "playback")
    items = playback.get("search_results", [])
    if not isinstance(items, list):
        raise TapoProtocolError("playback.search_results is not a list")
    days = set()
    for item in _unwrap_items(items, "search_results"):
        value = item.get("date")
        if not isinstance(value, str) or len(value) != 8 or not value.isdigit():
            raise TapoProtocolError("search_results date is not yyyyMMdd")
        try:
            days.add(datetime.datetime.strptime(value, _DATE_FMT).date())
        except ValueError as exc:
            raise TapoProtocolError("search_results date is not a valid date") from exc
    return sorted(days)


def search_day_clips_req(day_start_utc: int, day_end_utc: int, start_index: int = 0, end_index: int = 99, *,
                         player_id: str | None = None) -> dict:
    """searchVideoWithUTC, channel 0 [spec:vod.search.day_clips_utc].

    Default: the legacy-UTC shape with ``"id": 1``. With ``player_id`` (32 hex chars) the ``id`` key is
    replaced by ``"player_id": <player_id>``: some firmware (real C510W) answers the ``id`` form with
    -71103 and accepts only this one."""
    key: dict[str, Any] = {"player_id": str(player_id)} if player_id else {"id": 1}
    return _req("searchVideoWithUTC", {"playback": {"search_video_with_utc": {
        **key, "channel": 0, "start_time": int(day_start_utc), "end_time": int(day_end_utc),
        "start_index": int(start_index), "end_index": int(end_index)}}})


def parse_day_clips(result: dict) -> list[RecordingClip]:
    """Clips from ``result.playback.search_video_results`` [spec:vod.search.day_clips.response]."""
    playback = _dig_dict(result, "playback")
    items = playback.get("search_video_results", [])
    if not isinstance(items, list):
        raise TapoProtocolError("playback.search_video_results is not a list")
    clips = []
    for item in _unwrap_items(items, "search_video_results"):
        start = _utc(_to_int(item.get("startTime"), "startTime"))
        end = _utc(_to_int(item.get("endTime"), "endTime"))
        kind = item.get("video_type", item.get("vedio_type"))
        event_type = 0 if kind is None else _to_int(kind, "video_type")
        clips.append(RecordingClip(start=start, end=end, event_type=event_type))
    return clips


def has_more_clips(result: dict) -> bool:
    """True if a searchVideoWithUTC ``result`` says another page follows (``to_be_continued`` != 0)."""
    if not isinstance(result, dict) or not isinstance(result.get("playback"), dict):
        return False
    flag = result["playback"].get("to_be_continued", 0)
    if isinstance(flag, bool):
        return flag
    try:
        return _to_int(flag, "to_be_continued") != 0
    except TapoProtocolError:
        return False


# --------------------------------------------------------------------------- system
def reboot_req() -> dict:
    """rebootDevice [spec:control.system.reboot]."""
    return _req("rebootDevice", {"system": {"reboot": {}}})
