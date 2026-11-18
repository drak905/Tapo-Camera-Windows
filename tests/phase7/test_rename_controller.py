"""AppController.rename_camera / reset_camera_name and how AccountManager cloud refreshes treat the names:
owned cloud cameras are renamed on the camera (optimistic, rolled back on failure, pending until the cloud
reports the new alias or 24 h pass); shared and manual cameras are renamed in this app only."""
from __future__ import annotations

import dataclasses
import time

import pytest

from tapo_desktop.core.errors import TapoDeviceError
from tapo_desktop.services.camera_names import PENDING_RENAME_TTL_S, InvalidCameraName
from tests.e2e.fakes import make_config
from tests.phase2._app_helpers import sign_in
from tests.phase2.app_fakes import EMAIL, PASSWORD, cloud_device

pytestmark = [pytest.mark.ui, pytest.mark.timeout(60)]


def _owned(i: int = 0, **kw):
    return make_config(i, source="cloud", role=0, device_id=f"dev{i}", cloud_alias=f"Cam {i}", lan_found=True, **kw)


def _shared(i: int = 0, **kw):
    return make_config(i, source="cloud", role=1, share_type="deviceShare", device_id=f"dev{i}",
                       cloud_alias=f"Cam {i}", **kw)


def _stored(env, camera_id):
    return next(c for c in env.store.load() if c.id == camera_id)


def _record(controller):
    finished, failed = [], []
    controller.rename_finished.connect(lambda cid, err: finished.append((cid, err)))
    controller.command_failed.connect(lambda cid, action, msg: failed.append((cid, action, msg)))
    return finished, failed


# ================================================================== controller
def test_manual_camera_is_renamed_in_this_app_only(make_env, qtbot):
    env = make_env(1)
    finished, _failed = _record(env.controller)
    with qtbot.waitSignal(env.controller.cameras_changed, timeout=2000):
        on_camera = env.controller.rename_camera("cam0", "  Garage  ")
    assert on_camera is False
    assert env.controller.camera("cam0").name == "Garage"
    assert _stored(env, "cam0").name == "Garage"
    env.idle(qtbot)
    assert finished == [("cam0", "")]
    assert not env.has_svc("cam0") or env.svc("cam0").calls_to("rename") == []


def test_owned_cloud_camera_is_renamed_on_the_camera_optimistically(make_env, qtbot):
    env = make_env(cameras=[_owned(0)])
    finished, failed = _record(env.controller)
    assert env.controller.rename_camera("cam0", "Porch") is True
    cfg = env.controller.camera("cam0")
    assert cfg.name == "Porch" and cfg.pending_name == "Porch" and cfg.pending_since > 0  # before the camera answers
    env.idle(qtbot)
    assert [c.args for c in env.svc("cam0").calls_to("rename")] == [("Porch",)]
    assert finished == [("cam0", "")] and failed == []
    stored = _stored(env, "cam0")
    assert stored.name == "Porch" and stored.pending_name == "Porch"


def test_a_failed_camera_rename_is_rolled_back_with_a_message(make_env, qtbot):
    env = make_env(cameras=[_owned(0)])
    env.factory.fail("cam0", "rename", TapoDeviceError(-63103, "setDeviceAlias"))
    finished, failed = _record(env.controller)
    env.controller.rename_camera("cam0", "Porch")
    env.idle(qtbot)
    cfg = env.controller.camera("cam0")
    assert cfg.name == "Cam 0" and cfg.pending_name == "" and cfg.pending_since == 0.0
    assert _stored(env, "cam0").name == "Cam 0"
    assert len(failed) == 1 and failed[0][:2] == ("cam0", "rename_camera") and failed[0][2]
    assert len(finished) == 1 and finished[0][0] == "cam0" and finished[0][1]


def test_renaming_to_the_current_camera_name_still_asks_the_camera_and_sets_nothing_pending(make_env, qtbot):
    env = make_env(cameras=[_owned(0)])
    env.controller.rename_camera("cam0", "Cam 0")
    env.idle(qtbot)
    assert [c.args for c in env.svc("cam0").calls_to("rename")] == [("Cam 0",)]
    assert env.controller.camera("cam0").pending_name == ""


@pytest.mark.parametrize("bad", ["", "   ", "x" * 33, "ổ" * 11])
def test_invalid_names_are_refused_before_anything_changes(make_env, qtbot, bad):
    env = make_env(cameras=[_owned(0)])
    with pytest.raises(InvalidCameraName):
        env.controller.rename_camera("cam0", bad)
    assert env.controller.camera("cam0").name == "Cam 0"
    env.idle(qtbot)
    assert not env.has_svc("cam0") or env.svc("cam0").calls_to("rename") == []


def test_unknown_camera_raises_key_error(make_env):
    env = make_env(1)
    with pytest.raises(KeyError):
        env.controller.rename_camera("nope", "Name")
    with pytest.raises(KeyError):
        env.controller.reset_camera_name("nope")


def test_shared_camera_gets_a_local_display_name_and_can_be_reset(make_env, qtbot):
    env = make_env(cameras=[_shared(0)])
    assert env.controller.rename_camera("cam0", "Bà ngoại") is False
    cfg = env.controller.camera("cam0")
    assert cfg.name == "Bà ngoại" and cfg.display_name == "Bà ngoại" and cfg.cloud_alias == "Cam 0"
    env.idle(qtbot)
    assert not env.has_svc("cam0") or env.svc("cam0").calls_to("rename") == []
    env.controller.reset_camera_name("cam0")
    cfg = env.controller.camera("cam0")
    assert cfg.name == "Cam 0" and cfg.display_name == ""
    assert _stored(env, "cam0").display_name == ""


def test_renaming_does_not_restart_the_stream(make_env, qtbot):
    env = make_env(cameras=[_owned(0)])
    env.controller.start_view("cam0", env.cameras[0].grid_quality, 320, 180)
    env.idle(qtbot)
    starts = len(env.svc("cam0").calls_to("start_stream"))
    env.controller.rename_camera("cam0", "Porch")
    env.idle(qtbot)
    assert len(env.svc("cam0").calls_to("start_stream")) == starts
    assert env.svc("cam0").calls_to("close") == []


# ================================================================== cloud refreshes (AccountManager)
def _refresh(env, qtbot):
    with qtbot.waitSignal(env.accounts.cameras_refreshed, timeout=5000):
        env.accounts.refresh_cameras()
    env.idle(qtbot)


def test_cloud_refresh_keeps_the_pending_name_then_accepts_the_matching_alias(app_env, qtbot):
    dev = cloud_device(0, alias="Front")
    env = app_env(devices=[dev])
    sign_in(env, qtbot, EMAIL, PASSWORD)
    cid = dev.device_id
    assert env.controller.camera(cid).cloud_alias == "Front"
    env.controller.rename_camera(cid, "Porch")
    env.idle(qtbot)
    _refresh(env, qtbot)  # the cloud still reports the old alias
    cfg = env.controller.camera(cid)
    assert cfg.name == "Porch" and cfg.pending_name == "Porch" and cfg.cloud_alias == "Front"
    env.cloud.devices = [dataclasses.replace(dev, alias="Porch")]
    _refresh(env, qtbot)
    cfg = env.controller.camera(cid)
    assert cfg.name == "Porch" and cfg.pending_name == "" and cfg.cloud_alias == "Porch"
    env.cloud.devices = [dataclasses.replace(dev, alias="Renamed on the phone")]
    _refresh(env, qtbot)
    assert env.controller.camera(cid).name == "Renamed on the phone"


def test_a_pending_name_expires_after_24_hours(app_env, qtbot):
    dev = cloud_device(0, alias="Front")
    env = app_env(devices=[dev])
    sign_in(env, qtbot, EMAIL, PASSWORD)
    cid = dev.device_id
    env.controller.rename_camera(cid, "Porch")
    env.idle(qtbot)
    cfg = env.controller.camera(cid)
    env.controller.upsert_cameras([dataclasses.replace(cfg, pending_since=time.time() - PENDING_RENAME_TTL_S - 5)])
    _refresh(env, qtbot)
    cfg = env.controller.camera(cid)
    assert cfg.name == "Front" and cfg.pending_name == ""


def test_shared_camera_local_name_survives_refreshes_and_reset_shows_the_cloud_name(app_env, qtbot):
    dev = dataclasses.replace(cloud_device(1, alias="Owner's cam"), role=1)
    env = app_env(devices=[dev])
    sign_in(env, qtbot, EMAIL, PASSWORD)
    cid = dev.device_id
    assert env.controller.camera(cid).role == 1
    assert env.controller.rename_camera(cid, "Nhà bà") is False
    _refresh(env, qtbot)
    assert env.controller.camera(cid).name == "Nhà bà"
    env.cloud.devices = [dataclasses.replace(dev, alias="Owner renamed")]
    _refresh(env, qtbot)
    cfg = env.controller.camera(cid)
    assert cfg.name == "Nhà bà" and cfg.cloud_alias == "Owner renamed"
    env.controller.reset_camera_name(cid)
    assert env.controller.camera(cid).name == "Owner renamed"
    _refresh(env, qtbot)
    assert env.controller.camera(cid).name == "Owner renamed"
    svc = env.factory.services.get(cid)
    assert svc is None or svc.calls_to("rename") == []


def test_offline_refresh_keeps_the_real_camera_name_for_reset(app_env, qtbot):
    dev = dataclasses.replace(cloud_device(2, alias="Owner's cam"), role=1)
    env = app_env(devices=[dev])
    sign_in(env, qtbot, EMAIL, PASSWORD)
    cid = dev.device_id
    env.controller.rename_camera(cid, "Mine")
    env.cloud.offline = True
    _refresh(env, qtbot)  # cloud unavailable: cached list
    cfg = env.controller.camera(cid)
    assert cfg.name == "Mine" and cfg.cloud_alias == "Owner's cam"
    env.controller.reset_camera_name(cid)
    assert env.controller.camera(cid).name == "Owner's cam"


def test_names_persist_across_a_restart(app_env, qtbot, tmp_path):
    from tapo_desktop.services.config_store import ConfigStore

    dev = cloud_device(0, alias="Front")
    env = app_env(devices=[dev])
    sign_in(env, qtbot, EMAIL, PASSWORD)
    env.controller.rename_camera(dev.device_id, "Porch")
    env.idle(qtbot)
    stored = next(c for c in ConfigStore(env.store.path).load() if c.id == dev.device_id)
    assert stored.name == "Porch" and stored.pending_name == "Porch" and stored.cloud_alias == "Front"
