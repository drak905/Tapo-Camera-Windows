"""RemoteControlClient: the TapoControlClient API over the cloud passthrough, verified against the
mock camera's real shadow state; shared (role 1) things; the -71103 clip-search fallback."""
from __future__ import annotations

import datetime as dt

import pytest

from tapo_desktop.core.errors import TapoDeviceError
from tapo_desktop.models import DayNightMode, RecordingClip
from tests.phase4.conftest import DEVICE_ID, TERMINAL

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]


def _clip_search_forms(cloud) -> list[str]:
    forms = []
    for entry in cloud.passthrough_log:
        for req in entry["requests"]:
            if req.get("method") == "searchVideoWithUTC":
                inner = req["params"]["playback"]["search_video_with_utc"]
                forms.append("player_id" if "player_id" in inner else "id")
    return forms


def test_same_public_api_as_the_lan_client():
    from tapo_desktop.core.cloud.passthrough import RemoteControlClient
    from tapo_desktop.core.control_client.client import TapoControlClient

    public = {n for n in dir(TapoControlClient) if not n.startswith("_")}
    missing = sorted(n for n in public if not hasattr(RemoteControlClient, n))
    assert missing == []


def test_basic_properties(remote_client):
    assert remote_client.logged_in is True
    remote_client.login()  # no-op: the cloud session is the account's
    assert remote_client.protocol == "cloud"
    assert remote_client.uses_local_token is False
    remote_client.reset_auth_backoff()


def test_device_info_and_components(remote_client, remote_camera):
    info = remote_client.get_device_info()
    assert info.model == "C210" and info.mac == remote_camera.state.mac
    assert "ptz" in remote_client.get_components()


def test_toggles_change_the_real_state(remote_client, remote_camera):
    state = remote_camera.state
    remote_client.set_privacy(True)
    assert state.privacy is True and remote_client.get_privacy() is True
    remote_client.set_led(False)
    assert state.led is False and remote_client.get_led() is False
    remote_client.set_day_night(DayNightMode.ON)
    assert state.inf_type == "on" and remote_client.get_day_night() == DayNightMode.ON
    remote_client.set_motion_detection(True, 80)
    assert state.motion_enabled is True and state.motion_sensitivity == 80
    assert remote_client.get_motion_detection() == (True, 80)
    remote_client.set_alarm(True)
    assert state.alarm_active is True
    remote_client.set_alarm(False)
    assert state.alarm_active is False


def test_ptz_and_presets(remote_client, remote_camera):
    state = remote_camera.state
    x0 = state.ptz_x
    remote_client.step(0)
    assert state.ptz_x > x0
    remote_client.stop_move()
    assert state.ptz_moving is False
    remote_client.move(-5, 5)
    remote_client.add_preset("Porch")
    presets = remote_client.get_presets()
    gate = next(p for p in presets if p.name == "Porch")
    remote_client.step(180)
    remote_client.goto_preset(gate.id)
    assert (state.ptz_x, state.ptz_y) == (state.presets[gate.id]["x"], state.presets[gate.id]["y"])
    remote_client.delete_preset(gate.id)
    assert all(p.name != "Porch" for p in remote_client.get_presets())


def test_sd_and_recordings(remote_client, remote_camera):
    disks = remote_client.get_sd_status()
    assert disks and disks[0]["status"] == remote_camera.state.sd_status
    today = dt.datetime.now(dt.timezone.utc).date()
    days = remote_client.days_with_recordings(today - dt.timedelta(days=7), today)
    assert today in days
    clips = remote_client.recordings_for_day(today, tz=dt.timezone.utc)
    expected = sorted((c for c in remote_camera.state.recordings if c.start.date() == today), key=lambda c: c.start)
    assert [(c.start, c.end) for c in clips] == [(c.start, c.end) for c in expected]
    assert all(isinstance(c, RecordingClip) for c in clips)


def test_device_errors_and_request(remote_client, remote_camera):
    remote_camera.inject_error("getLedStatus", -40106)
    with pytest.raises(TapoDeviceError) as err:
        remote_client.get_led()
    assert err.value.code == -40106 and err.value.method == "getLedStatus"
    result = remote_client.request({"method": "getLastAlarmInfo", "params": {"system": {"name": ["last_alarm_info"]}}})
    assert "last_alarm_info" in result["system"]


def test_multiple_request_batches_in_one_cloud_call(remote_client, relay_cloud, remote_camera):
    from tapo_desktop.core.control_client import dtos

    before = len(relay_cloud.passthrough_log)
    items = remote_client.multiple_request([dtos.get_privacy_req(), dtos.get_led_req(), dtos.get_day_night_req()])
    assert [i["error_code"] for i in items] == [0, 0, 0]
    assert len(relay_cloud.passthrough_log) == before + 1


def test_reboot_and_close(remote_client, remote_camera):
    remote_client.reboot()
    assert remote_camera.state.reboot_count == 1
    remote_client.close()
    remote_client.close()  # idempotent; the client stays usable (stateless over the cloud)
    assert remote_client.get_privacy() is False


def test_clip_search_fallback_to_player_id(remote_client, relay_cloud, remote_camera):
    remote_camera.clip_search_requires_player_id = True
    today = dt.datetime.now(dt.timezone.utc).date()
    clips = remote_client.recordings_for_day(today, tz=dt.timezone.utc)
    assert clips
    assert _clip_search_forms(relay_cloud) == ["id", "player_id"]
    sent = [r for e in relay_cloud.passthrough_log for r in e["requests"] if r["method"] == "searchVideoWithUTC"][-1]
    assert sent["params"]["playback"]["search_video_with_utc"]["player_id"] == TERMINAL.lower()
    remote_client.recordings_for_day(today - dt.timedelta(days=1), tz=dt.timezone.utc)
    assert _clip_search_forms(relay_cloud) == ["id", "player_id", "player_id"]  # remembered


def test_clip_search_other_errors_are_not_retried(remote_client, relay_cloud, remote_camera):
    remote_camera.inject_error("searchVideoWithUTC", -40210)
    with pytest.raises(TapoDeviceError):
        remote_client.recordings_for_day(dt.date.today())
    assert _clip_search_forms(relay_cloud) == ["id"]


def test_shared_thing_is_controlled_with_the_users_own_token(mock_cloud_factory, mock_relay):
    """role 1 (shared to this account): same services-sync, same body, the shared user's token."""
    from tapo_desktop.core.cloud.client import TapoCloudClient
    from tapo_desktop.core.cloud.passthrough import CloudPassthrough, RemoteControlClient
    from tests.fixtures.mock_cloud import make_cloud_device
    from tests.fixtures.mock_tapo_server import MockTapoCamera
    from tests.phase4.conftest import DEFAULT_EMAIL, DEFAULT_PASSWORD

    shared = make_cloud_device(7, alias="Neighbour", deviceModel="C510W", role=1)
    cloud = mock_cloud_factory(devices=[shared])
    cam = MockTapoCamera(model="C510W").start()
    try:
        cloud.register_camera(shared["deviceId"], cam)
        client = TapoCloudClient(terminal_uuid=TERMINAL, base_url=cloud.base_url, timeout=5)
        client.login(DEFAULT_EMAIL, DEFAULT_PASSWORD)
        client.local_access_tokens()
        thing = client.things[shared["deviceId"]]
        assert thing.role == 1 and thing.share_type == "deviceShare"
        remote = RemoteControlClient(CloudPassthrough(client), thing.thing_name, thing.app_server_url)
        assert remote.get_device_info().model == "C510W"
        remote.set_privacy(True)
        assert cam.state.privacy is True
        assert cam.login_count == 0 and cam.login_attempts == 0
        hdr = {k.lower(): v for k, v in cloud.request_log[-1]["headers"].items()}
        assert hdr["authorization"] == f"ut|{client.token}"
    finally:
        cam.stop()
    assert DEVICE_ID
