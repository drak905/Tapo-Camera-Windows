"""Tier 1: TapoControlClient session logic against an in-process strict fake camera transport."""
from __future__ import annotations

import datetime as dt
import logging
import re
from concurrent.futures import ThreadPoolExecutor

import pytest

from tapo_desktop.core.control_client import dtos
from tapo_desktop.core.control_client.client import TapoControlClient
from tapo_desktop.core.errors import (
    TapoAuthError, TapoDeviceError, TapoError, TapoLockedError,
)
from tapo_desktop.models import DayNightMode, DeviceInfo
from tests.unit._core_helpers import FakeCameraTransport, md5u, sha256u

pytestmark = pytest.mark.unit
UTC = dt.timezone.utc
PW = "Correct-Horse-Battery-Staple-9"


def make(secure=True, password=PW, cam_password=PW, **kw):
    fake = FakeCameraTransport(cam_password, secure=secure, **kw)
    client = TapoControlClient("192.0.2.10", password, transport=fake)
    return client, fake


# --------------------------------------------------------------------------- secure login
def test_secure_login_step1_body_exact():
    client, fake = make()
    client.login()
    first = fake.login_bodies()[0]
    assert first["method"] == "login"
    assert set(first["params"]) == {"cnonce", "encrypt_type", "username"}
    assert first["params"]["encrypt_type"] == "3"
    assert first["params"]["username"] == "admin"
    assert re.fullmatch(r"[0-9A-F]{16}", first["params"]["cnonce"])


def test_secure_login_step2_body_exact():
    client, fake = make()
    client.login()
    bodies = fake.login_bodies()
    assert len(bodies) == 2
    step1, step2 = bodies
    cn = step1["params"]["cnonce"]
    assert step2 == {"method": "login", "params": {
        "cnonce": cn, "encrypt_type": "3", "username": "admin",
        "digest_passwd": sha256u(sha256u(PW) + cn + fake.nonce) + cn + fake.nonce}}
    assert client.is_secure is True
    assert client.logged_in is True
    assert fake.login_count == 1


def test_login_posts_to_root_path():
    client, fake = make()
    client.login()
    assert [p for p, _b, _h in fake.posts] == ["/", "/"]


def test_secure_wrong_password_raises_auth_error_without_sending_digest():
    client, fake = make(password="wrong-password")
    with pytest.raises(TapoAuthError) as ei:
        client.login()
    assert not isinstance(ei.value, TapoLockedError)
    assert all("digest_passwd" not in b["params"] for b in fake.login_bodies()), \
        "client must not send digest_passwd when device_confirm does not match"
    assert client.logged_in is False


def test_secure_tampered_device_confirm_raises_auth_error():
    client, fake = make()
    real = fake._login

    def tampered(req):
        resp = real(req)
        data = (resp.get("result") or {}).get("data") or {}
        if "device_confirm" in data:
            data["device_confirm"] = "0" * 64 + data["device_confirm"][64:]
        return resp

    fake._login = tampered
    with pytest.raises(TapoAuthError):
        client.login()


@pytest.mark.parametrize("code", [-40404])
def test_locked_login_raises_locked_error(code):
    client, fake = make()
    fake.lock_code = code
    with pytest.raises(TapoLockedError):
        client.login()


def test_auto_login_on_first_request():
    client, fake = make()
    assert client.logged_in is False
    info = client.get_device_info()
    assert isinstance(info, DeviceInfo) and info.model == "C210"
    assert fake.login_count == 1


# --------------------------------------------------------------------------- secure data path
def test_secure_requests_use_seq_from_start_seq_and_increment():
    client, fake = make(start_seq=500)
    for _ in range(5):
        client.get_led()
    seqs = [int(h["seq"]) for h in fake.data_headers()]
    assert seqs == [500, 501, 502, 503, 504]
    assert fake.seq_errors == 0 and fake.tag_errors == 0


def test_secure_requests_send_valid_tag_and_stok_path():
    client, fake = make()
    client.get_device_info()
    path, body, headers = [p for p in fake.posts if p[0].startswith("/stok=")][0]
    assert path == "/stok=FAKESTOK00011/ds"
    assert re.fullmatch(r"[0-9A-F]{64}", {k.lower(): v for k, v in headers.items()}["tapo_tag"])
    assert body.startswith('{"method":"securePassthrough","params":{"request":"')
    assert fake.tag_errors == 0


def test_request_wraps_in_multiple_request():
    client, fake = make()
    result = client.request(dtos.get_led_req())
    assert result == {"led": {"config": {"enabled": "on"}}}
    assert fake.inner_requests[-1] == dtos.get_led_req()


def test_multiple_request_returns_raw_items():
    client, fake = make()
    fake.item_errors["getLedStatus"] = [-64304]
    items = client.multiple_request([dtos.get_device_info_req(), dtos.get_led_req()])
    assert [i["method"] for i in items] == ["getDeviceInfo", "getLedStatus"]
    assert items[0]["error_code"] == 0
    assert items[1]["error_code"] == -64304


def test_item_error_raises_device_error_with_code_and_method():
    client, fake = make()
    fake.item_errors["getLedStatus"] = [-40106]
    with pytest.raises(TapoDeviceError) as ei:
        client.get_led()
    assert ei.value.code == -40106
    assert ei.value.method == "getLedStatus"
    assert client.get_led() is True  # next call fine


def test_top_level_session_expired_relogin_and_retry():
    client, fake = make()
    client.get_led()
    fake.expire_next = 1
    assert client.get_led() is True
    assert fake.login_count == 2
    assert fake.seq_errors == 0


def test_per_item_session_expired_relogin_and_retry():
    client, fake = make()
    client.get_led()
    fake.item_errors["getLedStatus"] = [-40401]
    assert client.get_led() is True
    assert fake.login_count == 2


def test_http_401_relogin_and_retry():
    client, fake = make()
    client.get_led()
    fake.http_status_next = [401]
    assert client.get_led() is True
    assert fake.login_count == 2


def test_persistent_expiry_gives_up_after_one_retry():
    client, fake = make()
    client.get_led()
    fake.always_expired = True
    with pytest.raises(TapoError):
        client.get_led()
    assert fake.login_count <= 3


def test_thread_safety_no_seq_desync():
    client, fake = make()

    def work(_):
        for _ in range(10):
            assert client.get_led() is True

    with ThreadPoolExecutor(8) as ex:
        list(ex.map(work, range(8)))
    assert fake.seq_errors == 0 and fake.tag_errors == 0
    assert fake.login_count == 1
    assert len(fake.data_headers()) == 80


# --------------------------------------------------------------------------- legacy
def test_legacy_login_body_exact():
    client, fake = make(secure=False)
    client.login()
    legacy = fake.login_bodies()[-1]
    assert legacy == {"method": "login", "params": {"hashed": True, "password": md5u(PW), "username": "admin"}}
    assert client.is_secure is False
    assert client.logged_in is True


def test_legacy_requests_are_plain_json_without_tag():
    client, fake = make(secure=False)
    assert client.get_device_info().model == "C210"
    path, body, headers = [p for p in fake.posts if p[0].startswith("/stok=")][0]
    assert body.startswith('{"method":"multipleRequest"')
    assert "tapo_tag" not in {k.lower() for k in headers}


def test_legacy_wrong_password():
    client, _fake = make(secure=False, password="nope")
    with pytest.raises(TapoAuthError):
        client.login()


# --------------------------------------------------------------------------- recordings
def test_days_with_recordings_request_and_parse():
    client, fake = make()
    fake.days = ["20250103", "20250110"]
    days = client.days_with_recordings(dt.date(2025, 1, 1), dt.date(2025, 1, 31))
    assert days == [dt.date(2025, 1, 3), dt.date(2025, 1, 10)]
    req = [r for r in fake.inner_requests if r["method"] == "searchDateWithVideo"][0]
    assert req == dtos.search_days_req(dt.date(2025, 1, 1), dt.date(2025, 1, 31))


def test_recordings_for_day_window_and_paging():
    client, fake = make()
    day_start = int(dt.datetime(2025, 1, 3, tzinfo=UTC).timestamp())
    fake.clips = [(day_start + i * 60, day_start + i * 60 + 30, "2") for i in range(150)]
    fake.clips.append((day_start - 3600, day_start - 3500, "1"))       # previous day: excluded
    fake.clips.append((day_start + 86400, day_start + 86460, "1"))     # next day: excluded
    clips = client.recordings_for_day(dt.date(2025, 1, 3), tz=UTC)
    assert len(clips) == 150
    assert clips[0].start == dt.datetime.fromtimestamp(day_start, UTC)
    assert clips[-1].end == dt.datetime.fromtimestamp(day_start + 149 * 60 + 30, UTC)
    reqs = [r["params"]["playback"]["search_video_with_utc"] for r in fake.inner_requests
            if r["method"] == "searchVideoWithUTC"]
    assert [(r["start_index"], r["end_index"]) for r in reqs] == [(0, 99), (100, 199)]
    assert all(r["start_time"] == day_start and r["end_time"] == day_start + 86399 for r in reqs)


def test_recordings_for_day_respects_timezone():
    client, fake = make()
    tz = dt.timezone(dt.timedelta(hours=7))
    client.recordings_for_day(dt.date(2025, 1, 3), tz=tz)
    req = [r for r in fake.inner_requests if r["method"] == "searchVideoWithUTC"][0]
    p = req["params"]["playback"]["search_video_with_utc"]
    expected_start = int(dt.datetime(2025, 1, 3, tzinfo=tz).timestamp())
    assert p["start_time"] == expected_start
    assert p["end_time"] == expected_start + 86399


# --------------------------------------------------------------------------- wrappers send the dto requests
@pytest.mark.parametrize("name,call,expected", [
    ("move", lambda c: c.move(10, -5), lambda: dtos.motor_move_req(10, -5)),
    ("step", lambda c: c.step(180), lambda: dtos.motor_step_req(180)),
    ("stop_move", lambda c: c.stop_move(), lambda: dtos.motor_stop_req()),
    ("goto_preset", lambda c: c.goto_preset("3"), lambda: dtos.goto_preset_req("3")),
    ("add_preset", lambda c: c.add_preset("Gate"), lambda: dtos.add_preset_req("Gate")),
    ("delete_preset", lambda c: c.delete_preset("3"), lambda: dtos.delete_preset_req("3")),
    ("set_day_night", lambda c: c.set_day_night(DayNightMode.ON), lambda: dtos.set_day_night_req(DayNightMode.ON)),
    ("set_led", lambda c: c.set_led(False), lambda: dtos.set_led_req(False)),
    ("set_alarm", lambda c: c.set_alarm(True), lambda: dtos.manual_alarm_req(True)),
    ("set_motion", lambda c: c.set_motion_detection(True, 80), lambda: dtos.set_motion_detection_req(True, 80)),
    ("reboot", lambda c: c.reboot(), lambda: dtos.reboot_req()),
], ids=lambda v: v if isinstance(v, str) else None)
def test_wrappers_send_expected_request(name, call, expected):
    client, fake = make()
    call(client)
    assert expected() in fake.inner_requests


def test_set_privacy_sends_lens_mask_request():
    client, fake = make()
    client.set_privacy(True)
    assert dtos.set_privacy_req(True) in fake.inner_requests


def test_close_closes_transport():
    client, fake = make()
    client.get_led()
    client.close()
    assert fake.closed is True


# --------------------------------------------------------------------------- no credential leaks
def test_repr_and_str_do_not_leak_password():
    client, _fake = make()
    for text in (repr(client), str(client)):
        assert PW not in text
        assert sha256u(PW) not in text and md5u(PW) not in text


def test_repr_after_login_does_not_leak_secrets():
    client, fake = make()
    client.login()
    for text in (repr(client), str(client)):
        assert PW not in text
        assert sha256u(PW) not in text and md5u(PW) not in text
        assert fake.stok not in text


def test_auth_error_does_not_leak_password():
    client, _fake = make(password="Leaky-Secret-123")
    with pytest.raises(TapoAuthError) as ei:
        client.login()
    for text in (str(ei.value), repr(ei.value), repr(ei.value.args)):
        assert "Leaky-Secret-123" not in text
        assert sha256u("Leaky-Secret-123") not in text


def test_logs_do_not_leak_secrets(caplog):
    caplog.set_level(logging.DEBUG)
    client, fake = make()
    client.get_device_info()
    fake.expire_next = 1
    client.get_led()
    bad, _ = make(password="Leaky-Secret-123")
    with pytest.raises(TapoAuthError):
        bad.login()
    for secret in (PW, sha256u(PW), md5u(PW), "Leaky-Secret-123", sha256u("Leaky-Secret-123")):
        assert secret not in caplog.text
