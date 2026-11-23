"""iMou in the UI: the connection dialog, the account-menu section, click-to-start tiles, disabled controls, and proof
that Tapo (and the existing Kami behaviour) are unchanged. Fake account manager and fake services; no network."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtCore import QObject, Qt, pyqtSignal  # noqa: E402
from PyQt6.QtWidgets import QApplication, QComboBox, QGroupBox, QLabel, QLineEdit, QPushButton, QSpinBox  # noqa: E402

from tapo_desktop.models import CameraConfig, CredentialKind, Credentials, StreamQuality  # noqa: E402
from tapo_desktop.services.config_store import ConfigStore  # noqa: E402
from tapo_desktop.services.stream_policy import plan_streams  # noqa: E402
from tests.e2e.fakes import FakeServiceFactory, FakeVault  # noqa: E402

SECRET = "secret-typed-in-the-dialog-0001"
CLICK_HEADLINE = "iMou - click to start"
RETRY_HEADLINE = "iMou - click to retry"


def imou_cfg(serial="MOCKSERIAL0001", name="Mock Camera A", online=True) -> CameraConfig:
    return CameraConfig(name=name, host="", id=f"imou:{serial}", source="imou", device_id=serial, model="IPC-MOCK",
                        online=online, lan_found=False, enable_onvif_events=False,
                        vendor_data={"serial": serial, "channel": "0", "cloud_name": name, "data_center": "sg"})


def lan_cfg(i: int) -> CameraConfig:
    return CameraConfig(name=f"Hall {i}", host=f"192.0.2.{i + 10}", id=f"lan{i}", model="C200",
                        enable_onvif_events=False)


class Env:
    def __init__(self, tmp_path, cams):
        from tapo_desktop.services.controller import AppController

        self.store = ConfigStore(tmp_path / "config.json")
        self.store.save(cams)
        self.vault = FakeVault()
        for c in cams:
            if c.source == "manual":
                self.vault.set(c.id, CredentialKind.CLOUD, Credentials("admin", "pw"))
        self.fake = FakeServiceFactory(frame_interval=0.25)
        self.built: list[str] = []
        self.calls: list[tuple] = []

        def factory(cfg):
            self.built.append(cfg.id)
            return self.fake(cfg)

        self.controller = AppController(self.store, self.vault, service_factory=factory)
        for name in ("start_view", "stop_view", "refresh_status", "load_presets", "load_recordings", "ptz_move"):
            self._spy(name)

    def _spy(self, name):
        real = getattr(self.controller, name)

        def wrapper(*args, **kwargs):
            self.calls.append((name, *args))
            return real(*args, **kwargs)

        setattr(self.controller, name, wrapper)

    def called(self, name):
        return [c for c in self.calls if c[0] == name]

    def close(self):
        self.controller.stop_all()
        self.fake.shutdown()


@pytest.fixture
def make_env(tmp_path, qtbot):
    envs = []

    def _make(cams):
        env = Env(tmp_path, list(cams))
        envs.append(env)
        return env

    yield _make
    for env in envs:
        env.close()


class FakeImouManager(QObject):
    """The surface the dialog and the main window use; records calls, never touches a network."""

    connected = pyqtSignal(str)
    connect_failed = pyqtSignal(str)
    disconnected = pyqtSignal()
    cameras_changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.calls: list[tuple] = []
        self.blocked: float | None = None
        self.on = False
        self.limits_saved = (20, 2)

    def test(self, app_id, secret, dc):
        self.calls.append(("test", app_id, secret, dc))

    def refresh(self):
        self.calls.append(("refresh",))

    def disconnect(self):
        self.calls.append(("disconnect",))

    def restore(self):
        self.calls.append(("restore",))

    def set_limits(self, daily_minutes, max_live):
        self.limits_saved = (daily_minutes, max_live)
        self.calls.append(("set_limits", daily_minutes, max_live))

    def daily_minutes(self):
        return self.limits_saved[0]

    def max_live(self):
        return self.limits_saved[1]

    def is_connected(self):
        return self.on

    def account_label(self):
        return "lcd***" if self.on else ""

    def blocked_until(self):
        return self.blocked

    def data_center(self):
        return "sg"

    def usage_minutes_today(self):
        return 12.5


def make_window(qtbot, env, **kw):
    from tapo_desktop.ui.main_window import MainWindow

    window = MainWindow(env.controller, tray_enabled=False, **kw)
    qtbot.addWidget(window)
    window.resize(1100, 750)
    window.show()
    qtbot.waitExposed(window)
    return window


def grid_of(window):
    return window.findChild(QObject, "cameraGrid")


def rows(window):
    lst = window.findChild(QObject, "cameraList")
    return [(lst.item(i).text(), lst.item(i).toolTip()) for i in range(lst.count())]


# ============================================================================== dialog
class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


def make_dialog(qtbot, manager, **kw):
    from tapo_desktop.ui.imou_dialog import ImouConnectDialog

    dlg = ImouConnectDialog(manager, clock=kw.pop("clock", Clock()), **kw)
    qtbot.addWidget(dlg)
    return dlg


def child(dlg, cls, name):
    w = dlg.findChild(cls, name)
    assert w is not None, name
    return w


def fill(dlg, app_id="lcdMOCKAPPID000001", secret=SECRET, dc="fk"):
    child(dlg, QLineEdit, "imouAppIdEdit").setText(app_id)
    child(dlg, QLineEdit, "imouSecretEdit").setText(secret)
    combo = child(dlg, QComboBox, "imouDataCenterCombo")
    combo.setCurrentIndex(combo.findData(dc))


def test_dialog_fields_are_a_fixed_dropdown_and_a_masked_secret(qtbot):
    dlg = make_dialog(qtbot, FakeImouManager())
    combo = child(dlg, QComboBox, "imouDataCenterCombo")
    assert not combo.isEditable()
    assert [combo.itemData(i) for i in range(combo.count())] == ["sg", "fk", "or"]
    assert child(dlg, QLineEdit, "imouSecretEdit").echoMode() == QLineEdit.EchoMode.Password
    assert child(dlg, QSpinBox, "imouDailyMinutesSpin").value() == 20
    assert child(dlg, QSpinBox, "imouMaxLiveSpin").value() == 2
    note = child(dlg, QLabel, "imouNoteLabel").text().lower()
    assert "quota" in note and "my resources" in note and "own" in note


def test_dialog_shows_the_local_minute_estimate_and_points_to_my_resources(qtbot):
    dlg = make_dialog(qtbot, FakeImouManager())
    text = child(dlg, QLabel, "imouUsageLabel").text()
    assert "12.5" in text and "estimate" in text.lower() and "My Resources" in text


def test_dialog_preselects_the_saved_data_center(qtbot):
    dlg = make_dialog(qtbot, FakeImouManager(), last_data_center="or")
    assert child(dlg, QComboBox, "imouDataCenterCombo").currentData() == "or"


def test_one_click_sends_exactly_one_test_and_saves_the_limits(qtbot):
    manager = FakeImouManager()
    dlg = make_dialog(qtbot, manager)
    fill(dlg)
    child(dlg, QSpinBox, "imouDailyMinutesSpin").setValue(30)
    child(dlg, QSpinBox, "imouMaxLiveSpin").setValue(3)
    button = child(dlg, QPushButton, "imouTestButton")
    button.click()
    assert [c for c in manager.calls if c[0] == "test"] == [("test", "lcdMOCKAPPID000001", SECRET, "fk")]
    assert ("set_limits", 30, 3) in manager.calls
    assert not button.isEnabled()
    button.click()
    child(dlg, QLineEdit, "imouSecretEdit").returnPressed.emit()
    assert len([c for c in manager.calls if c[0] == "test"]) == 1


def test_empty_fields_send_nothing(qtbot):
    manager = FakeImouManager()
    dlg = make_dialog(qtbot, manager)
    child(dlg, QPushButton, "imouTestButton").click()
    assert manager.calls == []
    assert child(dlg, QLabel, "imouErrorLabel").isVisibleTo(dlg)


def test_failure_shows_the_text_clears_the_secret_and_allows_one_more_click(qtbot):
    manager = FakeImouManager()
    dlg = make_dialog(qtbot, manager)
    fill(dlg)
    button = child(dlg, QPushButton, "imouTestButton")
    button.click()
    manager.connect_failed.emit("iMou: check the App ID and App secret. (SN1001)")
    err = child(dlg, QLabel, "imouErrorLabel")
    assert err.isVisibleTo(dlg) and "App ID" in err.text()
    assert child(dlg, QLineEdit, "imouSecretEdit").text() == ""
    assert child(dlg, QLineEdit, "imouAppIdEdit").text() == "lcdMOCKAPPID000001"
    assert button.isEnabled()
    fill(dlg)
    button.click()
    assert len([c for c in manager.calls if c[0] == "test"]) == 2


def test_success_clears_the_secret_and_closes(qtbot):
    manager = FakeImouManager()
    dlg = make_dialog(qtbot, manager)
    fill(dlg)
    child(dlg, QPushButton, "imouTestButton").click()
    manager.connected.emit("lcd***")
    assert child(dlg, QLineEdit, "imouSecretEdit").text() == ""
    assert dlg.result() == dlg.DialogCode.Accepted.value


def test_a_block_timer_disables_the_button_and_shows_the_seconds(qtbot):
    manager, clock = FakeImouManager(), Clock()
    manager.blocked = clock.now + 90
    dlg = make_dialog(qtbot, manager, clock=clock)
    button = child(dlg, QPushButton, "imouTestButton")
    assert not button.isEnabled() and "90" in button.text()
    clock.now += 91
    manager.blocked = None
    dlg.refresh_block_state()
    assert button.isEnabled() and button.text() == "Test"


# ============================================================================== account menu
def imou_actions(window):
    menu = window.findChild(QObject, "accountMenu")
    return menu, {a.objectName(): a for a in menu.actions() if a.objectName()}


def test_menu_without_an_imou_manager_is_exactly_as_before(make_env, qtbot):
    env = make_env([lan_cfg(0)])
    window = make_window(qtbot, env)
    menu, _ = imou_actions(window)
    assert [a.text() for a in menu.actions()] == ["Tapo", "Sign in\u2026", "Refresh cameras", "Sign out", "Kami",
                                                  "Kami account\u2026", "Sign out of Kami"]


def test_menu_gets_one_imou_section_after_the_existing_ones(make_env, qtbot):
    env = make_env([lan_cfg(0)])
    window = make_window(qtbot, env, imou_accounts=FakeImouManager())
    menu, by = imou_actions(window)
    assert [a.text() for a in menu.actions()] == [
        "Tapo", "Sign in\u2026", "Refresh cameras", "Sign out", "Kami", "Kami account\u2026", "Sign out of Kami",
        "iMou (Open Platform)", "iMou connection\u2026", "Refresh iMou cameras", "Disconnect iMou"]
    assert {"imouConnectAction", "imouRefreshAction", "imouDisconnectAction"} <= set(by)


def test_imou_actions_follow_the_connected_state(make_env, qtbot):
    env = make_env([lan_cfg(0)])
    manager = FakeImouManager()
    window = make_window(qtbot, env, imou_accounts=manager)
    _, by = imou_actions(window)
    assert by["imouConnectAction"].isEnabled()
    assert not by["imouRefreshAction"].isEnabled() and not by["imouDisconnectAction"].isEnabled()
    manager.on = True
    manager.connected.emit("lcd***")
    assert by["imouRefreshAction"].isEnabled() and by["imouDisconnectAction"].isEnabled()
    by["imouRefreshAction"].trigger()
    by["imouDisconnectAction"].trigger()
    assert manager.calls == [("refresh",), ("disconnect",)]
    manager.on = False
    manager.disconnected.emit()
    assert not by["imouRefreshAction"].isEnabled()


def test_a_failure_with_no_dialog_open_reaches_the_status_bar(make_env, qtbot):
    env = make_env([lan_cfg(0)])
    manager = FakeImouManager()
    window = make_window(qtbot, env, imou_accounts=manager)
    manager.connect_failed.emit("iMou: free quota used. iMou calls stop until tomorrow (UTC).")
    assert "free quota used" in window.statusBar().currentMessage()


# ============================================================================== tiles: click to start
def test_imou_tiles_start_by_themselves_in_view(make_env, qtbot):
    # requirement change: visible iMou tiles start through the normal view plan; nothing says "click to start" any more
    env = make_env([imou_cfg("A", "iMou A"), imou_cfg("B", "iMou B")])
    window = make_window(qtbot, env)
    qtbot.waitUntil(lambda: len(env.called("start_view")) == 2, timeout=3000)
    assert sorted(c[1:] for c in env.called("start_view")) == [
        ("imou:A", StreamQuality.SD, 640, 360), ("imou:B", StreamQuality.SD, 640, 360)]
    grid = grid_of(window)
    for cid in ("imou:A", "imou:B"):
        assert grid.tile(cid).status_text() != CLICK_HEADLINE


def test_a_click_on_a_running_imou_tile_does_not_restart_it(make_env, qtbot):
    # requirement change: the plan starts the stream, a click on a connecting or playing tile asks for nothing more
    env = make_env([imou_cfg("A", "iMou A")])
    window = make_window(qtbot, env)
    grid = grid_of(window)
    qtbot.waitUntil(lambda: len(env.called("start_view")) == 1, timeout=3000)
    assert env.called("start_view") == [("start_view", "imou:A", StreamQuality.SD, 640, 360)]
    grid._on_tile_clicked("imou:A")
    grid._on_tile_clicked("imou:A")
    assert len(env.called("start_view")) == 1


def test_full_screen_asks_for_hd_and_leaving_it_goes_back_to_sd(make_env, qtbot):
    env = make_env([imou_cfg("A", "iMou A")])
    window = make_window(qtbot, env)
    grid = grid_of(window)
    grid._on_tile_clicked("imou:A")
    grid.toggle_fullscreen("imou:A")
    assert env.called("start_view")[-1][1:] == ("imou:A", StreamQuality.HD, 1280, 720)
    grid.toggle_fullscreen("imou:A")
    assert env.called("start_view")[-1][1:] == ("imou:A", StreamQuality.SD, 640, 360)


def test_full_screen_from_the_menu_of_an_imou_tile_asks_for_hd_after_the_sd_start(make_env, qtbot):
    # requirement change: the tile is already streaming in SD from the plan, so full screen is the second call
    env = make_env([imou_cfg("A", "iMou A")])
    window = make_window(qtbot, env)
    grid = grid_of(window)
    qtbot.waitUntil(lambda: len(env.called("start_view")) == 1, timeout=3000)
    grid.toggle_fullscreen("imou:A")
    assert [c[1:] for c in env.called("start_view")] == [
        ("imou:A", StreamQuality.SD, 640, 360), ("imou:A", StreamQuality.HD, 1280, 720)]


def test_a_failed_stream_waits_and_the_next_click_starts_it_through_the_controller(make_env, qtbot):
    # requirement change: the view is not stopped after a failure (the service retries or waits); a click asks the
    # controller for an immediate start (view_remote_now, which passes preempt=True)
    env = make_env([imou_cfg("A", "iMou A")])
    window = make_window(qtbot, env)
    grid = grid_of(window)
    qtbot.waitUntil(lambda: len(env.called("start_view")) == 1, timeout=3000)
    asked = []
    real = env.controller.view_remote_now
    env.controller.view_remote_now = lambda cid: (asked.append(cid), real(cid))[1]
    env.controller.stream_state.emit("imou:A", "FAILED", "iMou: the camera is offline (DV1007).")
    tile = grid.tile("imou:A")
    assert env.called("stop_view") == [] and "offline" in tile.toolTip()
    grid._on_tile_clicked("imou:A")
    assert asked == ["imou:A"] and tile.stream_state() == "CONNECTING"


def test_an_imou_tile_that_leaves_the_view_stops_and_starts_again_when_it_comes_back(make_env, qtbot):
    # requirement change: coming back into the view starts the stream again (it is a visible tile like any other)
    env = make_env([imou_cfg("A", "iMou A"), lan_cfg(0)])
    window = make_window(qtbot, env)
    grid = grid_of(window)
    qtbot.waitUntil(lambda: len(env.called("start_view")) == 2, timeout=3000)
    grid.remove_from_view("imou:A")
    assert ("stop_view", "imou:A") in env.called("stop_view")
    n = len(env.called("start_view"))
    grid.add_to_view("imou:A")
    qtbot.waitUntil(lambda: len(env.called("start_view")) == n + 1, timeout=3000)
    assert env.called("start_view")[-1][1] == "imou:A"


def test_tapo_tiles_still_start_by_themselves_next_to_an_imou_tile(make_env, qtbot):
    env = make_env([lan_cfg(0), imou_cfg("A", "iMou A"), lan_cfg(1)])
    window = make_window(qtbot, env)
    qtbot.waitUntil(lambda: len(env.called("start_view")) >= 2, timeout=3000)
    qtbot.wait(200)
    assert sorted(c[1] for c in env.called("start_view")) == ["lan0", "lan1"]
    assert {c[2:] for c in env.called("start_view")} == {(StreamQuality.SD, 640, 360)}
    assert "imou:A" not in env.built


def test_plan_streams_for_a_tapo_only_config_is_unchanged():
    cams = {c.id: c for c in (lan_cfg(0), lan_cfg(1), lan_cfg(2))}
    cams["lan1"].grid_quality = StreamQuality.SD
    cams["lan2"].fullscreen_quality = StreamQuality.HD
    visible = ["lan0", None, "lan1", "lan0", "ghost", "lan2"]
    plan = {k: v.as_args() for k, v in plan_streams(visible, cams).items()}
    assert plan == {"lan0": (cams["lan0"].grid_quality, 640, 360), "lan1": (StreamQuality.SD, 640, 360),
                    "lan2": (cams["lan2"].grid_quality, 640, 360)}
    full = {k: v.as_args() for k, v in plan_streams(visible, cams, "lan2").items()}
    assert full == {"lan2": (StreamQuality.HD, 1280, 720)}
    assert list(plan) == ["lan0", "lan1", "lan2"]


def test_tapo_only_grid_and_list_golden(make_env, qtbot):
    env = make_env([lan_cfg(0), lan_cfg(1)])
    window = make_window(qtbot, env)
    assert rows(window) == [("Hall 0", "C200 - 192.0.2.10"), ("Hall 1", "C200 - 192.0.2.11")]
    qtbot.waitUntil(lambda: len(env.called("start_view")) == 2, timeout=3000)
    assert {c[2:] for c in env.called("start_view")} == {(lan_cfg(0).grid_quality, 640, 360)}


def test_tile_headlines_of_tapo_and_the_unsupported_text_are_unchanged():
    from tapo_desktop.ui.video_tile import OVERLAY_TEXT, REASON_HEADLINES, VideoTile

    assert OVERLAY_TEXT["FAILED"] == "Offline" and OVERLAY_TEXT["CONNECTING"] == "Connecting..."
    assert REASON_HEADLINES["Remote - click to view"] == "Remote - click to view"
    tile = VideoTile("x", "x")
    tile.set_stream_state("FAILED", "ffmpeg exited (code 1)")
    assert tile.status_text() == "Offline"
    tile.set_stream_state("FAILED", "Live view is not supported for this Kami camera")
    assert tile.status_text() == "Live view is not supported for this Kami camera"
    tile.set_stream_state("FAILED", "iMou: the camera is offline (DV1007).")
    assert tile.status_text() == RETRY_HEADLINE


# ============================================================================== list, controls, playback
def test_imou_rows_have_their_own_tooltip_and_status(make_env, qtbot):
    env = make_env([imou_cfg("A", "iMou A", True), imou_cfg("B", "iMou B", False)])
    window = make_window(qtbot, env)
    got = dict(rows(window))
    assert "iMou A" in got and "iMou B (Offline)" in got
    assert got["iMou A"].splitlines()[0] == "iMou (Open Platform), Online"
    assert got["iMou B (Offline)"].splitlines()[0] == "iMou (Open Platform), Offline"
    assert "Not on this network" not in " ".join(got) and "No address" not in " ".join(got.values())


def test_camera_row_status_of_other_cameras_is_unchanged():
    from tapo_desktop.ui.main_window import camera_row_status

    assert camera_row_status(lan_cfg(0)) == ""
    off = lan_cfg(1)
    off.online = False
    assert camera_row_status(off) == "Offline"
    cloud = CameraConfig(name="R", host="", id="R1", source="cloud", online=True, lan_found=False)
    assert camera_row_status(cloud) == "Not on this network" and camera_row_status(cloud, remote=True) == "Remote"
    assert camera_row_status(imou_cfg()) == "" and camera_row_status(imou_cfg(online=False)) == "Offline"


def test_capability_service_dispatch():
    from tapo_desktop.services.imou_camera_service import ImouCameraService
    from tapo_desktop.services.kami_camera_service import KamiCameraService
    from tapo_desktop.ui import kami_status, vendor_status

    kami = CameraConfig(name="K", host="", id="kami:u", source="kami")
    assert vendor_status.capability_service(imou_cfg()) is ImouCameraService
    assert vendor_status.capability_service(kami) is KamiCameraService
    assert vendor_status.capability_service(lan_cfg(0)) is None
    assert kami_status.capability_service(imou_cfg()) is None  # the existing helper is untouched
    assert kami_status.control_enabled(vendor_status.capability_service(imou_cfg()), "live_view") is True
    assert kami_status.control_enabled(vendor_status.capability_service(imou_cfg()), "ptz") is False


def test_imou_selection_disables_controls_and_calls_no_service(make_env, qtbot):
    env = make_env([lan_cfg(0), imou_cfg("A", "iMou A")])
    window = make_window(qtbot, env)
    panel = window.findChild(QObject, "controlsPanel")
    playback = window.findChild(QObject, "playbackPanel")
    env.calls.clear()
    window._select("lan0", force=True)
    qtbot.wait(100)
    assert [c[1] for c in env.called("refresh_status")] == ["lan0"]
    assert all(g.isEnabled() for g in panel.findChildren(QGroupBox))
    env.calls.clear()
    window._select("imou:A", force=True)
    qtbot.wait(100)
    assert env.called("refresh_status") == [] and env.called("load_presets") == []
    assert env.called("load_recordings") == []
    assert not any(g.isEnabled() for g in panel.findChildren(QGroupBox))
    assert not playback.findChild(QObject, "playButton").isEnabled()
    assert not playback.findChild(QObject, "downloadButton").isEnabled()
    # requirement change: iMou recordings are built; the panel shows the Refresh hint, not "not supported"
    text = playback.findChild(QObject, "downloadStatus").text().lower()
    assert "press refresh" in text and "imou" in text and "kami" not in text and "not supported" not in text
    window._select("lan0", force=True)
    qtbot.wait(100)
    assert all(g.isEnabled() for g in panel.findChildren(QGroupBox))


def test_keyboard_ptz_is_ignored_for_an_imou_camera(make_env, qtbot):
    from PyQt6.QtGui import QKeyEvent

    env = make_env([imou_cfg("A", "iMou A"), lan_cfg(0)])
    window = make_window(qtbot, env)
    window._select("imou:A", force=True)
    window.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_W, Qt.KeyboardModifier.NoModifier))
    window.keyReleaseEvent(QKeyEvent(QKeyEvent.Type.KeyRelease, Qt.Key.Key_W, Qt.KeyboardModifier.NoModifier))
    assert env.called("ptz_move") == []


def test_edit_of_an_imou_camera_does_not_open_the_tapo_camera_dialog(make_env, qtbot, monkeypatch):
    from tapo_desktop.ui import camera_dialog

    opened = []
    monkeypatch.setattr(camera_dialog.CameraDialog, "exec", lambda self: opened.append(1) or 0)
    env = make_env([imou_cfg("A", "iMou A")])
    window = make_window(qtbot, env)
    window._select("imou:A", force=True)
    window._edit_camera()
    assert opened == []
    assert "iMou" in window.statusBar().currentMessage()


# ============================================================================== wiring
def test_factory_builds_the_imou_service_for_imou_and_leaves_the_others(tmp_path):
    from tapo_desktop.services.camera_service import TapoCameraService
    from tapo_desktop.services.controller import AppController
    from tapo_desktop.services.imou_camera_service import ImouCameraService
    from tapo_desktop.services.kami_camera_service import KamiCameraService

    controller = AppController(ConfigStore(tmp_path / "c.json"), FakeVault())
    kami = CameraConfig(name="K", host="", id="kami:u", source="kami")
    assert isinstance(controller._default_factory(imou_cfg()), ImouCameraService)
    assert isinstance(controller._default_factory(kami), KamiCameraService)
    assert isinstance(controller._default_factory(lan_cfg(0)), TapoCameraService)


def test_the_vault_kind_and_id_exist_and_are_not_the_others():
    from tapo_desktop import models

    assert models.CredentialKind.IMOU_APP.value == "imou_app"
    assert models.IMOU_APP_VAULT_ID == "__imou_app__"
    assert len({models.ACCOUNT_VAULT_ID, models.KAMI_ACCOUNT_VAULT_ID, models.IMOU_APP_VAULT_ID}) == 3


def test_a_config_with_tapo_and_other_cameras_round_trips_unchanged_without_imou(tmp_path):
    store = ConfigStore(tmp_path / "c.json")
    kami = CameraConfig(name="K", host="", id="kami:u", source="kami", vendor_data={"uid": "u"})
    store.save([lan_cfg(0), kami])
    before = store.path.read_text(encoding="utf-8")
    store.save(store.load())
    assert store.path.read_text(encoding="utf-8") == before
    assert "imou" not in before.lower()


def test_build_creates_the_imou_manager_and_start_account_restores_without_a_network_call(tmp_path, monkeypatch, qtbot):
    from tapo_desktop import app as app_mod
    from tapo_desktop.services.imou_account import ImouAccountManager

    monkeypatch.setenv("TAPO_DESKTOP_HOME", str(tmp_path))
    qapp = QApplication.instance()
    window, controller, accounts = app_mod._build(qapp, ConfigStore(tmp_path / "config.json"), tmp_path,
                                                  interactive=False)
    try:
        assert isinstance(window.imou_accounts, ImouAccountManager)
        assert window.imou_accounts is not None
    finally:
        window.deleteLater()
        controller.stop_all()


def test_start_account_restores_the_imou_connection_once_and_a_broken_restore_does_not_stop_the_app(tmp_path):
    from tapo_desktop import app as app_mod

    class Boom(FakeImouManager):
        def restore(self):
            raise RuntimeError("vault broken")

    class TapoAccounts:
        config_store = ConfigStore(tmp_path / "c.json")

        def restore_session(self):
            return True

    class W:
        imou_accounts = FakeImouManager()
        kami_accounts = None

    app_mod._start_account(W, TapoAccounts())
    assert W.imou_accounts.calls == [("restore",)]
    W.imou_accounts = Boom()
    app_mod._start_account(W, TapoAccounts())  # must not raise


def test_selftest_with_an_imou_camera_in_the_config_starts_no_stream_and_no_call(tmp_path, monkeypatch):
    from tapo_desktop import app as app_mod

    monkeypatch.setenv("TAPO_DESKTOP_HOME", str(tmp_path))
    ConfigStore(tmp_path / "config.json").save([imou_cfg("A", "iMou A")])
    assert app_mod.main(["--selftest"]) == 0


# ============================================================================== files
def test_new_and_edited_files_are_lf_without_bom():
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    rels = [*(str(p.relative_to(root)) for p in (root / "src" / "tapo_desktop").rglob("*imou*.py")),
            *(str(p.relative_to(root)) for p in (root / "src" / "tapo_desktop" / "core" / "imou").glob("*.py")),
            "src/tapo_desktop/ui/vendor_status.py", "src/tapo_desktop/app.py", "src/tapo_desktop/models.py",
            "src/tapo_desktop/ui/main_window.py", "src/tapo_desktop/ui/grid.py", "src/tapo_desktop/ui/video_tile.py",
            "src/tapo_desktop/ui/controls_panel.py", "src/tapo_desktop/ui/playback_panel.py",
            "src/tapo_desktop/services/controller.py"]
    for rel in rels:
        data = (root / rel).read_bytes()
        assert not data.startswith(b"\xef\xbb\xbf"), rel
        assert b"\r\n" not in data, rel
