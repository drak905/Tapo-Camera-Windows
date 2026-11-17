"""UI / controller / account wiring for remote live view: "Remote" row text, the "Remote - click to view"
tile and click-to-promote, AccountManager exposing the relay provider, and one end-to-end stream from
the real controller wiring through the mock relay."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtCore import Qt  # noqa: E402

from tapo_desktop.models import CameraConfig, CredentialKind, Credentials, StreamQuality  # noqa: E402
from tapo_desktop.services.remote import REMOTE, REMOTE_CLICK_TO_VIEW, RelayProvider  # noqa: E402
from tests.e2e.fakes import FakeServiceFactory, FakeVault  # noqa: E402
from tests.phase3.conftest import DEFAULT_CLOUD_DEVICES, DEFAULT_EMAIL, DEFAULT_PASSWORD  # noqa: E402

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


def _remote_cfg(i: int = 1, **kw) -> CameraConfig:
    base = dict(name=f"Cabin {i}", host="", id=f"REM{i}", source="cloud", device_id=f"REM{i}", model="C210",
                lan_found=False, online=True)
    base.update(kw)
    return CameraConfig(**base)


@pytest.fixture
def ctl_env(tmp_path, qtbot):
    from tapo_desktop.services.config_store import ConfigStore
    from tapo_desktop.services.controller import AppController

    store = ConfigStore(tmp_path / "config.json")
    cams = [_remote_cfg(1), _remote_cfg(2, online=False), CameraConfig(name="Hall", host="192.0.2.5", id="lan1")]
    store.save(cams)
    vault = FakeVault()
    for c in cams:
        vault.set(c.id, CredentialKind.CLOUD, Credentials("admin", "pw"))
    factory = FakeServiceFactory()
    controller = AppController(store, vault, service_factory=factory)
    yield controller, factory
    controller.stop_all()
    factory.shutdown()


def test_row_status_helper():
    from tapo_desktop.services.camera_service import NOT_ON_NETWORK
    from tapo_desktop.ui.main_window import camera_row_status

    cfg = _remote_cfg()
    assert camera_row_status(cfg) == NOT_ON_NETWORK
    assert camera_row_status(cfg, remote=True) == REMOTE == "Remote"
    assert camera_row_status(_remote_cfg(online=False), remote=True) == "Offline"
    assert camera_row_status(CameraConfig(name="x", host="10.0.0.2"), remote=True) == ""


def test_controller_remote_available(ctl_env):
    controller, _ = ctl_env
    assert controller.remote_provider() is None
    assert not controller.remote_available("REM1")
    provider = RelayProvider(object(), terminal_uuid="T" * 32)
    controller.set_remote_provider(provider)
    assert controller.remote_provider() is provider
    assert controller.remote_available("REM1")
    assert not controller.remote_available("REM2")  # offline
    assert not controller.remote_available("lan1")  # on the LAN: local path
    assert not controller.remote_available("nope")
    controller.set_remote_provider(None)
    assert not controller.remote_available("REM1")


def test_main_window_rows_say_remote(ctl_env, qtbot):
    from tapo_desktop.ui.main_window import STATUS_ROLE, MainWindow

    controller, _ = ctl_env
    controller.set_remote_provider(RelayProvider(object(), terminal_uuid="T" * 32))
    w = MainWindow(controller, tray_enabled=False)
    qtbot.addWidget(w)
    lst = w.findChild(type(w._list), "cameraList")
    rows = {lst.item(i).data(Qt.ItemDataRole.UserRole): lst.item(i) for i in range(lst.count())}
    assert rows["REM1"].text() == "Cabin 1 (Remote)" and rows["REM1"].data(STATUS_ROLE) == "Remote"
    assert "Not on this network" not in rows["REM1"].text()
    assert "Offline" in rows["REM2"].text()
    assert rows["lan1"].text() == "Hall"


def test_tile_headline_for_waiting_remote_stream(qtbot):
    from tapo_desktop.ui.video_tile import VideoTile

    tile = VideoTile("REM1", "Cabin")
    qtbot.addWidget(tile)
    tile.set_stream_state("FAILED", REMOTE_CLICK_TO_VIEW)
    assert tile.status_text() == "Remote - click to view"
    tile.set_stream_state("FAILED", "Camera is offline")
    assert tile.status_text() == "Offline"


def test_click_on_waiting_tile_promotes_it(ctl_env, qtbot):
    from tapo_desktop.ui.grid import CameraGrid

    controller, _ = ctl_env
    promoted = []
    controller.view_remote_now = promoted.append  # type: ignore[method-assign]
    controller.start_view = lambda *a, **k: None  # type: ignore[method-assign]  # no fake frames/states racing
    grid = CameraGrid(controller)
    qtbot.addWidget(grid)
    grid.set_grid_size(2)
    grid.show()
    tile = grid.tile("REM1")
    controller.stream_state.emit("REM1", "FAILED", REMOTE_CLICK_TO_VIEW)
    assert tile.status_text() == REMOTE_CLICK_TO_VIEW
    qtbot.mouseClick(tile, Qt.MouseButton.LeftButton)
    assert promoted == ["REM1"]
    assert tile.stream_state() == "CONNECTING"
    # a normal (playing / failed for another reason) tile is not promoted
    controller.stream_state.emit("lan1", "FAILED", "boom")
    qtbot.mouseClick(grid.tile("lan1"), Qt.MouseButton.LeftButton)
    assert promoted == ["REM1"]
    # selection from the camera list promotes as well
    controller.stream_state.emit("REM1", "FAILED", REMOTE_CLICK_TO_VIEW)
    grid.select_camera(None)
    grid.select_camera("REM1")
    assert promoted == ["REM1", "REM1"]


def test_view_remote_now_restarts_with_preempt(ctl_env, qtbot):
    controller, factory = ctl_env
    controller.start_view("REM1", StreamQuality.SD, 640, 360)
    assert controller.wait_idle(3000)
    controller.view_remote_now("REM1")
    assert controller.wait_idle(3000)
    svc = factory.created_for("REM1")[-1]
    assert [c.name for c in svc.calls].count("start_stream") == 2
    controller.view_remote_now("lan1")  # not viewed: ignored
    controller.view_remote_now("nope")
    assert controller.wait_idle(3000)


# ---------------------------------------------------------------------------------- account manager
def _real_cloud_factory(base_url: str):
    from tapo_desktop.core.cloud.client import TapoCloudClient

    return lambda terminal_uuid: TapoCloudClient(terminal_uuid=terminal_uuid, base_url=base_url, timeout=5)


def test_account_manager_exposes_relay_provider(tmp_path, qtbot, relay_cloud, mock_relay):
    from tapo_desktop.services.account import AccountManager
    from tapo_desktop.services.config_store import ConfigStore
    from tapo_desktop.services.controller import AppController

    store = ConfigStore(tmp_path / "config.json")
    store.set_setting("max_remote_streams", 3)
    factory = FakeServiceFactory()
    controller = AppController(store, FakeVault(), service_factory=factory)
    accounts = AccountManager(controller, cloud_factory=_real_cloud_factory(relay_cloud.base_url),
                              discover=lambda: [])
    try:
        with qtbot.waitSignal(accounts.signed_in, timeout=5000):
            accounts.sign_in(DEFAULT_EMAIL, DEFAULT_PASSWORD)
        assert accounts.wait_idle(5000)
        provider = controller.remote_provider()
        assert provider is not None and accounts.relay_provider() is provider
        assert provider.gate.capacity == 3
        front = DEFAULT_CLOUD_DEVICES[0]["deviceId"]
        assert controller.remote_available(front)
        assert not controller.remote_available(DEFAULT_CLOUD_DEVICES[1]["deviceId"])  # status 0 -> offline
        accounts.sign_out()
        assert controller.remote_provider() is None
    finally:
        accounts.wait_idle(3000)
        controller.stop_all()
        factory.shutdown()


def test_fake_cloud_client_without_relay_api_gives_no_provider(tmp_path, qtbot):
    from tapo_desktop.services.account import AccountManager
    from tapo_desktop.services.config_store import ConfigStore
    from tapo_desktop.services.controller import AppController
    from tests.phase2.app_fakes import FakeCloud, cloud_device

    controller = AppController(ConfigStore(tmp_path / "c.json"), FakeVault(), service_factory=FakeServiceFactory())
    accounts = AccountManager(controller, cloud_factory=FakeCloud(devices=[cloud_device(0)]), discover=lambda: [])
    with qtbot.waitSignal(accounts.signed_in, timeout=5000):
        accounts.sign_in("a@b.c", "pw")
    assert accounts.wait_idle(5000)
    assert controller.remote_provider() is None


def test_end_to_end_remote_stream_through_real_wiring(tmp_path, qtbot, relay_cloud, mock_relay, ffmpeg_path,
                                                      monkeypatch):
    """Sign in (real cloud client vs mock cloud), the camera is not on the LAN, the controller's default
    TapoCameraService routes start_view through the relay and frames reach frame_ready."""
    import tapo_desktop.core.media_engine.ffmpeg as ffmpeg_mod
    from tapo_desktop.core.cloud.relay import RelayClient
    from tapo_desktop.services.account import AccountManager
    from tapo_desktop.services.config_store import ConfigStore
    from tapo_desktop.services.controller import AppController

    monkeypatch.setattr(ffmpeg_mod, "find_ffmpeg", lambda *a, **k: ffmpeg_path)
    monkeypatch.setattr("tapo_desktop.core.media_engine.supervisor.find_ffmpeg", lambda *a, **k: ffmpeg_path)
    store = ConfigStore(tmp_path / "config.json")
    controller = AppController(store, FakeVault())
    ctx = mock_relay.client_context()
    accounts = AccountManager(
        controller, cloud_factory=_real_cloud_factory(relay_cloud.base_url), discover=lambda: [],
        relay_factory=lambda cloud: RelayClient(cloud, terminal_uuid=cloud.terminal_uuid, timeout=5,
                                                ssl_context=ctx))
    front = DEFAULT_CLOUD_DEVICES[0]["deviceId"]
    frames, states = [], []
    controller.frame_ready.connect(lambda cid, img: frames.append((cid, img.width(), img.height())))
    controller.stream_state.connect(lambda cid, s, r: states.append((cid, s, r)))
    try:
        with qtbot.waitSignal(accounts.signed_in, timeout=5000):
            accounts.sign_in(DEFAULT_EMAIL, DEFAULT_PASSWORD)
        assert accounts.wait_idle(5000)
        controller.start_view(front, StreamQuality.SD, 640, 360)
        qtbot.waitUntil(lambda: any(cid == front for cid, *_ in frames), timeout=30000)
        assert (front, 640, 360) in frames
        assert any(s == "PLAYING" for cid, s, _ in states if cid == front), states
        assert relay_cloud.calls("/v2/relay/request")[-1]["body"]["cloudType"] == 2
        controller.refresh_status(front)  # control is LAN-only
        failures = []
        controller.command_failed.connect(lambda cid, action, msg: failures.append(msg))
        controller.refresh_status(front)
        qtbot.waitUntil(lambda: bool(failures), timeout=5000)
        assert "Available on the camera's network only" in failures[-1]
    finally:
        controller.stop_all()
        accounts.wait_idle(3000)
    assert mock_relay.stops >= 1
