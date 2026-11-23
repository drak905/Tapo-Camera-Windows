"""iMou account manager: Test = one token + one list call, secrets only in the vault, manual refresh, clean sign-out.

Mock cloud and fake Tapo services only; the real controller, config store and Tapo/Kami managers are used for the
cross-vendor checks.
"""
from __future__ import annotations

import json
import time

import pytest

from tapo_desktop.models import CameraConfig, CredentialKind, Credentials
from tests.fixtures.mock_imou_cloud import MOCK_APP_ID, MOCK_APP_SECRET, make_device


def tapo_cfg(i=0) -> CameraConfig:
    return CameraConfig(name=f"Hall {i}", host=f"192.0.2.{10 + i}", id=f"lan{i}", model="C200", enable_onvif_events=False)


def cloud_cfg() -> CameraConfig:
    return CameraConfig(name="Cloud Cam", host="", id="cloud-dev-1", source="cloud", device_id="cloud-dev-1",
                        model="C510W", lan_found=False, enable_onvif_events=False)


def kami_like_cfg() -> CameraConfig:
    return CameraConfig(name="Other Vendor Cam", host="", id="kami:u1", source="kami", device_id="u1",
                        lan_found=False, enable_onvif_events=False, vendor_data={"uid": "u1"})


def connect(env, qtbot, *, secret=MOCK_APP_SECRET, dc="sg"):
    env.manager.test(MOCK_APP_ID, secret, dc)
    env.idle(qtbot)


# ------------------------------------------------------------------------------------------ Test button
def test_test_makes_exactly_one_token_call_and_one_list_call(imou_env, qtbot, cloud):
    env = imou_env()
    connect(env, qtbot)
    assert cloud.paths() == ["accessToken", "listDeviceDetailsByPage"]
    assert env.signals["failed"] == [] and len(env.signals["connected"]) == 1


def test_connected_adds_the_cameras_with_imou_ids_and_non_secret_vendor_data(imou_env, qtbot, cloud):
    cloud.devices[1]["deviceStatus"] = "offline"
    env = imou_env()
    connect(env, qtbot)
    cams = {c.id: c for c in env.controller.cameras()}
    assert set(cams) == {"imou:MOCKSERIAL0001", "imou:MOCKSERIAL0002"}
    a, b = cams["imou:MOCKSERIAL0001"], cams["imou:MOCKSERIAL0002"]
    assert (a.source, a.host, a.name, a.device_id, a.online, a.lan_found) == (
        "imou", "", "Mock Camera A", "MOCKSERIAL0001", True, False)
    assert b.online is False
    assert a.vendor_data == {"serial": "MOCKSERIAL0001", "channel": "0", "cloud_name": "Mock Camera A",
                             "data_center": "sg"}
    assert a.enable_onvif_events is False  # no ONVIF for iMou
    assert not any(MOCK_APP_ID in json.dumps(v) or MOCK_APP_SECRET in json.dumps(v) for v in
                   (c.vendor_data for c in cams.values()))


def test_secrets_live_only_in_the_vault_never_in_config_or_logs(imou_env, qtbot, cloud, caplog, tmp_path):
    import logging

    caplog.set_level(logging.DEBUG)
    env = imou_env()
    connect(env, qtbot)
    creds = env.vault.get("__imou_app__", CredentialKind.IMOU_APP)
    assert creds == Credentials(MOCK_APP_ID, MOCK_APP_SECRET)
    text = env.store.path.read_text(encoding="utf-8")
    assert MOCK_APP_SECRET not in text and MOCK_APP_ID not in text
    assert env.store.get_setting("imou_data_center", None) == "sg"
    assert MOCK_APP_SECRET not in "\n".join(r.getMessage() for r in caplog.records)
    assert MOCK_APP_ID not in "\n".join(r.getMessage() for r in caplog.records)
    assert MOCK_APP_ID not in env.signals["connected"][0] and "***" in env.signals["connected"][0]


def test_connected_sets_the_runtime_for_the_camera_services(imou_env, qtbot):
    from tapo_desktop.services import imou_camera_service

    env = imou_env()
    assert imou_camera_service.get_runtime() is None
    connect(env, qtbot)
    assert imou_camera_service.get_runtime() is not None
    assert env.manager.is_connected()


def test_wrong_secret_is_one_call_nothing_stored_and_a_two_minute_block(imou_env, qtbot, cloud, clock):
    env = imou_env()
    connect(env, qtbot, secret="wrong-secret")
    assert cloud.paths() == ["accessToken"]
    assert len(env.signals["failed"]) == 1 and "App ID" in env.signals["failed"][0]
    assert env.vault.get("__imou_app__", CredentialKind.IMOU_APP) is None
    assert env.ids() == [] and not env.manager.is_connected()
    until = env.manager.blocked_until()
    assert until is not None and 100 < until - clock() <= 120
    connect(env, qtbot, secret="wrong-secret")  # a second click inside the block sends nothing
    assert cloud.paths() == ["accessToken"] and len(env.signals["failed"]) == 2
    clock.advance(121)
    assert env.manager.blocked_until() is None
    connect(env, qtbot)
    assert cloud.paths() == ["accessToken", "accessToken", "listDeviceDetailsByPage"]
    assert env.manager.is_connected()


def test_a_refused_test_with_the_wrong_secret_never_overwrites_the_saved_connection(imou_env, qtbot, cloud, clock):
    env = imou_env()
    connect(env, qtbot)
    clock.advance(1)
    cloud.fail("accessToken", "SN1001")
    env.manager.test(MOCK_APP_ID, "another-secret", "sg")
    env.idle(qtbot)
    assert env.vault.get("__imou_app__", CredentialKind.IMOU_APP) == Credentials(MOCK_APP_ID, MOCK_APP_SECRET)


def test_unknown_data_center_or_empty_fields_send_nothing(imou_env, qtbot, cloud):
    env = imou_env()
    for args in (("", MOCK_APP_SECRET, "sg"), (MOCK_APP_ID, "", "sg"), (MOCK_APP_ID, MOCK_APP_SECRET, "evil"),
                 (MOCK_APP_ID, MOCK_APP_SECRET, "https://evil.example")):
        env.manager.test(*args)
        env.idle(qtbot)
    assert cloud.calls == [] and len(env.signals["failed"]) == 4


def test_a_second_test_while_one_runs_is_refused(imou_env, qtbot, cloud):
    env = imou_env()
    env.manager.test(MOCK_APP_ID, MOCK_APP_SECRET, "sg")
    env.manager.test(MOCK_APP_ID, MOCK_APP_SECRET, "sg")
    env.idle(qtbot)
    assert cloud.count("accessToken") == 1 and cloud.count("listDeviceDetailsByPage") == 1
    assert any("already" in m.lower() for m in env.signals["failed"])


def test_a_connection_error_is_a_message_and_stores_nothing(imou_env, qtbot, cloud):
    env = imou_env()
    cloud.drop_connection = True
    connect(env, qtbot)
    assert len(env.signals["failed"]) == 1 and "reach" in env.signals["failed"][0].lower()
    assert env.vault.get("__imou_app__", CredentialKind.IMOU_APP) is None


def test_quota_block_survives_a_restart_and_stops_test_before_any_call(imou_env, qtbot, cloud, clock):
    env = imou_env()
    connect(env, qtbot)
    cloud.fail("listDeviceDetailsByPage", "OP1014")
    env.manager.refresh()
    env.idle(qtbot)
    assert any("free quota used" in m.lower() for m in env.signals["failed"])
    calls = len(cloud.calls)
    again = env.build_manager()
    again.restore()
    again.test(MOCK_APP_ID, MOCK_APP_SECRET, "sg")
    again.wait_idle(3000)
    qtbot.wait(20)
    assert len(cloud.calls) == calls
    assert "free quota used" in env.signals["failed"][-1].lower()


# ------------------------------------------------------------------------------------------ refresh / restore
def test_no_call_at_construction_or_restore_and_no_polling(imou_env, qtbot, cloud, clock):
    env = imou_env()
    connect(env, qtbot)
    n = len(cloud.calls)
    again = env.build_manager()
    again.restore()
    again.wait_idle(3000)
    qtbot.wait(200)
    clock.advance(3600)
    qtbot.wait(100)
    assert len(cloud.calls) == n
    assert again.is_connected()


def test_refresh_is_one_list_call_with_the_cached_token_and_updates_online_state(imou_env, qtbot, cloud):
    env = imou_env()
    connect(env, qtbot)
    cloud.devices[0]["deviceStatus"] = "offline"
    cloud.devices.append(make_device("MOCKSERIAL0003", "Mock Camera C"))
    before = len(cloud.calls)
    env.manager.refresh()
    env.idle(qtbot)
    assert cloud.paths()[before:] == ["listDeviceDetailsByPage"]
    cams = {c.id: c for c in env.controller.cameras()}
    assert cams["imou:MOCKSERIAL0001"].online is False and "imou:MOCKSERIAL0003" in cams


def test_refresh_after_a_restart_needs_one_token_call_then_one_list_call(imou_env, qtbot, cloud):
    env = imou_env()
    connect(env, qtbot)
    again = env.build_manager()
    again.restore()
    again.wait_idle(3000)
    before = len(cloud.calls)
    again.refresh()
    again.wait_idle(3000)
    qtbot.wait(20)
    assert cloud.paths()[before:] == ["accessToken", "listDeviceDetailsByPage"]


def test_a_local_name_survives_a_refresh_and_a_cloud_rename_applies_when_not_renamed_locally(imou_env, qtbot, cloud):
    env = imou_env()
    connect(env, qtbot)
    env.controller.rename_camera("imou:MOCKSERIAL0001", "Front Gate")
    cloud.devices[0]["deviceName"] = "Cloud Renamed A"
    cloud.devices[1]["deviceName"] = "Cloud Renamed B"
    env.manager.refresh()
    env.idle(qtbot)
    names = {c.id: c.name for c in env.controller.cameras()}
    assert names == {"imou:MOCKSERIAL0001": "Front Gate", "imou:MOCKSERIAL0002": "Cloud Renamed B"}


def test_refresh_drops_imou_cameras_that_are_no_longer_listed_and_nothing_else(imou_env, qtbot, cloud):
    env = imou_env([tapo_cfg(), cloud_cfg(), kami_like_cfg()])
    connect(env, qtbot)
    cloud.devices.pop(1)
    env.manager.refresh()
    env.idle(qtbot)
    assert env.ids("imou") == ["imou:MOCKSERIAL0001"]
    assert set(env.ids()) == {"lan0", "cloud-dev-1", "kami:u1", "imou:MOCKSERIAL0001"}


def test_refresh_when_not_connected_does_nothing(imou_env, qtbot, cloud):
    env = imou_env()
    env.manager.refresh()
    env.idle(qtbot)
    assert cloud.calls == []


def test_restore_does_not_connect_after_a_failed_attempt(imou_env, qtbot, cloud):
    from tapo_desktop.services import imou_camera_service

    env = imou_env()
    connect(env, qtbot)
    connect(env, qtbot, secret="wrong-secret")  # blocked or refused: the failure flag is set
    imou_camera_service.set_runtime(None)
    again = env.build_manager()
    again.restore()
    assert imou_camera_service.get_runtime() is None and not again.is_connected()


# ------------------------------------------------------------------------------------------ disconnect and isolation
def test_disconnect_removes_only_imou_cameras_and_only_the_imou_vault_entry(imou_env, qtbot):
    from tapo_desktop.services import imou_camera_service

    env = imou_env([tapo_cfg(), cloud_cfg(), kami_like_cfg()])
    for cid in ("lan0", "cloud-dev-1"):
        env.vault.set(cid, CredentialKind.CLOUD, Credentials("admin", "pw"))
    env.vault.set("__tplink_account__", CredentialKind.CLOUD, Credentials("owner@example.com", "pw"))
    env.vault.set("__kami_account__", CredentialKind.KAMI_ACCOUNT, Credentials("owner@example.com", "pw"))
    connect(env, qtbot)
    env.vault.deleted.clear()
    env.manager.disconnect()
    assert env.ids() == ["lan0", "cloud-dev-1", "kami:u1"]
    assert env.vault.get("__imou_app__", CredentialKind.IMOU_APP) is None
    assert set(env.vault.deleted) <= {"__imou_app__", "imou:MOCKSERIAL0001", "imou:MOCKSERIAL0002"}
    assert env.vault.get("__tplink_account__", CredentialKind.CLOUD) is not None
    assert env.vault.get("__kami_account__", CredentialKind.KAMI_ACCOUNT) is not None
    assert env.vault.get("lan0", CredentialKind.CLOUD) is not None
    assert imou_camera_service.get_runtime() is None and not env.manager.is_connected()
    assert env.signals["disconnected"] == 1
    assert env.store.get_setting("imou_account", None) is None


def test_tapo_and_kami_sign_out_do_not_touch_imou_cameras_or_secrets(imou_env, qtbot):
    from tapo_desktop.services.account import AccountManager
    from tapo_desktop.services.kami_account import KamiAccountManager

    env = imou_env([cloud_cfg(), kami_like_cfg()])
    connect(env, qtbot)
    imou_ids = env.ids("imou")
    assert len(imou_ids) == 2
    tapo = AccountManager(env.controller, cloud_factory=lambda t: None, discover=lambda: [])
    tapo.sign_out()
    kami = KamiAccountManager(env.controller, env.vault, env.store, client_factory=lambda r, c: None)
    kami.sign_out()
    assert env.ids("imou") == imou_ids
    assert env.vault.get("__imou_app__", CredentialKind.IMOU_APP) == Credentials(MOCK_APP_ID, MOCK_APP_SECRET)
    assert env.manager.is_connected()


# ------------------------------------------------------------------------------------------ settings
def test_limits_default_to_twenty_minutes_and_two_streams(imou_env):
    env = imou_env()
    limits = env.manager.limits()
    assert (limits.daily_seconds, limits.max_live, limits.idle_seconds, limits.stall_timeout) == (1200.0, 2, 600.0, 30.0)
    assert (env.manager.daily_minutes(), env.manager.max_live()) == (20, 2)


def test_set_limits_is_clamped_saved_and_read_again_on_the_next_start(imou_env):
    env = imou_env()
    env.manager.set_limits(5, 3)
    assert (env.manager.limits().daily_seconds, env.manager.limits().max_live) == (300.0, 3)
    env.manager.set_limits(100000, 99)
    assert (env.manager.daily_minutes(), env.manager.max_live()) == (600, 4)
    env.manager.set_limits(0, 0)
    assert (env.manager.daily_minutes(), env.manager.max_live()) == (1, 1)
    again = env.build_manager()
    assert (again.daily_minutes(), again.max_live()) == (1, 1)


@pytest.mark.parametrize("junk", [None, "x", -3, 0, 1e9, True, [1]])
def test_damaged_limit_settings_fall_back_to_the_defaults(imou_env, junk):
    env = imou_env()
    env.store.set_setting("imou_daily_minutes", junk)
    env.store.set_setting("imou_max_live", junk)
    assert env.manager.daily_minutes() in (20, 600, 1) and env.manager.max_live() in (2, 4, 1)
    if junk in (None, "x", True, [1]):
        assert (env.manager.daily_minutes(), env.manager.max_live()) == (20, 2)


def test_a_click_after_connect_starts_a_stream_through_the_real_service_and_counts_minutes(imou_env, qtbot, cloud, clock):
    from tapo_desktop.models import StreamQuality
    from tapo_desktop.services import imou_camera_service

    env = imou_env()
    connect(env, qtbot)
    rt = imou_camera_service.get_runtime()
    svc = imou_camera_service.ImouCameraService(env.controller.camera("imou:MOCKSERIAL0001"), env.vault)
    states = []
    svc.start_stream(StreamQuality.SD, 640, 360, lambda f: None, lambda s, r=None: states.append(s.name))
    assert env.sup_instances[0].started
    clock.advance(60)
    svc.stop_stream()
    assert rt.meter.used("imou:MOCKSERIAL0001") == 60
    assert env.store.get_setting("imou_usage", None)["seconds"]["imou:MOCKSERIAL0001"] == 60
    assert cloud.count("accessToken") == 1  # the token of the Test is reused


def test_the_minute_estimate_is_the_sum_over_all_cameras_for_today(imou_env, qtbot, clock):
    from tapo_desktop.services import imou_camera_service

    env = imou_env()
    assert env.manager.usage_minutes_today() == 0
    connect(env, qtbot)
    meter = imou_camera_service.get_runtime().meter
    meter.add("imou:MOCKSERIAL0001", 90)
    meter.add("imou:MOCKSERIAL0002", 30)
    assert env.manager.usage_minutes_today() == 2.0
    clock.advance(86_400)
    assert env.manager.usage_minutes_today() == 0
