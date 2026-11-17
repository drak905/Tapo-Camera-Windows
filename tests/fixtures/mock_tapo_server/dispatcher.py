"""multipleRequest command dispatcher operating on a DeviceShadow.

Implements every command behind docs/CONTRACT_CORE.md dtos ([spec:control.*], [spec:vod.search.*],
[spec:sd.status]) plus a few related reads. Each handler returns the ``result`` dict or raises
``CommandError(code)``. Error codes:

* -40106 unknown method            [spec:error_code.common]
* -40211 missing necessary params  (module/section/field absent, selector does not name the section)
* -40210 invalid param value       (wrong type / value outside the documented enum)
* -64304 PTZ boundary reached; -64306 preset limit reached  [spec:error_code.ptz]
"""
from __future__ import annotations

import time
from datetime import date, datetime, timezone
from typing import Any, Callable

from .shadow import MAX_PRESETS, PAN_LIMIT, STEP, TILT_LIMIT, DeviceShadow


class CommandError(Exception):
    def __init__(self, code: int):
        super().__init__(code)
        self.code = code


MISSING = -40211
INVALID = -40210
UNSUPPORTED = -40106


def _module(params: Any, module: str) -> dict:
    if not isinstance(params, dict) or not isinstance(params.get(module), dict):
        raise CommandError(MISSING)
    return params[module]


def _section(params: Any, module: str, section: str) -> dict:
    mod = _module(params, module)
    data = mod.get(section)
    if not isinstance(data, dict):
        raise CommandError(MISSING)
    return data


def _require_selector(params: Any, module: str, section: str, kinds: tuple[str, ...] = ("name", "table")) -> None:
    """Read requests: {module: {"name"|"table": [section] | section}} [spec:envelope.get_selector_forms]."""
    mod = _module(params, module)
    for k in kinds:
        v = mod.get(k)
        if v == section or (isinstance(v, list) and section in v):
            return
    raise CommandError(MISSING)


def _onoff(value: Any) -> bool:
    if value == "on":
        return True
    if value == "off":
        return False
    raise CommandError(INVALID)


def _int_str(value: Any) -> int:
    if isinstance(value, bool):
        raise CommandError(INVALID)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            raise CommandError(INVALID) from None
    raise CommandError(INVALID)


def _strict_str_int(value: Any) -> int:
    """Fields the spec types as string(int): must be a JSON string."""
    if not isinstance(value, str):
        raise CommandError(INVALID)
    return _int_str(value)


def _onoff_str(b: bool) -> str:
    return "on" if b else "off"


# ---------------------------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------------------------
def get_device_info(s: DeviceShadow, p: Any) -> dict:
    _require_selector(p, "device_info", "basic_info", ("name",))
    return {"device_info": {"basic_info": {
        "device_type": "SMART.IPCAMERA", "device_model": s.model, "device_name": s.model,
        "device_info": f"{s.model} {s.hardware} IPC", "hw_version": s.hardware, "sw_version": s.firmware,
        "device_alias": s.alias, "mac": s.mac, "dev_id": s.device_id, "oem_id": "4F62C0C0A1B2C3D4E5F6",
        "hw_id": "8E1B2C3D4E5F60718293A4B5C6D7E8F9", "features": "3", "region": "EU", "barcode": "",
        "ffs": False, "no_rtsp_constrain": 1, "has_set_location_info": 0, "latitude": 0, "longitude": 0,
        "led_status": _onoff_str(s.led), "status": "configured"}}}


def get_component_list(s: DeviceShadow, p: Any) -> dict:
    _require_selector(p, "app_component", "app_component_list", ("name",))
    return {"app_component": {"app_component_list": [{"name": k, "version": v} for k, v in s.components.items()]}}


def _ptz_status(s: DeviceShadow) -> dict:
    return {"x_curpos": round(s.ptz_x / PAN_LIMIT, 4), "y_curpos": round(s.ptz_y / TILT_LIMIT, 4),
            "timestamp": int(time.time())}


def motor_move(s: DeviceShadow, p: Any) -> dict:
    d = _section(p, "motor", "move")
    if "x_coord" not in d or "y_coord" not in d:
        raise CommandError(MISSING)
    x, y = _strict_str_int(d["x_coord"]), _strict_str_int(d["y_coord"])
    if not s.clamp_move(x, y):
        raise CommandError(-64304)
    return {"move_time": 0.5, "x_destpos": s.ptz_x, "y_destpos": s.ptz_y, **_ptz_status(s)}


_STEP_DIRS = {"0": (STEP, 0), "90": (0, STEP), "180": (-STEP, 0), "270": (0, -STEP)}


def motor_step(s: DeviceShadow, p: Any) -> dict:
    d = _section(p, "motor", "movestep")
    if "direction" not in d:
        raise CommandError(MISSING)
    if d["direction"] not in _STEP_DIRS:
        raise CommandError(INVALID)
    dx, dy = _STEP_DIRS[d["direction"]]
    if not s.clamp_move(dx, dy):
        raise CommandError(-64304)
    s.ptz_moving = True
    return {}


def motor_stop(s: DeviceShadow, p: Any) -> dict:
    mod = _module(p, "motor")
    if mod.get("stop") != "null":
        raise CommandError(MISSING)
    s.ptz_moving = False
    return {"status_pan": "idle", "status_tilt": "idle", **_ptz_status(s)}


def get_presets(s: DeviceShadow, p: Any) -> dict:
    _require_selector(p, "preset", "preset", ("name",))
    ids = sorted(s.presets, key=lambda k: (len(k), k))
    pr = [s.presets[i] for i in ids]
    return {"preset": {"preset": {
        "id": ids, "name": [x["name"] for x in pr],
        "position_pan": [f"{x['x'] / PAN_LIMIT:.6f}" for x in pr],
        "position_tilt": [f"{x['y'] / TILT_LIMIT:.6f}" for x in pr],
        "read_only": ["1" if x.get("read_only") else "0" for x in pr]}}}


def goto_preset(s: DeviceShadow, p: Any) -> dict:
    d = _section(p, "preset", "goto_preset")
    pid = d.get("id")
    if pid is None:
        raise CommandError(MISSING)
    if not isinstance(pid, str) or pid not in s.presets:
        raise CommandError(INVALID)
    s.ptz_x, s.ptz_y = s.presets[pid]["x"], s.presets[pid]["y"]
    return {}


def add_preset(s: DeviceShadow, p: Any) -> dict:
    d = _section(p, "preset", "set_preset")
    name = d.get("name")
    if not isinstance(name, str) or d.get("save_ptz") is None:
        raise CommandError(MISSING)
    if d.get("save_ptz") != "1" or not name:
        raise CommandError(INVALID)
    pid = d.get("id")
    if pid is not None and not isinstance(pid, str):
        raise CommandError(INVALID)
    if pid not in s.presets and len(s.presets) >= MAX_PRESETS:
        raise CommandError(-64306)
    pid = pid or s.next_preset_id()
    s.presets[pid] = {"name": name, "x": s.ptz_x, "y": s.ptz_y, "read_only": False}
    return {"id": pid}


def delete_preset(s: DeviceShadow, p: Any) -> dict:
    d = _section(p, "preset", "remove_preset")
    ids = d.get("id")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
        raise CommandError(MISSING if ids is None else INVALID)
    if not any(i in s.presets for i in ids):
        raise CommandError(INVALID)
    for i in ids:
        s.presets.pop(i, None)
    return {}


def get_privacy(s: DeviceShadow, p: Any) -> dict:
    _require_selector(p, "lens_mask", "lens_mask_info", ("name",))
    return {"lens_mask": {"lens_mask_info": {"enabled": _onoff_str(s.privacy)}}}


def set_privacy(s: DeviceShadow, p: Any) -> dict:
    d = _section(p, "lens_mask", "lens_mask_info")
    if "enabled" not in d:
        raise CommandError(MISSING)
    s.privacy = _onoff(d["enabled"])
    return {}


def get_day_night(s: DeviceShadow, p: Any) -> dict:
    _require_selector(p, "image", "common", ("name",))
    return {"image": {"common": {"inf_type": s.inf_type, "light_freq_mode": "auto", "wide_dynamic": "off",
                                 "inf_start_time": "64800", "inf_end_time": "21600", "inf_sensitivity": "4"}}}


def set_day_night(s: DeviceShadow, p: Any) -> dict:
    d = _section(p, "image", "common")
    if "inf_type" not in d:
        raise CommandError(MISSING)
    if d["inf_type"] not in ("auto", "on", "off"):
        raise CommandError(INVALID)
    s.inf_type = d["inf_type"]
    return {}


_NV_MODES = {"inf_night_vision", "wtl_night_vision", "md_night_vision", "shed_night_vision",
             "dbl_night_vision", "lamp_closed_vision"}


def get_night_vision(s: DeviceShadow, p: Any) -> dict:
    _require_selector(p, "image", "switch", ("name",))
    return {"image": {"switch": {"night_vision_mode": s.night_vision_mode, "flip_type": "off", "ldc": "off"}}}


def set_night_vision(s: DeviceShadow, p: Any) -> dict:
    d = _section(p, "image", "switch")
    if "night_vision_mode" not in d:
        raise CommandError(MISSING)
    if d["night_vision_mode"] not in _NV_MODES:
        raise CommandError(INVALID)
    s.night_vision_mode = d["night_vision_mode"]
    return {}


def get_led(s: DeviceShadow, p: Any) -> dict:
    _require_selector(p, "led", "config", ("name",))
    return {"led": {"config": {"enabled": _onoff_str(s.led)}}}


def set_led(s: DeviceShadow, p: Any) -> dict:
    d = _section(p, "led", "config")
    if "enabled" not in d:
        raise CommandError(MISSING)
    s.led = _onoff(d["enabled"])
    return {}


def manual_alarm(s: DeviceShadow, p: Any) -> dict:
    d = _section(p, "msg_alarm", "manual_alarm")
    if "action" not in d:
        raise CommandError(MISSING)
    if d["action"] not in ("start", "stop"):
        raise CommandError(INVALID)
    s.alarm_active = d["action"] == "start"
    return {}


def _legacy_sensitivity(v: int) -> str:
    return "low" if v < 40 else ("normal" if v < 70 else "high")


def get_motion(s: DeviceShadow, p: Any) -> dict:
    mod = _module(p, "motion_detection")
    names = mod.get("name")
    if not (names == "motion_det" or (isinstance(names, list) and "motion_det" in names)):
        raise CommandError(MISSING)
    out: dict = {"motion_det": {"enabled": _onoff_str(s.motion_enabled),
                                "digital_sensitivity": str(s.motion_sensitivity),
                                "sensitivity": _legacy_sensitivity(s.motion_sensitivity),
                                "enhanced": "off", "non_vehicle_enabled": "off"}}
    tables = mod.get("table")
    if tables == "region_info" or (isinstance(tables, list) and "region_info" in tables):
        out["region_info"] = []
    return {"motion_detection": out}


def set_motion(s: DeviceShadow, p: Any) -> dict:
    d = _section(p, "motion_detection", "motion_det")
    if "enabled" not in d and "digital_sensitivity" not in d:
        raise CommandError(MISSING)
    enabled = _onoff(d["enabled"]) if "enabled" in d else None
    sens = None
    if "digital_sensitivity" in d:
        sens = _strict_str_int(d["digital_sensitivity"])
        if not 0 <= sens <= 100:
            raise CommandError(INVALID)
    if enabled is not None:
        s.motion_enabled = enabled
    if sens is not None:
        s.motion_sensitivity = sens
    return {}


def get_sd_status(s: DeviceShadow, p: Any) -> dict:
    _require_selector(p, "harddisk_manage", "hd_info", ("table",))
    return {"harddisk_manage": {"hd_info": [{"hd_info_1": {
        "disk_name": "1", "status": s.sd_status, "detect_status": "normal", "type": "local",
        "total_space": "29.7GB", "free_space": "10.1GB", "video_total_space": "28.0GB",
        "video_free_space": "9.0GB", "percent": "66", "write_protect": "0", "record_duration": "1543",
        "record_free_duration": "500", "record_start_time": str(int(time.time()) - 86400 * 7),
        "loop_record_status": "1"}}]}}


def _parse_ymd(v: Any) -> date:
    if not isinstance(v, str) or len(v) != 8 or not v.isdigit():
        raise CommandError(INVALID)
    try:
        return datetime.strptime(v, "%Y%m%d").date()
    except ValueError:
        raise CommandError(INVALID) from None


def search_days(s: DeviceShadow, p: Any) -> dict:
    d = _section(p, "playback", "search_year_utility")
    if "start_date" not in d or "end_date" not in d:
        raise CommandError(MISSING)
    start, end = _parse_ymd(d["start_date"]), _parse_ymd(d["end_date"])
    days = sorted({c.start.astimezone(timezone.utc).date() for c in s.recordings
                   if start <= c.start.astimezone(timezone.utc).date() <= end})
    return {"playback": {"search_results": [{f"search_results_{i + 1}": {"date": dd.strftime("%Y%m%d")}}
                                            for i, dd in enumerate(days)]}}


def _clip_item(i: int, c) -> dict:
    ev = str(c.event_type)
    return {f"search_video_results_{i + 1}": {"startTime": int(c.start.timestamp()), "endTime": int(c.end.timestamp()),
                                              "vedio_type": ev, "video_type": ev}}


def _page(d: dict, clips: list) -> dict:
    si = _int_str(d.get("start_index", 0))
    ei = _int_str(d.get("end_index", 99))
    if si < 0 or ei < si:
        raise CommandError(INVALID)
    page = clips[si:ei + 1]
    return {"playback": {"search_video_results": [_clip_item(si + i, c) for i, c in enumerate(page)],
                         "to_be_continued": 1 if len(clips) > ei + 1 else 0,
                         "filter_enable": True, "filter_chn_enable": False, "playback_delete_v2_enable": False}}


def search_day_clips(s: DeviceShadow, p: Any) -> dict:
    d = _section(p, "playback", "search_video_utility")
    if "date" not in d:
        raise CommandError(MISSING)
    day = _parse_ymd(d["date"])
    clips = sorted((c for c in s.recordings if c.start.astimezone(timezone.utc).date() == day), key=lambda c: c.start)
    return _page(d, clips)


def search_day_clips_utc(s: DeviceShadow, p: Any) -> dict:
    d = _section(p, "playback", "search_video_with_utc")
    if "start_time" not in d or "end_time" not in d:
        raise CommandError(MISSING)
    st, et = d["start_time"], d["end_time"]
    if not isinstance(st, int) or not isinstance(et, int) or isinstance(st, bool) or isinstance(et, bool) or et < st:
        raise CommandError(INVALID)
    clips = sorted((c for c in s.recordings if st <= int(c.start.timestamp()) <= et), key=lambda c: c.start)
    return _page(d, clips)


def search_detection_list(s: DeviceShadow, p: Any) -> dict:
    d = _section(p, "playback", "search_detection_list")
    st, et = _int_str(d.get("start_time", 0)), _int_str(d.get("end_time", 2 ** 31))
    items = [{"start_time": int(c.start.timestamp()), "end_time": int(c.end.timestamp()), "alarm_type": c.event_type,
              "file_id": f"{int(c.start.timestamp())}"}
             for c in sorted(s.recordings, key=lambda c: c.start)
             if st <= int(c.start.timestamp()) <= et and c.event_type != 1]
    return {"playback": {"search_detection_list": items, "snapshot_enable": True, "to_be_continued": 0}}


def last_alarm_info(s: DeviceShadow, p: Any) -> dict:
    _require_selector(p, "system", "last_alarm_info", ("name",))
    return {"system": {"last_alarm_info": {"last_alarm_time": str(s.last_alarm_time),
                                           "last_alarm_type": s.last_alarm_type}}}


def reboot(s: DeviceShadow, p: Any) -> dict:
    mod = _module(p, "system")
    if "reboot" not in mod or mod["reboot"] not in ({}, "null"):
        raise CommandError(MISSING)
    s.reboot_count += 1
    return {}


HANDLERS: dict[str, Callable[[DeviceShadow, Any], dict]] = {
    "getDeviceInfo": get_device_info,
    "getAppComponentList": get_component_list,
    "motorMove": motor_move,
    "singalMove": motor_step,
    "stopMove": motor_stop,
    "getPresetConfig": get_presets,
    "motorMoveToPreset": goto_preset,
    "addMotorPostion": add_preset,
    "deletePreset": delete_preset,
    "getLensMaskConfig": get_privacy,
    "setLensMaskConfig": set_privacy,
    "getDayNightModeConfig": get_day_night,
    "setDayNightModeConfig": set_day_night,
    "getNightVisionModeConfig": get_night_vision,
    "setNightVisionModeConfig": set_night_vision,
    "getLedStatus": get_led,
    "setLedStatus": set_led,
    "doManualAlarm": manual_alarm,
    "getDetectionConfig": get_motion,
    "setDetectionConfig": set_motion,
    "getSdCardStatus": get_sd_status,
    "searchDateWithVideo": search_days,
    "searchVideoOfDay": search_day_clips,
    "searchVideoWithUTC": search_day_clips_utc,
    "searchDetectionList": search_detection_list,
    "getLastAlarmInfo": last_alarm_info,
    "rebootDevice": reboot,
}


def dispatch(shadow: DeviceShadow, method: Any, params: Any) -> tuple[int, dict]:
    """Returns (error_code, result)."""
    handler = HANDLERS.get(method) if isinstance(method, str) else None
    if handler is None:
        return UNSUPPORTED, {}
    try:
        with shadow.lock:
            return 0, handler(shadow, params if params is not None else {})
    except CommandError as e:
        return e.code, {}
