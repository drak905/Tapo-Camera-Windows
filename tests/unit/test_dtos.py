"""Tier 1: request builders match the spec templates exactly; parsers are strict but tolerant."""
from __future__ import annotations

import datetime as dt
import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tapo_desktop.core.control_client import dtos
from tapo_desktop.core.errors import TapoProtocolError
from tapo_desktop.models import DayNightMode, DeviceInfo, Preset, RecordingClip
from tests.unit._core_helpers import spec_value

pytestmark = pytest.mark.unit
UTC = dt.timezone.utc


def _req(item_id: str, params_key: str = "params") -> dict:
    v = spec_value(item_id)
    return {"method": v["method"], "params": v[params_key]}


def _canon(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, sort_keys=True)


def assert_same(actual, expected):
    """Exact structural equality, including value types (\"1\" != 1, [x] != x)."""
    assert _canon(actual) == _canon(expected), f"\nactual:   {_canon(actual)}\nexpected: {_canon(expected)}"


# --------------------------------------------------------------------------- envelope
def test_multiple_request_matches_spec():
    tmpl = spec_value("envelope.multiple_request")["template"]
    built = dtos.multiple_request([dtos.get_device_info_req(), dtos.get_component_list_req()])
    assert_same(built, tmpl)


def test_multiple_request_preserves_order_and_empty():
    a, b = dtos.get_led_req(), dtos.get_privacy_req()
    assert dtos.multiple_request([b, a])["params"]["requests"] == [b, a]
    assert_same(dtos.multiple_request([]), {"method": "multipleRequest", "params": {"requests": []}})


# --------------------------------------------------------------------------- request templates
SIMPLE_REQUESTS = [
    ("control.device_info.get", lambda: dtos.get_device_info_req()),
    ("capability.component_list", lambda: dtos.get_component_list_req()),
    ("control.ptz.move", lambda: dtos.motor_move_req(10, 0)),
    ("control.ptz.move_step", lambda: dtos.motor_step_req(90)),
    ("control.ptz.stop", lambda: dtos.motor_stop_req()),
    ("control.preset.list", lambda: dtos.get_presets_req()),
    ("control.preset.goto", lambda: dtos.goto_preset_req("1")),
    ("control.preset.add", lambda: dtos.add_preset_req("Door")),
    ("control.preset.delete", lambda: dtos.delete_preset_req("1")),
    ("control.privacy.get", lambda: dtos.get_privacy_req()),
    ("control.privacy.set", lambda: dtos.set_privacy_req(True)),
    ("control.day_night.get", lambda: dtos.get_day_night_req()),
    ("control.day_night.set", lambda: dtos.set_day_night_req(DayNightMode.AUTO)),
    ("control.led.get", lambda: dtos.get_led_req()),
    ("control.led.set", lambda: dtos.set_led_req(False)),
    ("control.alarm.manual", lambda: dtos.manual_alarm_req(True)),
    ("control.motion_detection.get", lambda: dtos.get_motion_detection_req()),
    ("control.motion_detection.set", lambda: dtos.set_motion_detection_req(True, 60)),
    ("control.sd.get_status", lambda: dtos.get_sd_status_req()),
    ("vod.search.days", lambda: dtos.search_days_req(dt.date(2025, 1, 1), dt.date(2025, 1, 31))),
    ("control.system.reboot", lambda: dtos.reboot_req()),
]


@pytest.mark.parametrize("item_id,build", SIMPLE_REQUESTS, ids=[i for i, _ in SIMPLE_REQUESTS])
def test_request_matches_spec_template(item_id, build):
    assert_same(build(), _req(item_id))


def test_request_is_json_serializable_and_fresh():
    r1 = dtos.get_device_info_req()
    json.dumps(r1)
    r1["params"]["device_info"]["name"].append("mutated")
    assert_same(dtos.get_device_info_req(), _req("control.device_info.get"))


def test_search_day_clips_matches_spec_legacy_utc_shape():
    v = spec_value("vod.search.day_clips_utc")
    p = v["params_v_legacy"]["playback"]["search_video_with_utc"]
    built = dtos.search_day_clips_req(p["start_time"], p["end_time"])
    assert_same(built, {"method": v["method"], "params": v["params_v_legacy"]})


def test_search_day_clips_paging_indexes():
    built = dtos.search_day_clips_req(1735862400, 1735948799, start_index=100, end_index=199)
    inner = built["params"]["playback"]["search_video_with_utc"]
    assert inner["start_index"] == 100 and inner["end_index"] == 199
    assert inner["start_time"] == 1735862400 and inner["end_time"] == 1735948799
    assert all(isinstance(inner[k], int) for k in ("start_time", "end_time", "start_index", "end_index", "channel"))


@pytest.mark.parametrize("x,y", [(-5, 7), (0, -120), (360, 0)])
def test_motor_move_coords_are_signed_int_strings(x, y):
    built = dtos.motor_move_req(x, y)
    assert_same(built, {"method": "motorMove", "params": {"motor": {"move": {"x_coord": str(x), "y_coord": str(y)}}}})


@pytest.mark.parametrize("direction", [0, 90, 180, 270])
def test_motor_step_all_directions(direction):
    built = dtos.motor_step_req(direction)
    assert_same(built, {"method": "singalMove", "params": {"motor": {"movestep": {"direction": str(direction)}}}})


@pytest.mark.parametrize("direction", [45, -90, 360, 1])
def test_motor_step_invalid_direction(direction):
    with pytest.raises(ValueError):
        dtos.motor_step_req(direction)


def test_set_privacy_off():
    assert_same(dtos.set_privacy_req(False),
                {"method": "setLensMaskConfig", "params": {"lens_mask": {"lens_mask_info": {"enabled": "off"}}}})


@pytest.mark.parametrize("mode,value", [(DayNightMode.AUTO, "auto"), (DayNightMode.ON, "on"), (DayNightMode.OFF, "off")])
def test_set_day_night_values(mode, value):
    assert_same(dtos.set_day_night_req(mode),
                {"method": "setDayNightModeConfig", "params": {"image": {"common": {"inf_type": value}}}})


def test_set_led_on():
    assert_same(dtos.set_led_req(True), {"method": "setLedStatus", "params": {"led": {"config": {"enabled": "on"}}}})


def test_manual_alarm_stop():
    assert_same(dtos.manual_alarm_req(False),
                {"method": "doManualAlarm", "params": {"msg_alarm": {"manual_alarm": {"action": "stop"}}}})


@pytest.mark.parametrize("level", [20, 60, 80])
def test_set_motion_detection_levels(level):
    assert_same(dtos.set_motion_detection_req(True, level),
                {"method": "setDetectionConfig",
                 "params": {"motion_detection": {"motion_det": {"enabled": "on", "digital_sensitivity": str(level)}}}})


def test_set_motion_detection_without_sensitivity():
    assert_same(dtos.set_motion_detection_req(False),
                {"method": "setDetectionConfig", "params": {"motion_detection": {"motion_det": {"enabled": "off"}}}})


@pytest.mark.parametrize("level", [0, 50, 100, 21])
def test_set_motion_detection_invalid_sensitivity(level):
    with pytest.raises(ValueError):
        dtos.set_motion_detection_req(True, level)


def test_search_days_date_format():
    built = dtos.search_days_req(dt.date(2024, 2, 9), dt.date(2024, 3, 1))
    assert_same(built, {"method": "searchDateWithVideo", "params": {"playback": {"search_year_utility": {
        "channel": [0], "start_date": "20240209", "end_date": "20240301"}}}})


def test_goto_and_delete_preset_ids_are_strings():
    assert_same(dtos.goto_preset_req("7"),
                {"method": "motorMoveToPreset", "params": {"preset": {"goto_preset": {"id": "7"}}}})
    assert_same(dtos.delete_preset_req("7"),
                {"method": "deletePreset", "params": {"preset": {"remove_preset": {"id": ["7"]}}}})


# --------------------------------------------------------------------------- parsers on spec templates
BASIC_INFO = {"device_info": {"basic_info": {
    "device_model": "C210", "device_alias": "Front door", "sw_version": "1.3.9 Build 231019 Rel.61069n",
    "hw_version": "2.0", "mac": "AA-BB-CC-DD-EE-FF", "dev_id": "8021C1F2B5A9", "device_type": "SMART.IPCAMERA",
    "extra_unknown": {"x": 1}}}}


def test_parse_device_info():
    info = dtos.parse_device_info(BASIC_INFO)
    assert isinstance(info, DeviceInfo)
    assert info.model == "C210"
    assert info.alias == "Front door"
    assert info.firmware == "1.3.9 Build 231019 Rel.61069n"
    assert info.hardware == "2.0"
    assert info.mac == "AA-BB-CC-DD-EE-FF"
    assert info.device_id == "8021C1F2B5A9"


def test_parse_device_info_minimal_fields():
    info = dtos.parse_device_info({"device_info": {"basic_info": {"device_model": "C100"}}})
    assert info.model == "C100"
    assert info.hardware == "" and info.mac == "" and info.device_id == ""


def test_parse_device_info_spec_multiple_response_example():
    tmpl = spec_value("envelope.multiple_response")["template"]
    item = tmpl["result"]["responses"][0]
    assert item["method"] == "getDeviceInfo"
    assert dtos.parse_device_info(item["result"]).model == "C200"


@pytest.mark.parametrize("bad", [{}, {"device_info": {}}, {"device_info": {"basic_info": "x"}}, {"device_info": None}])
def test_parse_device_info_unusable(bad):
    with pytest.raises(TapoProtocolError):
        dtos.parse_device_info(bad)


def test_parse_component_list_spec_template():
    tmpl = spec_value("capability.component_list")["response_template"]
    assert dtos.parse_component_list(tmpl) == {"ptz": 1, "lensMask": 2}


def test_parse_component_list_string_versions():
    result = {"app_component": {"app_component_list": [{"name": "ptz", "version": "3"}, {"name": "led", "version": 1}]}}
    assert dtos.parse_component_list(result) == {"ptz": 3, "led": 1}


def test_parse_presets_spec_template():
    tmpl = spec_value("control.preset.list")["response_template"]
    assert dtos.parse_presets(tmpl) == [
        Preset(id="1", name="Door", pan=0.1, tilt=0.0, read_only=False),
        Preset(id="2", name="Gate", pan=-0.4, tilt=0.2, read_only=False),
    ]


def test_parse_presets_read_only_and_missing_positions():
    result = {"preset": {"preset": {"id": ["5"], "name": ["Home"], "read_only": ["1"]}}}
    assert dtos.parse_presets(result) == [Preset(id="5", name="Home", pan=0.0, tilt=0.0, read_only=True)]


def test_parse_presets_empty():
    assert dtos.parse_presets({"preset": {"preset": {"id": [], "name": []}}}) == []


def test_parse_presets_mismatched_lengths():
    with pytest.raises(TapoProtocolError):
        dtos.parse_presets({"preset": {"preset": {"id": ["1", "2"], "name": ["only one"]}}})


@pytest.mark.parametrize("value,expected", [("on", True), ("off", False)])
def test_parse_privacy(value, expected):
    assert dtos.parse_privacy({"lens_mask": {"lens_mask_info": {"enabled": value}}}) is expected


@pytest.mark.parametrize("bad", [{}, {"lens_mask": {}}, {"lens_mask": {"lens_mask_info": {"enabled": "maybe"}}}])
def test_parse_privacy_unusable(bad):
    with pytest.raises(TapoProtocolError):
        dtos.parse_privacy(bad)


@pytest.mark.parametrize("value,mode", [("auto", DayNightMode.AUTO), ("on", DayNightMode.ON), ("off", DayNightMode.OFF)])
def test_parse_day_night(value, mode):
    result = {"image": {"common": {"inf_type": value, "light_freq_mode": "auto"}}}
    assert dtos.parse_day_night(result) is mode


def test_parse_day_night_unknown_value():
    with pytest.raises(TapoProtocolError):
        dtos.parse_day_night({"image": {"common": {"inf_type": "sometimes"}}})


@pytest.mark.parametrize("value,expected", [("on", True), ("off", False)])
def test_parse_led(value, expected):
    assert dtos.parse_led({"led": {"config": {"enabled": value}}}) is expected


def test_parse_motion_detection():
    result = {"motion_detection": {"motion_det": {"enabled": "on", "digital_sensitivity": "60",
                                                  "enhanced": "off"}, "region_info": []}}
    assert dtos.parse_motion_detection(result) == (True, 60)


def test_parse_motion_detection_without_sensitivity():
    assert dtos.parse_motion_detection({"motion_detection": {"motion_det": {"enabled": "off"}}}) == (False, None)


def test_parse_motion_detection_unusable():
    with pytest.raises(TapoProtocolError):
        dtos.parse_motion_detection({"motion_detection": {}})


def test_parse_sd_status_spec_template():
    tmpl = spec_value("control.sd.get_status")["response_template"]
    disks = dtos.parse_sd_status(tmpl)
    assert isinstance(disks, list) and len(disks) == 1
    assert disks[0]["status"] == "normal"
    assert disks[0]["total_space"] == "29.7GB"
    assert disks[0]["percent"] == 34


def test_parse_sd_status_no_card():
    assert dtos.parse_sd_status({"harddisk_manage": {"hd_info": []}}) == []


def test_parse_days_spec_template():
    result = spec_value("vod.search.days.response")["result"]
    assert dtos.parse_days(result) == [dt.date(2025, 1, 3)]


def test_parse_days_multiple_sorted_unique():
    result = {"playback": {"search_results": [
        {"search_results_1": {"date": "20250105"}},
        {"search_results_2": {"date": "20250103"}},
        {"search_results_3": {"date": "20250105"}}]}}
    assert dtos.parse_days(result) == [dt.date(2025, 1, 3), dt.date(2025, 1, 5)]


def test_parse_days_empty():
    assert dtos.parse_days({"playback": {"search_results": []}}) == []


def test_parse_days_bad_date():
    with pytest.raises(TapoProtocolError):
        dtos.parse_days({"playback": {"search_results": [{"search_results_1": {"date": "2025-13-45"}}]}})


def test_parse_day_clips_spec_template():
    result = spec_value("vod.search.day_clips.response")["result"]
    clips = dtos.parse_day_clips(result)
    assert clips == [RecordingClip(start=dt.datetime.fromtimestamp(1735900000, UTC),
                                   end=dt.datetime.fromtimestamp(1735900120, UTC), event_type=2)]
    assert clips[0].start.tzinfo is not None
    assert clips[0].duration_seconds == 120


def test_parse_day_clips_string_times_and_video_type_only():
    result = {"playback": {"search_video_results": [
        {"search_video_results_1": {"startTime": "1735900000", "endTime": "1735900060", "video_type": "6"}}]}}
    assert dtos.parse_day_clips(result) == [RecordingClip(start=dt.datetime.fromtimestamp(1735900000, UTC),
                                                          end=dt.datetime.fromtimestamp(1735900060, UTC),
                                                          event_type=6)]


def test_parse_day_clips_empty():
    assert dtos.parse_day_clips({"playback": {"search_video_results": [], "to_be_continued": 0}}) == []


def test_parse_day_clips_bad_times():
    with pytest.raises(TapoProtocolError):
        dtos.parse_day_clips({"playback": {"search_video_results": [
            {"search_video_results_1": {"startTime": "yesterday", "endTime": 1}}]}})


# --------------------------------------------------------------------------- fuzz: only TapoProtocolError
json_leaf = st.one_of(st.none(), st.booleans(), st.integers(-2**40, 2**40), st.floats(allow_nan=False),
                      st.text(max_size=12))
json_val = st.recursive(json_leaf, lambda ch: st.one_of(st.lists(ch, max_size=4),
                                                        st.dictionaries(st.text(max_size=8), ch, max_size=4)),
                        max_leaves=20)
KEYS = st.sampled_from(["device_info", "basic_info", "device_model", "app_component", "app_component_list",
                        "name", "version", "preset", "id", "position_pan", "position_tilt", "read_only",
                        "lens_mask", "lens_mask_info", "enabled", "image", "common", "inf_type", "led",
                        "config", "motion_detection", "motion_det", "digital_sensitivity",
                        "harddisk_manage", "hd_info", "hd_info_1", "playback", "search_results",
                        "search_results_1", "date", "search_video_results", "search_video_results_1",
                        "startTime", "endTime", "vedio_type", "video_type", "to_be_continued"])
spec_shaped = st.recursive(json_leaf, lambda ch: st.one_of(st.lists(ch, max_size=3),
                                                           st.dictionaries(KEYS, ch, max_size=4)),
                           max_leaves=25)
PARSERS = [dtos.parse_device_info, dtos.parse_component_list, dtos.parse_presets, dtos.parse_privacy,
           dtos.parse_day_night, dtos.parse_led, dtos.parse_motion_detection, dtos.parse_sd_status,
           dtos.parse_days, dtos.parse_day_clips]


def _only_protocol_errors(parser, data):
    try:
        parser(data)
    except TapoProtocolError:
        pass


@pytest.mark.parametrize("parser", PARSERS, ids=lambda p: p.__name__)
@given(data=st.dictionaries(st.text(max_size=8), json_val, max_size=4))
@settings(max_examples=60, deadline=None)
def test_parsers_fuzz_random(parser, data):
    _only_protocol_errors(parser, data)


@pytest.mark.parametrize("parser", PARSERS, ids=lambda p: p.__name__)
@given(data=st.dictionaries(KEYS, spec_shaped, max_size=4))
@settings(max_examples=120, deadline=None)
def test_parsers_fuzz_spec_shaped(parser, data):
    _only_protocol_errors(parser, data)


@pytest.mark.parametrize("parser", PARSERS, ids=lambda p: p.__name__)
@pytest.mark.parametrize("data", [None, [], "str", 5])
def test_parsers_non_dict_input(parser, data):
    with pytest.raises(TapoProtocolError):
        parser(data)
