"""Tier 2: TapoControlClient against the stateful mock camera (fixtures from lane-fixture)."""
from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor

import pytest

from tapo_desktop.core.control_client.transport import PinStore
from tapo_desktop.core.errors import (
    CertificateMismatchError, TapoAuthError, TapoDeviceError, TapoLockedError,
)
from tapo_desktop.models import DayNightMode, DeviceInfo, Preset
from tests.integration._core_it_helpers import (
    RecordingTransport, cloud_password, logged_requests, make_client, methods,
)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(30)]
UTC = dt.timezone.utc


# --------------------------------------------------------------------------- login
def test_secure_login_and_device_info(mock_camera):
    client = make_client(mock_camera)
    try:
        client.login()
        assert client.is_secure is True
        assert client.logged_in is True
        info = client.get_device_info()
        assert isinstance(info, DeviceInfo)
        assert info.model == getattr(mock_camera, "model", "C210")
        assert mock_camera.login_count == 1
        assert "getDeviceInfo" in methods(mock_camera)
    finally:
        client.close()


def test_auto_login_on_first_request(mock_camera):
    client = make_client(mock_camera)
    try:
        assert client.logged_in is False
        client.get_device_info()
        assert client.logged_in is True and mock_camera.login_count == 1
    finally:
        client.close()


def test_legacy_login_and_device_info(legacy_mock_camera):
    client = make_client(legacy_mock_camera)
    try:
        client.login()
        assert client.is_secure is False
        assert client.get_device_info().model == getattr(legacy_mock_camera, "model", "C210")
        assert legacy_mock_camera.login_count == 1
    finally:
        client.close()


def test_secure_wrong_password(mock_camera):
    client = make_client(mock_camera, password="definitely-wrong")
    try:
        with pytest.raises(TapoAuthError) as ei:
            client.login()
        assert not isinstance(ei.value, TapoLockedError)
        assert "definitely-wrong" not in str(ei.value)
    finally:
        client.close()


def test_legacy_wrong_password(legacy_mock_camera):
    client = make_client(legacy_mock_camera, password="definitely-wrong")
    try:
        with pytest.raises(TapoAuthError):
            client.login()
    finally:
        client.close()


def test_locked_login(mock_camera):
    mock_camera.lock_login(5.0)
    client = make_client(mock_camera)
    try:
        with pytest.raises(TapoLockedError):
            client.login()
    finally:
        client.close()


# --------------------------------------------------------------------------- seq / session
def test_seq_increments_per_request(mock_camera):
    transport = RecordingTransport(mock_camera.host, mock_camera.https_port, pin_store=PinStore(), timeout=5)
    client = make_client(mock_camera, transport=transport)
    try:
        for _ in range(10):
            client.get_led()
        seqs = [int(h["seq"]) for p, h in transport.sent if p.startswith("/stok=")]
        assert len(seqs) == 10
        assert seqs == list(range(seqs[0], seqs[0] + 10))
        assert all("tapo_tag" in h for p, h in transport.sent if p.startswith("/stok="))
        assert mock_camera.login_count == 1
        assert methods(mock_camera).count("getLedStatus") == 10
    finally:
        client.close()


def test_expire_sessions_relogin_transparently(mock_camera):
    client = make_client(mock_camera)
    try:
        client.get_device_info()
        assert mock_camera.login_count == 1
        mock_camera.expire_sessions()
        assert client.get_device_info().model
        assert mock_camera.login_count == 2
    finally:
        client.close()


def test_token_expiry_every_50_over_200_requests(mock_camera):
    mock_camera.inject_token_expiry(50)
    client = make_client(mock_camera)
    try:
        for _ in range(200):
            assert client.get_led() in (True, False)
        assert 4 <= mock_camera.login_count <= 6
    finally:
        mock_camera.inject_token_expiry(0)
        client.close()


def test_http_401_relogin(mock_camera):
    client = make_client(mock_camera)
    try:
        client.get_led()
        mock_camera.inject_http_status(401, times=1)
        client.get_led()
        assert mock_camera.login_count == 2
    finally:
        client.close()


def test_injected_item_error(mock_camera):
    client = make_client(mock_camera)
    try:
        mock_camera.inject_error("getLedStatus", -40106, times=1)
        with pytest.raises(TapoDeviceError) as ei:
            client.get_led()
        assert ei.value.code == -40106
        assert ei.value.method == "getLedStatus"
        assert client.get_led() in (True, False)
    finally:
        client.close()


def test_multiple_request_returns_raw_items_with_errors(mock_camera):
    from tapo_desktop.core.control_client import dtos

    client = make_client(mock_camera)
    try:
        mock_camera.inject_error("getLedStatus", -64304, times=1)
        items = client.multiple_request([dtos.get_device_info_req(), dtos.get_led_req()])
        by_method = {i["method"]: i for i in items}
        assert by_method["getDeviceInfo"]["error_code"] == 0
        assert by_method["getLedStatus"]["error_code"] == -64304
    finally:
        client.close()


def test_unknown_method_raises_device_error(mock_camera):
    client = make_client(mock_camera)
    try:
        with pytest.raises(TapoDeviceError) as ei:
            client.request({"method": "noSuchMethod", "params": {"x": {"name": ["y"]}}})
        assert ei.value.code == -40106
    finally:
        client.close()


# --------------------------------------------------------------------------- toggles
@pytest.fixture
def client(mock_camera):
    c = make_client(mock_camera)
    yield c
    c.close()


@pytest.mark.parametrize("value", [True, False, True])
def test_privacy_roundtrip(mock_camera, client, value):
    client.set_privacy(value)
    assert mock_camera.state.privacy is value
    assert client.get_privacy() is value


@pytest.mark.parametrize("mode", [DayNightMode.ON, DayNightMode.OFF, DayNightMode.AUTO])
def test_day_night_roundtrip(mock_camera, client, mode):
    client.set_day_night(mode)
    assert mock_camera.state.inf_type == mode.value
    assert client.get_day_night() is mode


@pytest.mark.parametrize("value", [False, True])
def test_led_roundtrip(mock_camera, client, value):
    client.set_led(value)
    assert mock_camera.state.led is value
    assert client.get_led() is value


def test_alarm_start_stop(mock_camera, client):
    client.set_alarm(True)
    assert mock_camera.state.alarm_active is True
    client.set_alarm(False)
    assert mock_camera.state.alarm_active is False


@pytest.mark.parametrize("enabled,level", [(True, 80), (True, 20), (False, 60)])
def test_motion_detection_roundtrip(mock_camera, client, enabled, level):
    client.set_motion_detection(enabled, level)
    assert mock_camera.state.motion_enabled is enabled
    assert int(mock_camera.state.motion_sensitivity) == level
    assert client.get_motion_detection() == (enabled, level)


def test_motion_detection_enable_only(mock_camera, client):
    client.set_motion_detection(True, 60)
    client.set_motion_detection(False)
    assert mock_camera.state.motion_enabled is False
    assert client.get_motion_detection()[0] is False


def test_ptz_move_updates_position(mock_camera, client):
    x0, y0 = mock_camera.state.ptz_x, mock_camera.state.ptz_y
    client.move(10, -5)
    assert (mock_camera.state.ptz_x, mock_camera.state.ptz_y) == (x0 + 10, y0 - 5)
    client.move(-10, 5)
    assert (mock_camera.state.ptz_x, mock_camera.state.ptz_y) == (x0, y0)


def test_ptz_step_and_stop(mock_camera, client):
    client.step(90)
    client.stop_move()
    ms = methods(mock_camera)
    assert "singalMove" in ms and "stopMove" in ms


def test_presets_add_goto_delete(mock_camera, client):
    before = {p.id for p in client.get_presets()}
    client.add_preset("Door")
    presets = client.get_presets()
    assert all(isinstance(p, Preset) for p in presets)
    added = [p for p in presets if p.id not in before]
    assert len(added) == 1 and added[0].name == "Door"
    pid = added[0].id
    assert pid in mock_camera.state.presets

    client.move(20, 0)
    client.goto_preset(pid)
    assert logged_requests(mock_camera, "motorMoveToPreset")[-1]["params"] == {"preset": {"goto_preset": {"id": pid}}}

    client.delete_preset(pid)
    assert pid not in {p.id for p in client.get_presets()}
    assert pid not in mock_camera.state.presets


def test_components_and_sd_status(mock_camera, client):
    comps = client.get_components()
    assert comps == {k: int(v) for k, v in mock_camera.state.components.items()}
    disks = client.get_sd_status()
    assert isinstance(disks, list)
    assert all(isinstance(d, dict) for d in disks)


def test_reboot_request_sent(mock_camera, client):
    client.reboot()
    assert "rebootDevice" in methods(mock_camera)


# --------------------------------------------------------------------------- recordings
def _seeded_days(cam) -> list[dt.date]:
    return sorted({c.start.astimezone(UTC).date() for c in cam.state.recordings})


def test_days_with_recordings(mock_camera, client):
    days = _seeded_days(mock_camera)
    assert len(days) == 2, "fixture contract: 3 clips today UTC + 1 yesterday"
    assert client.days_with_recordings(days[0], days[-1]) == days


def test_recordings_for_day(mock_camera, client):
    today = _seeded_days(mock_camera)[-1]
    expected = sorted((c.start, c.end) for c in mock_camera.state.recordings
                      if c.start.astimezone(UTC).date() == today)
    clips = client.recordings_for_day(today, tz=UTC)
    assert sorted((c.start, c.end) for c in clips) == expected
    assert len(clips) == 3
    assert all(c.start.tzinfo is not None for c in clips)


def test_recordings_for_yesterday(mock_camera, client):
    yesterday = _seeded_days(mock_camera)[0]
    assert len(client.recordings_for_day(yesterday, tz=UTC)) == 1


# --------------------------------------------------------------------------- TOFU
def test_tofu_pins_on_first_contact(mock_camera):
    pins = PinStore()
    client = make_client(mock_camera, pin_store=pins)
    try:
        client.login()
        assert pins.get(mock_camera.host, mock_camera.https_port) == mock_camera.cert_fingerprint
    finally:
        client.close()


def test_tofu_rotated_certificate_is_rejected(mock_camera):
    pins = PinStore()
    c1 = make_client(mock_camera, pin_store=pins)
    c1.login()
    c1.close()
    old = pins.get(mock_camera.host, mock_camera.https_port)
    mock_camera.rotate_certificate()
    c2 = make_client(mock_camera, pin_store=pins)
    try:
        with pytest.raises(CertificateMismatchError) as ei:
            c2.login()
        assert ei.value.expected == old
        assert ei.value.actual == mock_camera.cert_fingerprint
        assert pins.get(mock_camera.host, mock_camera.https_port) == old, "pin must not be silently replaced"
    finally:
        c2.close()


def test_tofu_forget_then_repin(mock_camera):
    pins = PinStore()
    c0 = make_client(mock_camera, pin_store=pins)
    c0.login()
    c0.close()
    mock_camera.rotate_certificate()
    pins.forget(mock_camera.host, mock_camera.https_port)
    c = make_client(mock_camera, pin_store=pins)
    try:
        c.login()
        assert pins.get(mock_camera.host, mock_camera.https_port) == mock_camera.cert_fingerprint
    finally:
        c.close()


def test_transport_fingerprint_property(mock_camera):
    from tapo_desktop.core.control_client.transport import HttpsTransport

    t = HttpsTransport(mock_camera.host, mock_camera.https_port, pin_store=PinStore(), timeout=5)
    try:
        status, _headers, text = t.post("/", '{"method":"login","params":{"cnonce":"ABCDEF0123456789",'
                                             '"encrypt_type":"3","username":"admin"}}',
                                        {"Content-Type": "application/json"})
        assert status == 200
        assert '"error_code":-40413' in text.replace(" ", "")
        assert t.fingerprint == mock_camera.cert_fingerprint
    finally:
        t.close()


# --------------------------------------------------------------------------- concurrency
@pytest.mark.timeout(60)
def test_concurrent_requests_8x25(mock_camera):
    client = make_client(mock_camera)
    errors: list[BaseException] = []

    def work(i):
        for j in range(25):
            try:
                if (i + j) % 2:
                    client.get_led()
                else:
                    client.get_device_info()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

    try:
        with ThreadPoolExecutor(8) as ex:
            list(ex.map(work, range(8)))
        assert errors == []
        assert mock_camera.login_count == 1, "seq desync would have forced re-logins"
        ms = methods(mock_camera)
        assert ms.count("getLedStatus") + ms.count("getDeviceInfo") == 200
    finally:
        client.close()


def test_credentials_not_in_repr(mock_camera):
    client = make_client(mock_camera)
    try:
        client.login()
        assert cloud_password(mock_camera) not in repr(client)
        assert cloud_password(mock_camera) not in str(client)
    finally:
        client.close()
