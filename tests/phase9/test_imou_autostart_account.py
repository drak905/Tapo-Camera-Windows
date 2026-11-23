"""iMou auto-start, account side: the online state a Refresh learns reaches the services, an offline camera that comes
back starts its tile, and a refused key found while streaming is remembered. Mock cloud, no network."""
from __future__ import annotations

from tapo_desktop.models import StreamQuality
from tests.phase9.test_imou_account import connect

CID = "imou:MOCKSERIAL0001"


def spy_now(env):
    asked = []
    real = env.controller.view_remote_now
    env.controller.view_remote_now = lambda cid: (asked.append(cid), real(cid))[1]
    return asked


def test_a_refresh_fills_the_runtime_online_map(imou_env, qtbot, cloud):
    from tapo_desktop.services import imou_camera_service

    cloud.devices[0]["deviceStatus"] = "offline"
    env = imou_env()
    connect(env, qtbot)
    rt = imou_camera_service.get_runtime()
    assert rt.online_of(CID, default=True) is False and rt.online_of("imou:MOCKSERIAL0002", default=False) is True
    cloud.devices[0]["deviceStatus"] = "online"
    env.manager.refresh()
    env.idle(qtbot)
    assert rt.online_of(CID, default=False) is True


def test_a_camera_that_comes_back_online_in_a_refresh_starts_its_tile_and_only_that_one(imou_env, qtbot, cloud):
    cloud.devices[0]["deviceStatus"] = "offline"
    env = imou_env()
    connect(env, qtbot)
    asked = spy_now(env)
    cloud.devices[0]["deviceStatus"] = "online"
    env.manager.refresh()
    env.idle(qtbot)
    assert asked == [CID]
    env.manager.refresh()  # nothing changed: nobody is restarted
    env.idle(qtbot)
    assert asked == [CID]


def test_a_refresh_that_changes_nothing_restarts_no_running_camera(imou_env, qtbot, cloud):
    env = imou_env()
    connect(env, qtbot)
    asked = spy_now(env)
    env.manager.refresh()
    env.idle(qtbot)
    assert asked == []


def test_a_camera_that_goes_offline_in_a_refresh_is_not_started(imou_env, qtbot, cloud):
    env = imou_env()
    connect(env, qtbot)
    asked = spy_now(env)
    cloud.devices[0]["deviceStatus"] = "offline"
    env.manager.refresh()
    env.idle(qtbot)
    assert asked == []


def test_a_start_that_meets_a_refused_key_sets_the_failed_flag_so_the_next_launch_does_not_reconnect(imou_env, qtbot, cloud):
    from tapo_desktop.core.imou.errors import ImouError
    from tapo_desktop.services import imou_camera_service

    import pytest

    env = imou_env()
    connect(env, qtbot)
    assert env.store.get_setting("imou_last_failed", False) is False
    svc = imou_camera_service.ImouCameraService(env.controller.camera(CID), env.vault)
    cloud.fail("createVideoDownloadStreamUrl", "SN1001", "words", times=None)
    with pytest.raises(ImouError):
        svc.start_stream(StreamQuality.SD, 640, 360, lambda f: None, lambda s, r=None: None)
    assert env.store.get_setting("imou_last_failed", False) is True
    again = env.build_manager()
    imou_camera_service.set_runtime(None)
    again.restore()
    assert imou_camera_service.get_runtime() is None
