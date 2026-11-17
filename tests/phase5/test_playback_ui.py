"""PlayerWindow (playback screen like the Tapo app) and the PlaybackPanel entry points (playButton,
double-click), against AppController + FakePlaybackService."""
from __future__ import annotations

from datetime import datetime

import pytest
from PyQt6.QtCore import QDate, QPoint, Qt
from PyQt6.QtWidgets import QAbstractButton, QComboBox, QDateEdit, QLabel, QListWidget, QWidget

from tests.e2e.fakes import CAMERA, CLOUD, RECORDING_DAY, FakeVault, default_recordings, make_config
from tests.phase5._fakes import FakePlaybackFactory

pytestmark = pytest.mark.ui

CLIPS = default_recordings()[RECORDING_DAY]


@pytest.fixture
def ctrl(tmp_path, qtbot):
    from tapo_desktop.models import CredentialKind
    from tapo_desktop.services.config_store import ConfigStore
    from tapo_desktop.services.controller import AppController

    factory = FakePlaybackFactory()
    store = ConfigStore(tmp_path / "config.json")
    store.save([make_config(0)])
    vault = FakeVault()
    vault.set("cam0", CredentialKind.CLOUD, CLOUD)
    vault.set("cam0", CredentialKind.CAMERA_ACCOUNT, CAMERA)
    controller = AppController(store, vault, service_factory=factory)
    controller.factory = factory  # type: ignore[attr-defined]
    yield controller
    controller.stop_all()
    factory.shutdown()


def _svc(ctrl):
    return ctrl.factory.services["cam0"]


def _calls(ctrl, name):
    return _svc(ctrl).calls_to(name) if "cam0" in ctrl.factory.services else []


def _panel(ctrl, qtbot):
    from tapo_desktop.ui.playback_panel import PlaybackPanel

    panel = PlaybackPanel(ctrl)
    qtbot.addWidget(panel)
    panel.show()
    panel.set_camera("cam0")
    panel.findChild(QDateEdit, "recordingDate").setDate(QDate(RECORDING_DAY.year, RECORDING_DAY.month,
                                                              RECORDING_DAY.day))
    lst = panel.findChild(QListWidget, "recordingsList")
    qtbot.waitUntil(lambda: lst.count() == 2, timeout=3000)
    return panel, lst


def _window(ctrl, qtbot, clip_index=0):
    from tapo_desktop.ui.player_window import PlayerWindow

    w = PlayerWindow(ctrl, "cam0", day=RECORDING_DAY, clips=CLIPS, start=CLIPS[clip_index].start)
    qtbot.addWidget(w)
    w.resize(900, 600)
    w.show()
    qtbot.waitExposed(w)
    qtbot.waitUntil(lambda: bool(_calls(ctrl, "start_playback")), timeout=3000)
    return w


def _child(w, cls, name):
    child = w.findChild(cls, name)
    assert child is not None, name
    return child


# ---------------------------------------------------------------------------------- panel entry
def test_play_button_opens_one_window_per_camera(ctrl, qtbot):
    panel, lst = _panel(ctrl, qtbot)
    play = _child(panel, QAbstractButton, "playButton")
    assert not play.isEnabled()
    lst.setCurrentRow(0)
    assert play.isEnabled()
    play.click()
    qtbot.waitUntil(lambda: panel.player_window("cam0") is not None, timeout=3000)
    w = panel.player_window("cam0")
    qtbot.addWidget(w)
    assert w.objectName() == "playerWindow_cam0" and w.isVisible() and w.isWindow()
    assert not w.isModal()
    qtbot.waitUntil(lambda: bool(_calls(ctrl, "start_playback")), timeout=3000)
    assert _calls(ctrl, "start_playback")[0].args[0] == CLIPS[0].start.timestamp()
    item = lst.item(1)
    rect = lst.visualItemRect(item)
    qtbot.mouseDClick(lst.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    assert panel.player_window("cam0") is w
    target = CLIPS[1].start.timestamp()
    qtbot.waitUntil(lambda: any(c.args[0] == target for c in _calls(ctrl, "seek_playback"))
                    or any(c.args[0] == target for c in _calls(ctrl, "start_playback")), timeout=3000)
    w.close()
    qtbot.waitUntil(lambda: panel.player_window("cam0") is None, timeout=3000)


# ---------------------------------------------------------------------------------- window
def test_window_widgets_frames_and_time_label(ctrl, qtbot):
    w = _window(ctrl, qtbot)
    for cls, name in ((QWidget, "playbackTimeline"), (QAbstractButton, "playPauseButton"),
                      (QComboBox, "speedCombo"), (QAbstractButton, "prevClipButton"),
                      (QAbstractButton, "nextClipButton"), (QAbstractButton, "snapshotButton"),
                      (QAbstractButton, "downloadClipButton"), (QLabel, "playbackTimeLabel"),
                      (QDateEdit, "playbackDate"), (QWidget, "playbackVideo")):
        _child(w, cls, name)
    qtbot.waitUntil(lambda: w.current_image() is not None, timeout=3000)
    img = w.current_image()
    assert img.width() * 9 == img.height() * 16
    label = _child(w, QLabel, "playbackTimeLabel")
    qtbot.waitUntil(lambda: w.position() is not None, timeout=3000)
    shown = datetime.strptime(label.text(), "%H:%M:%S").time()
    local = datetime.fromtimestamp(w.position()).time()
    assert abs((shown.hour * 3600 + shown.minute * 60 + shown.second)
               - (local.hour * 3600 + local.minute * 60 + local.second)) <= 2
    timeline = _child(w, QWidget, "playbackTimeline")
    assert timeline.segments() == [(c.start.timestamp(), c.end.timestamp()) for c in CLIPS]
    qtbot.waitUntil(lambda: timeline.position() is not None, timeout=3000)
    assert timeline.grab().width() > 0


def test_timeline_click_seeks(ctrl, qtbot):
    w = _window(ctrl, qtbot)
    timeline = _child(w, QWidget, "playbackTimeline")
    target = CLIPS[1].start.timestamp() + 20
    x = timeline.x_for(target)
    assert 0 < x < timeline.width()
    per_px = 86400 / max(1, timeline.width() - 2 * timeline.margin())
    assert abs(timeline.utc_at(x) - target) <= per_px
    qtbot.mouseClick(timeline, Qt.MouseButton.LeftButton, pos=QPoint(int(round(x)), timeline.height() // 2))
    qtbot.waitUntil(lambda: bool(_calls(ctrl, "seek_playback")), timeout=3000)
    assert abs(_calls(ctrl, "seek_playback")[-1].args[0] - target) <= per_px + 1


def test_timeline_drag_seeks_on_release(ctrl, qtbot):
    w = _window(ctrl, qtbot)
    timeline = _child(w, QWidget, "playbackTimeline")
    y = timeline.height() // 2
    x0, x1 = timeline.x_for(CLIPS[0].start.timestamp()), timeline.x_for(CLIPS[1].start.timestamp() + 30)
    qtbot.mousePress(timeline, Qt.MouseButton.LeftButton, pos=QPoint(int(x0), y))
    qtbot.mouseMove(timeline, QPoint(int((x0 + x1) / 2), y))
    qtbot.mouseMove(timeline, QPoint(int(x1), y))
    qtbot.mouseRelease(timeline, Qt.MouseButton.LeftButton, pos=QPoint(int(x1), y))
    qtbot.waitUntil(lambda: bool(_calls(ctrl, "seek_playback")), timeout=3000)
    assert len(_calls(ctrl, "seek_playback")) == 1
    assert abs(_calls(ctrl, "seek_playback")[-1].args[0] - timeline.utc_at(int(x1))) <= 1


def test_play_pause_toggles(ctrl, qtbot):
    w = _window(ctrl, qtbot)
    button = _child(w, QAbstractButton, "playPauseButton")
    qtbot.waitUntil(lambda: w.playback_state() == "PLAYING", timeout=3000)
    assert button.text() == "Pause"
    button.click()
    qtbot.waitUntil(lambda: bool(_calls(ctrl, "pause_playback")), timeout=3000)
    assert button.text() == "Play"
    button.click()
    qtbot.waitUntil(lambda: bool(_calls(ctrl, "resume_playback")), timeout=3000)
    assert button.text() == "Pause"


def test_speed_and_clip_navigation(ctrl, qtbot):
    w = _window(ctrl, qtbot)
    combo = _child(w, QComboBox, "speedCombo")
    assert [combo.itemData(i) for i in range(combo.count())] == ["1/1", "2/1", "4/1", "8/1"]
    combo.setCurrentIndex(2)
    qtbot.waitUntil(lambda: bool(_calls(ctrl, "set_playback_speed")), timeout=3000)
    assert _calls(ctrl, "set_playback_speed")[-1].args == ("4/1",)
    qtbot.waitUntil(lambda: w.position() is not None, timeout=3000)
    _child(w, QAbstractButton, "nextClipButton").click()
    qtbot.waitUntil(lambda: bool(_calls(ctrl, "seek_playback")), timeout=3000)
    assert _calls(ctrl, "seek_playback")[-1].args[0] == CLIPS[1].start.timestamp()
    qtbot.waitUntil(lambda: w.position() is not None and w.position() >= CLIPS[1].start.timestamp(), timeout=3000)
    _child(w, QAbstractButton, "prevClipButton").click()
    qtbot.waitUntil(lambda: len(_calls(ctrl, "seek_playback")) == 2, timeout=3000)
    assert _calls(ctrl, "seek_playback")[-1].args[0] in (CLIPS[0].start.timestamp(), CLIPS[1].start.timestamp())


def test_date_change_reloads_clips(ctrl, qtbot):
    w = _window(ctrl, qtbot)
    date_edit = _child(w, QDateEdit, "playbackDate")
    assert date_edit.date().toPyDate() == RECORDING_DAY
    date_edit.setDate(QDate(2024, 3, 10))
    qtbot.waitUntil(lambda: any(c.args[0].isoformat() == "2024-03-10" for c in _calls(ctrl, "recordings_for_day")),
                    timeout=3000)
    timeline = _child(w, QWidget, "playbackTimeline")
    qtbot.waitUntil(lambda: timeline.segments() == [], timeout=3000)
    assert _calls(ctrl, "stop_playback")
    date_edit.setDate(QDate(RECORDING_DAY.year, RECORDING_DAY.month, RECORDING_DAY.day))
    qtbot.waitUntil(lambda: len(timeline.segments()) == 2, timeout=3000)
    qtbot.waitUntil(lambda: len(_calls(ctrl, "start_playback")) >= 2, timeout=3000)
    assert _calls(ctrl, "start_playback")[-1].args[0] == CLIPS[0].start.timestamp()


def test_close_stops_playback(ctrl, qtbot):
    w = _window(ctrl, qtbot)
    w.close()
    qtbot.waitUntil(lambda: bool(_calls(ctrl, "stop_playback")), timeout=3000)


def test_snapshot_and_download(ctrl, qtbot, tmp_path):
    w = _window(ctrl, qtbot)
    qtbot.waitUntil(lambda: w.current_image() is not None, timeout=3000)
    w.snapshot_folder = tmp_path / "snaps"
    _child(w, QAbstractButton, "snapshotButton").click()
    qtbot.waitUntil(lambda: (tmp_path / "snaps").exists() and any((tmp_path / "snaps").iterdir()), timeout=3000)
    out = tmp_path / "clip.mp4"
    asked = []
    w.choose_save_path = lambda clip: (asked.append(clip), out)[1]
    _child(w, QAbstractButton, "downloadClipButton").click()
    qtbot.waitUntil(lambda: bool(_calls(ctrl, "download_recording")), timeout=3000)
    assert asked == [CLIPS[0]] and _calls(ctrl, "download_recording")[0].args == (CLIPS[0], out)


def test_failed_state_is_shown(ctrl, qtbot):
    w = _window(ctrl, qtbot)
    ctrl.playback_state.emit("cam0", "FAILED", "The camera refused another video session.")
    status = _child(w, QLabel, "playbackStatus")
    assert "refused" in status.text()
    assert _child(w, QAbstractButton, "playPauseButton").text() == "Play"
