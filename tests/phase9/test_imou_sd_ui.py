"""iMou SD playback in the UI: the Playback tab reads the card only on Refresh, the player window is 1x with no download,
Tapo cameras are unchanged. Offscreen Qt, fake services, no network."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import datetime as dt  # noqa: E402

import pytest  # noqa: E402
from PyQt6.QtCore import QDate, QObject  # noqa: E402
from PyQt6.QtWidgets import QAbstractButton, QComboBox, QDateEdit, QLabel, QListWidget, QPushButton  # noqa: E402

from tapo_desktop.models import CameraConfig, RecordingClip  # noqa: E402
from tests.e2e.fakes import FakeVault, make_config  # noqa: E402
from tests.phase5._fakes import FakePlaybackFactory  # noqa: E402

pytestmark = pytest.mark.ui

DAY = dt.date(2026, 9, 19)
OTHER = dt.date(2026, 9, 18)
IMOU_ID = "imou:MOCKSERIAL0001"
HINT = "Press Refresh to read the recordings from the iMou camera"
NO_DOWNLOAD = "Downloads are not available for iMou cameras"


def clip(h, m=0, minutes=10):
    start = dt.datetime(DAY.year, DAY.month, DAY.day, h, m).astimezone(dt.timezone.utc)
    return RecordingClip(start=start, end=start + dt.timedelta(minutes=minutes), event_type=1)


CLIPS = [clip(8), clip(9, 30)]


def imou_cfg() -> CameraConfig:
    return CameraConfig(name="iMou A", host="", id=IMOU_ID, source="imou", device_id="MOCKSERIAL0001", model="IPC",
                        online=True, lan_found=False, enable_onvif_events=False,
                        vendor_data={"serial": "MOCKSERIAL0001", "channel": "0", "data_center": "sg"})


class FakeClock:
    now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def _fresh_cache():
    from tapo_desktop.ui import imou_status

    imou_status.IMOU_LIST_CACHE.clear()
    yield
    imou_status.IMOU_LIST_CACHE.clear()
    imou_status.IMOU_LIST_CACHE.clock = __import__("time").monotonic


@pytest.fixture
def ctrl(tmp_path, qtbot):
    from tapo_desktop.services.config_store import ConfigStore
    from tapo_desktop.services.controller import AppController

    factory = FakePlaybackFactory(recordings={DAY: list(CLIPS), OTHER: list(CLIPS[:1])})
    store = ConfigStore(tmp_path / "config.json")
    store.save([imou_cfg(), make_config(0)])
    controller = AppController(store, FakeVault(), service_factory=factory)
    controller.factory = factory  # type: ignore[attr-defined]
    yield controller
    controller.stop_all()
    factory.shutdown()


def loads(ctrl, cid=IMOU_ID):
    svc = ctrl.factory.services.get(cid)
    return svc.calls_to("recordings_for_day") if svc is not None else []


def make_panel(ctrl, qtbot):
    from tapo_desktop.ui.playback_panel import PlaybackPanel

    panel = PlaybackPanel(ctrl)
    qtbot.addWidget(panel)
    panel.resize(400, 500)
    panel.show()
    qtbot.waitExposed(panel)
    return panel


def child(panel, cls, name):
    w = panel.findChild(cls, name)
    assert w is not None, name
    return w


def pick_day(panel, day):
    child(panel, QDateEdit, "recordingDate").setDate(QDate(day.year, day.month, day.day))


def status(panel):
    return child(panel, QLabel, "downloadStatus").text()


def refresh(panel):
    child(panel, QPushButton, "refreshRecordingsButton").click()


# ================================================================================================ profile
def test_the_profile_exists_for_imou_only():
    from tapo_desktop.ui import vendor_status

    profile = vendor_status.playback_profile(imou_cfg())
    assert profile is not None and profile.on_demand_list and not profile.allow_download
    assert profile.speeds == ("1/1",) and profile.hint_text == HINT and profile.no_download_text == NO_DOWNLOAD
    assert vendor_status.playback_profile(make_config(0)) is None
    assert vendor_status.playback_profile(CameraConfig(name="K", host="", id="kami:u", source="kami")) is None
    assert vendor_status.playback_profile(None) is None


def test_the_playing_text_is_a_fixed_text_on_the_tile_without_the_retry_headline():
    from tapo_desktop.core.imou.texts import PLAYING_TEXT
    from tapo_desktop.ui import imou_status

    assert imou_status.headline_for(PLAYING_TEXT) is None


# ================================================================================================ the panel
def test_selecting_an_imou_camera_reads_nothing_and_shows_the_hint(ctrl, qtbot):
    panel = make_panel(ctrl, qtbot)
    panel.set_camera(IMOU_ID)
    pick_day(panel, DAY)
    pick_day(panel, OTHER)
    qtbot.wait(100)
    assert status(panel) == HINT and IMOU_ID not in ctrl.factory.services and loads(ctrl) == []
    assert child(panel, QPushButton, "refreshRecordingsButton").isEnabled()
    assert not child(panel, QPushButton, "downloadButton").isEnabled()
    assert child(panel, QPushButton, "downloadButton").toolTip() == NO_DOWNLOAD
    assert not child(panel, QPushButton, "playButton").isEnabled()


def test_refresh_reads_the_card_once_and_lists_the_clips(ctrl, qtbot):
    panel = make_panel(ctrl, qtbot)
    panel.set_camera(IMOU_ID)
    pick_day(panel, DAY)
    refresh(panel)
    lst = child(panel, QListWidget, "recordingsList")
    qtbot.waitUntil(lambda: lst.count() == 2, timeout=3000)
    assert len(loads(ctrl)) == 1 and "2 recording" in status(panel)
    lst.setCurrentRow(0)
    assert child(panel, QPushButton, "playButton").isEnabled()
    assert not child(panel, QPushButton, "downloadButton").isEnabled()


def test_the_note_of_the_list_is_added_to_the_status_line(ctrl, qtbot):
    from tapo_desktop.core.imou import notes

    notes.set(IMOU_ID, "iMou: the camera reports two clocks. Check the time of day against the picture.")
    try:
        panel = make_panel(ctrl, qtbot)
        panel.set_camera(IMOU_ID)
        pick_day(panel, DAY)
        refresh(panel)
        qtbot.waitUntil(lambda: "2 recording" in status(panel), timeout=3000)
        assert "two clocks" in status(panel)
    finally:
        notes.set(IMOU_ID, "")


def test_changing_the_day_or_the_camera_never_calls_but_shows_a_fresh_cached_list(ctrl, qtbot):
    from tapo_desktop.ui import imou_status

    clock = FakeClock()
    imou_status.IMOU_LIST_CACHE.clock = clock
    panel = make_panel(ctrl, qtbot)
    panel.set_camera(IMOU_ID)
    pick_day(panel, DAY)
    refresh(panel)
    lst = child(panel, QListWidget, "recordingsList")
    qtbot.waitUntil(lambda: lst.count() == 2, timeout=3000)
    pick_day(panel, OTHER)
    assert lst.count() == 0 and status(panel) == HINT
    pick_day(panel, DAY)
    assert lst.count() == 2 and len(loads(ctrl)) == 1
    panel.set_camera(make_config(0).id)
    panel.set_camera(IMOU_ID)
    assert lst.count() == 2 and len(loads(ctrl)) == 1
    clock.now += 61  # a minute later the card is asked again only by a Refresh
    pick_day(panel, OTHER)
    pick_day(panel, DAY)
    assert lst.count() == 0 and status(panel) == HINT and len(loads(ctrl)) == 1


def test_no_card_is_remembered_until_refresh(ctrl, qtbot):
    from tapo_desktop.core.imou.errors import ImouNoCardError

    ctrl.factory.fail(IMOU_ID, "recordings_for_day", ImouNoCardError("the camera has no memory card (or the card has an error) (DV1049)."), times=1)
    panel = make_panel(ctrl, qtbot)
    panel.set_camera(IMOU_ID)
    pick_day(panel, DAY)
    refresh(panel)
    qtbot.waitUntil(lambda: "memory card" in status(panel), timeout=3000)
    assert len(loads(ctrl)) == 1
    pick_day(panel, OTHER)
    assert "memory card" in status(panel) and len(loads(ctrl)) == 1  # no second question
    refresh(panel)
    lst = child(panel, QListWidget, "recordingsList")
    qtbot.waitUntil(lambda: lst.count() == 1, timeout=3000)
    assert len(loads(ctrl)) == 2


def test_an_empty_day_says_so(ctrl, qtbot):
    panel = make_panel(ctrl, qtbot)
    panel.set_camera(IMOU_ID)
    pick_day(panel, dt.date(2026, 1, 1))
    refresh(panel)
    qtbot.waitUntil(lambda: "No recordings" in status(panel), timeout=3000)


def test_a_tapo_camera_still_loads_at_once_and_keeps_its_download_button(ctrl, qtbot):
    panel = make_panel(ctrl, qtbot)
    panel.set_camera("cam0")
    pick_day(panel, DAY)
    qtbot.waitUntil(lambda: bool(loads(ctrl, "cam0")), timeout=3000)
    assert "Press Refresh" not in status(panel)
    qtbot.waitUntil(lambda: child(panel, QListWidget, "recordingsList").count() >= 0, timeout=1000)
    assert child(panel, QPushButton, "downloadButton").toolTip() == ""


# ================================================================================================ the player window
def open_player(ctrl, qtbot):
    panel = make_panel(ctrl, qtbot)
    panel.set_camera(IMOU_ID)
    pick_day(panel, DAY)
    refresh(panel)
    lst = child(panel, QListWidget, "recordingsList")
    qtbot.waitUntil(lambda: lst.count() == 2, timeout=3000)
    lst.setCurrentRow(0)
    child(panel, QPushButton, "playButton").click()
    qtbot.waitUntil(lambda: panel.player_window(IMOU_ID) is not None, timeout=3000)
    w = panel.player_window(IMOU_ID)
    qtbot.addWidget(w)
    return panel, w


def test_the_player_window_plays_at_1x_and_has_no_download(ctrl, qtbot):
    panel, w = open_player(ctrl, qtbot)
    speed = child(w, QComboBox, "speedCombo")
    assert [speed.itemData(i) for i in range(speed.count())] == ["1/1"] and not speed.isEnabled()
    assert "1x" in speed.toolTip()
    download = child(w, QAbstractButton, "downloadClipButton")
    assert not download.isEnabled() and download.toolTip() == NO_DOWNLOAD
    qtbot.waitUntil(lambda: bool(ctrl.factory.services[IMOU_ID].calls_to("start_playback")), timeout=3000)
    assert len(loads(ctrl)) == 1  # the clips came from the panel: the window asked nothing
    w.close()
    qtbot.waitUntil(lambda: bool(ctrl.factory.services[IMOU_ID].calls_to("stop_playback")), timeout=3000)


def test_the_player_window_uses_the_cache_for_a_day_it_has_and_asks_for_one_it_has_not(ctrl, qtbot):
    panel, w = open_player(ctrl, qtbot)
    w._date.setDate(QDate(OTHER.year, OTHER.month, OTHER.day))
    qtbot.waitUntil(lambda: len(loads(ctrl)) == 2, timeout=3000)  # a day change in the window is a click: one call
    w._date.setDate(QDate(DAY.year, DAY.month, DAY.day))
    qtbot.wait(150)
    assert len(loads(ctrl)) == 2  # the first day is cached
    assert len(ctrl.factory.services[IMOU_ID].calls_to("start_playback")) == 1  # a day change starts no stream
    w.close()


def test_a_tapo_player_window_keeps_all_speeds_and_the_download_button(ctrl, qtbot):
    from tapo_desktop.ui.player_window import PlayerWindow

    w = PlayerWindow(ctrl, "cam0", day=DAY, clips=CLIPS, start=CLIPS[0].start)
    qtbot.addWidget(w)
    speed = child(w, QComboBox, "speedCombo")
    assert speed.count() == 4 and speed.isEnabled()
    assert child(w, QAbstractButton, "downloadClipButton").toolTip() == ""
    w.close()


# ================================================================================================ the dialog
def test_the_dialog_says_live_and_playback_minutes(qtbot):
    from tests.phase9.test_imou_ui import FakeImouManager, make_dialog

    dlg = make_dialog(qtbot, FakeImouManager())
    text = " ".join(lbl.text() for lbl in dlg.findChildren(QLabel)).lower()
    assert "live and playback" in text
    assert "live and playback" in child(dlg, QLabel, "imouUsageLabel").text().lower()
    assert isinstance(dlg, QObject)
