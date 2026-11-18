"""Camera name rules [spec:rename.validation], the setDeviceAlias payload [spec:rename.request], the
locale-aware natural list order and the pending / local name resolution used by cloud refreshes."""
from __future__ import annotations

import dataclasses
import time

import pytest
from PyQt6.QtCore import QLocale

from tapo_desktop.models import CameraConfig
from tapo_desktop.services import camera_names as cn

pytestmark = pytest.mark.unit


# ------------------------------------------------------------------ request payload
def test_set_device_alias_request_is_exactly_the_apps():
    from tapo_desktop.core.control_client import dtos

    assert dtos.set_device_alias_req("Cổng trước") == {
        "method": "setDeviceAlias", "params": {"system": {"sys": {"dev_alias": "Cổng trước"}}}}


def test_control_api_rename_sends_one_set_device_alias():
    from tapo_desktop.core.control_client.client import ControlApi

    sent = []

    class Api(ControlApi):
        def request(self, req):
            sent.append(req)
            return {}

    Api().rename("Garage")
    assert sent == [{"method": "setDeviceAlias", "params": {"system": {"sys": {"dev_alias": "Garage"}}}}]


# ------------------------------------------------------------------ validation
def test_valid_names_are_trimmed():
    assert cn.validate_camera_name("  Front door  ") == "Front door"
    assert cn.validate_camera_name("a" * 32) == "a" * 32
    assert cn.name_error("Garage") == ""


@pytest.mark.parametrize("text", ["", "   ", "\t"])
def test_empty_names_are_refused(text):
    with pytest.raises(cn.InvalidCameraName) as err:
        cn.validate_camera_name(text)
    assert str(err.value)
    assert cn.name_error(text) == str(err.value)


def test_the_limit_is_32_utf8_bytes_not_characters():
    assert cn.MAX_NAME_BYTES == 32
    viet = "Cổng" * 4  # "ổ" is 3 bytes in UTF-8: 4 * 6 = 24 bytes
    assert cn.validate_camera_name(viet) == viet
    too_long = "ổ" * 11  # 11 characters, 33 bytes
    with pytest.raises(cn.InvalidCameraName) as err:
        cn.validate_camera_name(too_long)
    assert "32" in str(err.value)
    with pytest.raises(cn.InvalidCameraName):
        cn.validate_camera_name("a" * 33)


def test_control_characters_are_refused():
    with pytest.raises(cn.InvalidCameraName):
        cn.validate_camera_name("Front\ndoor")


def test_decomposed_vietnamese_is_composed_before_counting():
    decomposed = "Co\u0302\u0309ng"  # "Cổng" typed as base letter + combining marks
    assert cn.validate_camera_name(decomposed) == "Cổng"


def test_invalid_camera_name_is_a_value_error():
    assert issubclass(cn.InvalidCameraName, ValueError)


# ------------------------------------------------------------------ rename modes
def test_only_owned_cloud_cameras_rename_on_the_camera():
    assert cn.renames_on_camera(CameraConfig(name="a", host="", source="cloud", role=0)) is True
    assert cn.renames_on_camera(CameraConfig(name="a", host="", source="cloud", role=1)) is False
    assert cn.renames_on_camera(CameraConfig(name="a", host="192.0.2.1", source="manual")) is False


# ------------------------------------------------------------------ sorting
def _cams(*names: str) -> list[CameraConfig]:
    return [CameraConfig(name=n, host="", id=f"id{i}") for i, n in enumerate(names)]


def _names(cams) -> list[str]:
    return [c.name for c in cams]


NAMES = ("cam 010", "Zebra", "Cam 002", "Đường", "Cửa sau", "Cổng", "Dan", "apple", "Cong", "Camera 10", "Camera 9")


@pytest.mark.parametrize("locale", [None, QLocale("vi_VN"), QLocale("en_US")], ids=["system", "vi", "en"])
def test_a_to_z_is_natural_case_insensitive_and_diacritic_aware(locale):
    out = _names(cn.sort_cameras(_cams(*NAMES), cn.SORT_NAME_ASC, locale))
    assert sorted(out) == sorted(NAMES)
    assert out[0] == "apple" and out[-1] == "Zebra"  # case-insensitive
    assert out.index("Cam 002") < out.index("cam 010") < out.index("Camera 9") < out.index("Camera 10")  # numbers
    assert out.index("Cong") < out.index("Dan") and out.index("Cổng") < out.index("Dan")  # "Cổng" next to "Cong"
    assert out.index("Camera 10") < out.index("Cổng") < out.index("Cửa sau")
    assert out.index("Dan") < out.index("Đường") < out.index("Zebra")


def test_z_to_a_is_the_reverse_and_account_order_keeps_the_input():
    cams = _cams(*NAMES)
    asc = _names(cn.sort_cameras(cams, cn.SORT_NAME_ASC, QLocale("vi_VN")))
    assert _names(cn.sort_cameras(cams, cn.SORT_NAME_DESC, QLocale("vi_VN"))) == asc[::-1]
    assert _names(cn.sort_cameras(cams, cn.SORT_ACCOUNT)) == list(NAMES)


def test_equal_names_keep_a_stable_order():
    cams = [CameraConfig(name="Cam", host="", id="b"), CameraConfig(name="cam", host="", id="a")]
    assert [c.id for c in cn.sort_cameras(cams, cn.SORT_NAME_ASC)] == ["b", "a"]


def test_sort_mode_falls_back_to_a_to_z():
    assert cn.sort_mode(None) == cn.SORT_NAME_ASC
    assert cn.sort_mode("bogus") == cn.SORT_NAME_ASC
    assert cn.sort_mode(cn.SORT_ACCOUNT) == cn.SORT_ACCOUNT
    assert cn.SETTING_CAMERA_SORT == "camera_sort"


# ------------------------------------------------------------------ pending / local names
BASE = CameraConfig(name="Old", host="", id="d1", source="cloud", cloud_alias="Old")


def test_a_pending_rename_wins_until_the_cloud_alias_matches():
    now = time.time()
    pending = dataclasses.replace(BASE, name="New", pending_name="New", pending_since=now - 60)
    assert cn.resolve_cloud_name(pending, "Old", now) == ("New", "New", now - 60)
    assert cn.resolve_cloud_name(pending, "New", now) == ("New", "", 0.0)


def test_a_pending_rename_expires_after_24_hours():
    now = time.time()
    stale = dataclasses.replace(BASE, name="New", pending_name="New", pending_since=now - cn.PENDING_RENAME_TTL_S - 1)
    assert cn.resolve_cloud_name(stale, "Old", now) == ("Old", "", 0.0)


def test_a_local_display_name_always_wins():
    now = time.time()
    local = dataclasses.replace(BASE, name="Mine", display_name="Mine")
    assert cn.resolve_cloud_name(local, "Owner renamed it", now)[0] == "Mine"


def test_without_overrides_the_cloud_alias_is_used():
    assert cn.resolve_cloud_name(BASE, "Renamed in the Tapo app", time.time()) == ("Renamed in the Tapo app", "", 0.0)
