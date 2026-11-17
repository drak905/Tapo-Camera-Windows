"""AppController playback API: start/pause/resume/seek/speed/stop off the GUI thread, the
playback_frame / playback_state / playback_position signals on the GUI thread, failures, playback next to
the live view, stop_all."""
from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest
from PyQt6.QtGui import QImage

from tapo_desktop.core.errors import StreamLimitError
from tapo_desktop.models import StreamQuality
from tests.e2e.fakes import CLOUD, CAMERA, FakeVault, RECORDING_DAY, default_recordings, make_config
from tests.phase5._fakes import FakePlaybackFactory

pytestmark = pytest.mark.ui

CLIPS = default_recordings()[RECORDING_DAY]


@pytest.fixture
def env(tmp_path, qtbot):
    from tapo_desktop.models import CredentialKind
    from tapo_desktop.services.config_store import ConfigStore
    from tapo_desktop.services.controller import AppController

    made = []

    def make(**service_kwargs):
        factory = FakePlaybackFactory(**service_kwargs)
        store = ConfigStore(tmp_path / "config.json")
        cams = [make_config(0)]
        store.save(cams)
        vault = FakeVault()
        vault.set("cam0", CredentialKind.CLOUD, CLOUD)
        vault.set("cam0", CredentialKind.CAMERA_ACCOUNT, CAMERA)
        ctrl = AppController(store, vault, service_factory=factory)
        made.append((ctrl, factory))
        return ctrl, factory

    yield make
    for ctrl, factory in made:
        ctrl.stop_all()
        factory.shutdown()


class Signals:
    def __init__(self, ctrl):
        self.gui = threading.get_ident()
        self.frames: list[tuple[str, QImage]] = []
        self.states: list[tuple[str, str, str]] = []
        self.positions: list[tuple[str, float]] = []
        self.wrong_thread = 0
        ctrl.playback_frame.connect(lambda cid, img: self._add(self.frames, (cid, img)))
        ctrl.playback_state.connect(lambda cid, s, r: self._add(self.states, (cid, s, r)))
        ctrl.playback_position.connect(lambda cid, u: self._add(self.positions, (cid, u)))

    def _add(self, target, item):
        if threading.get_ident() != self.gui:
            self.wrong_thread += 1
        target.append(item)


def test_start_emits_frames_state_and_position(env, qtbot):
    ctrl, factory = env()
    sig = Signals(ctrl)
    start, end = CLIPS[0].start, CLIPS[-1].end
    ctrl.start_playback("cam0", start, end, clips=CLIPS, width=320, height=180)
    qtbot.waitUntil(lambda: bool(sig.frames) and bool(sig.positions), timeout=5000)
    qtbot.waitUntil(lambda: ("cam0", "PLAYING", "") in sig.states, timeout=3000)
    cid, img = sig.frames[-1]
    assert cid == "cam0" and (img.width(), img.height()) == (320, 180)
    assert img.pixelColor(5, 5).blue() == 200
    svc = factory.services["cam0"]
    assert svc.playback_args == (start.timestamp(), end.timestamp(), 320, 180)
    assert svc.playback_kwargs["segments"] == [(c.start.timestamp(), c.end.timestamp()) for c in CLIPS]
    assert sig.positions[-1][1] >= start.timestamp()
    assert sig.wrong_thread == 0
    call = svc.calls_to("start_playback")[0]
    assert call.thread_id != threading.get_ident()


def test_controls_reach_the_service_in_order(env, qtbot):
    ctrl, factory = env()
    ctrl.start_playback("cam0", CLIPS[0].start, CLIPS[-1].end)
    ctrl.pause_playback("cam0")
    ctrl.resume_playback("cam0")
    ctrl.seek_playback("cam0", CLIPS[1].start.timestamp())
    ctrl.set_playback_speed("cam0", "2/1")
    ctrl.stop_playback("cam0")
    assert ctrl.wait_idle(5000)
    names = [n for n in factory.services["cam0"].call_names() if "playback" in n]
    assert names == ["start_playback", "pause_playback", "resume_playback", "seek_playback",
                     "set_playback_speed", "stop_playback"]
    assert factory.services["cam0"].calls_to("seek_playback")[0].args == (CLIPS[1].start.timestamp(),)


def test_start_accepts_epoch_seconds(env, qtbot):
    ctrl, factory = env()
    ctrl.start_playback("cam0", 1_700_000_000, 1_700_000_100)
    assert ctrl.wait_idle(5000)
    assert factory.services["cam0"].playback_args[:2] == (1_700_000_000.0, 1_700_000_100.0)


def test_failure_is_reported_as_playback_state(env, qtbot):
    ctrl, factory = env(playback_error=StreamLimitError("camera refused another media session (-52407)"))
    sig = Signals(ctrl)
    ctrl.start_playback("cam0", CLIPS[0].start, CLIPS[0].end)
    qtbot.waitUntil(lambda: bool(sig.states), timeout=5000)
    cid, state, reason = sig.states[-1]
    assert (cid, state) == ("cam0", "FAILED") and reason


def test_end_of_recordings_state(env, qtbot):
    ctrl, factory = env()
    sig = Signals(ctrl)
    t = datetime(2024, 3, 9, 1, 0, tzinfo=timezone.utc).timestamp()
    ctrl.start_playback("cam0", t, t + 0.3)
    qtbot.waitUntil(lambda: ("cam0", "STOPPED", "end of recordings") in sig.states, timeout=5000)


def test_live_view_and_playback_run_together(env, qtbot):
    ctrl, factory = env()
    live: list = []
    ctrl.frame_ready.connect(lambda cid, img: live.append(img))
    sig = Signals(ctrl)
    ctrl.start_view("cam0", StreamQuality.SD, 160, 90)
    ctrl.start_playback("cam0", CLIPS[0].start, CLIPS[-1].end, width=320, height=180)
    qtbot.waitUntil(lambda: len(live) >= 3 and len(sig.frames) >= 3, timeout=5000)
    assert all((i.width(), i.height()) == (160, 90) for i in live)
    assert all((i.width(), i.height()) == (320, 180) for _, i in sig.frames)
    svc = factory.services["cam0"]
    assert svc.stream_args is not None and svc.playback_args is not None


def test_stop_all_stops_playback(env, qtbot):
    ctrl, factory = env()
    ctrl.start_playback("cam0", CLIPS[0].start, CLIPS[-1].end)
    assert ctrl.wait_idle(5000)
    ctrl.stop_all()
    assert "stop_playback" in factory.services["cam0"].call_names()


def test_unknown_camera_is_ignored(env, qtbot):
    ctrl, factory = env()
    ctrl.start_playback("nope", CLIPS[0].start, CLIPS[0].end)
    ctrl.pause_playback("nope")
    ctrl.stop_playback("nope")
    assert ctrl.wait_idle(3000)
