"""AppController against FakeCameraService (contract: docs/CONTRACT_UI.md, services.controller)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import threading  # noqa: E402
from dataclasses import replace  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

import pytest  # noqa: E402
from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtGui import QImage  # noqa: E402

from tapo_desktop.models import CredentialKind, Credentials, DayNightMode, MotionEvent, StreamQuality  # noqa: E402
from tapo_desktop.services.config_store import ConfigStore  # noqa: E402
from tests.e2e.fakes import CAMERA, CLOUD, DEFAULT_COLOR, RECORDING_DAY, default_recordings, make_config  # noqa: E402

pytestmark = pytest.mark.ui

DIRECT = Qt.ConnectionType.DirectConnection


class Recorder:
    """Collects signal emissions (args + receiving thread) via a DirectConnection."""

    def __init__(self):
        self.events: list[tuple] = []
        self.threads: list[int] = []
        self._lock = threading.Lock()

    def attach(self, signal, tag: str):
        def slot(*args):
            with self._lock:
                self.events.append((tag, *args))
                self.threads.append(threading.get_ident())
        signal.connect(slot, DIRECT)
        return self

    def of(self, tag: str) -> list[tuple]:
        with self._lock:
            return [e[1:] for e in self.events if e[0] == tag]

    def tags(self) -> list[str]:
        with self._lock:
            return [e[0] for e in self.events]

    def clear(self):
        with self._lock:
            self.events.clear()
            self.threads.clear()


def _all_signals(ctrl) -> Recorder:
    rec = Recorder()
    for name in ("cameras_changed", "frame_ready", "stream_state", "status_changed", "command_failed",
                 "motion_event", "recordings_ready", "download_progress", "download_finished",
                 "presets_ready"):
        rec.attach(getattr(ctrl, name), name)
    return rec


def _primed_status(env, qtbot, rec, cam_id="cam0"):
    env.controller.refresh_status(cam_id)
    qtbot.waitUntil(lambda: rec.of("status_changed"), timeout=3000)
    rec.clear()


# --------------------------------------------------------------------------- camera management
def test_cameras_loaded_from_store(make_env):
    env = make_env(3)
    assert [c.id for c in env.controller.cameras()] == ["cam0", "cam1", "cam2"]
    assert env.controller.config_store is env.store
    assert env.factory.created == []  # services are created lazily


def test_add_camera_persists_config_and_vault(make_env, tmp_path, qtbot):
    env = make_env(0)
    rec = _all_signals(env.controller)
    cfg = make_config(7, name="Porch")
    cloud, cam = Credentials("admin", "cl0ud-PW!"), Credentials("viewer", "c4m-PW?")
    env.controller.add_camera(cfg, cloud, cam)
    assert rec.of("cameras_changed") == [()]
    assert [c.id for c in env.controller.cameras()] == ["cam7"]
    assert ConfigStore(tmp_path / "config.json").load() == [cfg]
    assert env.vault.get("cam7", CredentialKind.CLOUD) == cloud
    assert env.vault.get("cam7", CredentialKind.CAMERA_ACCOUNT) == cam
    text = (tmp_path / "config.json").read_text(encoding="utf-8")
    assert "cl0ud-PW!" not in text and "c4m-PW?" not in text
    with pytest.raises(ValueError):
        env.controller.add_camera(replace(cfg, name="dup"), cloud, cam)


def test_update_camera_persists_and_replaces_service(make_env, tmp_path, qtbot):
    env = make_env(2)
    ctrl = env.controller
    ctrl.refresh_status("cam0")
    env.idle(qtbot)
    old = env.svc("cam0")
    new_cloud = Credentials("admin", "new-cloud")
    ctrl.update_camera(replace(env.cameras[0], name="Renamed", host="192.0.2.99"), cloud=new_cloud)
    env.idle(qtbot)
    assert ConfigStore(tmp_path / "config.json").load()[0].name == "Renamed"
    assert ctrl.cameras()[0].host == "192.0.2.99"
    assert env.vault.get("cam0", CredentialKind.CLOUD) == new_cloud
    assert env.vault.get("cam0", CredentialKind.CAMERA_ACCOUNT) == CAMERA  # None = unchanged
    assert "close" in old.call_names()
    ctrl.refresh_status("cam0")
    env.idle(qtbot)
    services = env.factory.created_for("cam0")
    assert len(services) == 2 and services[-1].config.host == "192.0.2.99"
    with pytest.raises(KeyError):
        ctrl.update_camera(make_config(9))


def test_remove_camera_stops_closes_and_forgets(make_env, tmp_path, qtbot):
    env = make_env(2)
    ctrl = env.controller
    ctrl.start_view("cam1", StreamQuality.SD, 64, 36)
    qtbot.waitUntil(lambda: env.has_svc("cam1") and env.svc("cam1").frames_sent > 0, timeout=3000)
    rec = _all_signals(ctrl)
    ctrl.remove_camera("cam1")
    env.idle(qtbot)
    svc = env.svc("cam1")
    names = svc.call_names()
    assert "stop_stream" in names and "close" in names
    assert names.index("stop_stream") < names.index("close")
    assert [c.id for c in ctrl.cameras()] == ["cam0"]
    assert [c.id for c in ConfigStore(tmp_path / "config.json").load()] == ["cam0"]
    assert env.vault.camera_ids() == ["cam0"]
    assert rec.of("cameras_changed")


# --------------------------------------------------------------------------- streaming
def test_start_view_emits_qimage_of_requested_size_and_states(make_env, qtbot):
    env = make_env(1)
    ctrl = env.controller
    rec = _all_signals(ctrl)
    ctrl.start_view("cam0", StreamQuality.SD, 320, 180)
    qtbot.waitUntil(lambda: rec.of("frame_ready"), timeout=3000)
    qtbot.waitUntil(lambda: ("cam0", "PLAYING", "") in rec.of("stream_state"), timeout=3000)
    svc = env.svc("cam0")
    names = svc.call_names()
    assert names.index("connect") < names.index("start_stream")
    assert names.count("connect") == 1
    assert svc.calls_to("start_stream")[0].args == (StreamQuality.SD, 320, 180)
    cam_id, img = rec.of("frame_ready")[0]
    assert cam_id == "cam0"
    assert isinstance(img, QImage) and not img.isNull()
    assert (img.width(), img.height()) == (320, 180)
    assert img.format() == QImage.Format.Format_RGB888
    c = img.pixelColor(160, 90)
    assert (c.red(), c.green(), c.blue()) == DEFAULT_COLOR
    states = [s for (cid, s, _r) in rec.of("stream_state") if cid == "cam0"]
    assert states.index("CONNECTING") < states.index("PLAYING")


def test_frame_qimage_is_deep_copy(make_env, qtbot):
    env = make_env(1)
    rec = _all_signals(env.controller)
    env.controller.start_view("cam0", StreamQuality.SD, 32, 18)
    qtbot.waitUntil(lambda: rec.of("frame_ready"), timeout=3000)
    img = rec.of("frame_ready")[0][1]
    env.controller.stop_view("cam0")
    env.idle(qtbot)
    import gc
    gc.collect()
    c = img.pixelColor(5, 5)  # must not read freed frame bytes
    assert (c.red(), c.green(), c.blue()) == DEFAULT_COLOR


def test_stream_state_reason_forwarded(make_env, qtbot):
    env = make_env(1)
    rec = _all_signals(env.controller)
    env.controller.start_view("cam0", StreamQuality.SD, 64, 36)
    qtbot.waitUntil(lambda: env.has_svc("cam0") and env.svc("cam0").on_state is not None, timeout=3000)
    env.svc("cam0").emit_state("RECONNECTING", "stall timeout")
    qtbot.waitUntil(lambda: ("cam0", "RECONNECTING", "stall timeout") in rec.of("stream_state"), timeout=3000)


def test_start_view_connect_failure_emits_failed(make_env, qtbot):
    env = make_env(1)
    env.factory.fail("cam0", "connect")
    rec = _all_signals(env.controller)
    env.controller.start_view("cam0", StreamQuality.SD, 64, 36)
    qtbot.waitUntil(lambda: any(s == "FAILED" for (_c, s, _r) in rec.of("stream_state")), timeout=3000)
    failed = [r for (c, s, r) in rec.of("stream_state") if s == "FAILED"]
    assert failed[0]  # reason = str(exc), non-empty
    assert "start_stream" not in env.svc("cam0").call_names()


def test_stop_view_stops_stream(make_env, qtbot):
    env = make_env(1)
    env.controller.start_view("cam0", StreamQuality.SD, 64, 36)
    qtbot.waitUntil(lambda: env.has_svc("cam0") and env.svc("cam0").frames_sent > 0, timeout=3000)
    env.controller.stop_view("cam0")
    env.idle(qtbot)
    assert "stop_stream" in env.svc("cam0").call_names()


def test_service_calls_off_gui_thread_and_signals_on_gui_thread(make_env, qtbot):
    env = make_env(1)
    ctrl = env.controller
    rec = _all_signals(ctrl)
    gui = threading.get_ident()
    ctrl.start_view("cam0", StreamQuality.SD, 64, 36)
    ctrl.refresh_status("cam0")
    ctrl.ptz_move("cam0", 1, 0)
    ctrl.ptz_stop("cam0")
    ctrl.load_recordings("cam0", RECORDING_DAY)
    qtbot.waitUntil(lambda: rec.of("frame_ready") and rec.of("status_changed") and rec.of("recordings_ready"),
                    timeout=3000)
    env.idle(qtbot)
    calls = env.svc("cam0").calls
    assert {c.name for c in calls} >= {"connect", "start_stream", "get_status", "ptz_move", "ptz_stop",
                                       "recordings_for_day"}
    assert all(c.thread_id != gui for c in calls), [(c.name, c.thread_id) for c in calls]
    assert rec.threads and all(t == gui for t in rec.threads)


def test_events_started_after_connect_and_motion_forwarded(make_env, qtbot):
    env = make_env(1)
    rec = _all_signals(env.controller)
    env.controller.start_view("cam0", StreamQuality.SD, 64, 36)
    qtbot.waitUntil(lambda: env.has_svc("cam0") and env.svc("cam0").on_event is not None, timeout=3000)
    names = env.svc("cam0").call_names()
    assert names.index("connect") < names.index("start_events")
    evt = MotionEvent(camera_host=env.cameras[0].host, topic="tns1:RuleEngine/CellMotionDetector/Motion",
                      active=True, timestamp=datetime(2024, 3, 9, 12, tzinfo=timezone.utc))
    env.svc("cam0").emit_event(evt)
    qtbot.waitUntil(lambda: rec.of("motion_event"), timeout=3000)
    assert rec.of("motion_event") == [("cam0", evt)]


def test_events_not_started_when_disabled(make_env, qtbot):
    env = make_env(cameras=[make_config(0, enable_onvif_events=False)])
    env.controller.refresh_status("cam0")
    env.idle(qtbot)
    names = env.svc("cam0").call_names()
    assert "connect" in names and "start_events" not in names


def test_stop_all_is_synchronous(make_env, qtbot):
    env = make_env(2)
    ctrl = env.controller
    for cid in ("cam0", "cam1"):
        ctrl.start_view(cid, StreamQuality.SD, 64, 36)
    qtbot.waitUntil(lambda: all(env.has_svc(c) and env.svc(c).on_event is not None and env.svc(c).frames_sent
                                for c in ("cam0", "cam1")), timeout=3000)
    ctrl.stop_all()
    for cid in ("cam0", "cam1"):  # no event processing / waiting in between
        names = env.svc(cid).call_names()
        assert "stop_stream" in names and "stop_events" in names, (cid, names)
        assert env.svc(cid).stream_args is None


# --------------------------------------------------------------------------- status / toggles
def test_refresh_status_emits_full_dict(make_env, qtbot):
    env = make_env(1)
    rec = _all_signals(env.controller)
    env.controller.refresh_status("cam0")
    qtbot.waitUntil(lambda: rec.of("status_changed"), timeout=3000)
    cam_id, status = rec.of("status_changed")[0]
    assert cam_id == "cam0"
    assert status == {"privacy": False, "day_night": DayNightMode.AUTO, "led": False,
                      "motion_detection": True, "alarm": False}


@pytest.mark.parametrize("method,key,value", [
    ("set_privacy", "privacy", True),
    ("set_led", "led", True),
    ("set_alarm", "alarm", True),
    ("set_motion_detection", "motion_detection", False),
    ("set_day_night", "day_night", DayNightMode.ON),
])
def test_toggle_is_optimistic_and_forwarded(make_env, qtbot, method, key, value):
    env = make_env(1)
    rec = _all_signals(env.controller)
    _primed_status(env, qtbot, rec)
    getattr(env.controller, method)("cam0", value)
    # emitted BEFORE returning, without processing events
    emitted = rec.of("status_changed")
    assert emitted, "optimistic status_changed must be emitted synchronously"
    cam_id, status = emitted[0]
    assert cam_id == "cam0" and status[key] == value
    assert set(status) == {"privacy", "day_night", "led", "motion_detection", "alarm"}
    env.idle(qtbot)
    calls = env.svc("cam0").calls_to(method)
    assert [c.args for c in calls] == [(value,)]
    assert rec.of("command_failed") == []


@pytest.mark.parametrize("method,key,value,old", [
    ("set_privacy", "privacy", True, False),
    ("set_led", "led", True, False),
    ("set_day_night", "day_night", DayNightMode.OFF, DayNightMode.AUTO),
])
def test_toggle_failure_emits_command_failed_then_rollback(make_env, qtbot, method, key, value, old):
    env = make_env(1)
    rec = _all_signals(env.controller)
    _primed_status(env, qtbot, rec)
    env.factory.fail("cam0", method)
    getattr(env.controller, method)("cam0", value)
    qtbot.waitUntil(lambda: rec.of("command_failed"), timeout=3000)
    env.idle(qtbot)
    failed = rec.of("command_failed")
    assert failed[0][0] == "cam0" and failed[0][1] == method and failed[0][2]
    tags = [t for t in rec.tags() if t in ("status_changed", "command_failed")]
    assert tags[0] == "status_changed"  # optimistic
    i = tags.index("command_failed")
    assert "status_changed" in tags[i + 1:], "rollback status_changed must follow command_failed"
    final = rec.of("status_changed")[-1][1]
    assert final[key] == old


def test_ptz_forwarded_in_order(make_env, qtbot):
    env = make_env(1)
    ctrl = env.controller
    ctrl.ptz_move("cam0", 0, 1)
    ctrl.ptz_stop("cam0")
    ctrl.ptz_move("cam0", -1, 0)
    ctrl.ptz_stop("cam0")
    env.idle(qtbot)
    seq = [(c.name, c.args) for c in env.svc("cam0").calls if c.name.startswith("ptz")]
    assert seq == [("ptz_move", (0, 1)), ("ptz_stop", ()), ("ptz_move", (-1, 0)), ("ptz_stop", ())]


def test_ptz_failure_reports_command_failed(make_env, qtbot):
    env = make_env(1)
    env.factory.fail("cam0", "ptz_move")
    rec = _all_signals(env.controller)
    env.controller.ptz_move("cam0", 1, 0)
    qtbot.waitUntil(lambda: rec.of("command_failed"), timeout=3000)
    assert rec.of("command_failed")[0][:2] == ("cam0", "ptz_move")


def test_presets_and_goto(make_env, qtbot):
    env = make_env(1)
    rec = _all_signals(env.controller)
    env.controller.load_presets("cam0")
    qtbot.waitUntil(lambda: rec.of("presets_ready"), timeout=3000)
    cam_id, presets = rec.of("presets_ready")[0]
    assert cam_id == "cam0" and [p.name for p in presets] == ["Door", "Window", "Garden"]
    env.controller.goto_preset("cam0", "2")
    env.idle(qtbot)
    assert [c.args for c in env.svc("cam0").calls_to("goto_preset")] == [("2",)]


# --------------------------------------------------------------------------- recordings
def test_load_recordings_emits_recordings_ready(make_env, qtbot):
    env = make_env(1)
    rec = _all_signals(env.controller)
    env.controller.load_recordings("cam0", RECORDING_DAY)
    qtbot.waitUntil(lambda: rec.of("recordings_ready"), timeout=3000)
    assert rec.of("recordings_ready") == [("cam0", RECORDING_DAY, default_recordings()[RECORDING_DAY])]
    assert env.svc("cam0").calls_to("recordings_for_day")[0].args == (RECORDING_DAY,)


def test_load_recordings_failure(make_env, qtbot):
    env = make_env(1)
    env.factory.fail("cam0", "recordings_for_day")
    rec = _all_signals(env.controller)
    env.controller.load_recordings("cam0", RECORDING_DAY)
    qtbot.waitUntil(lambda: rec.of("command_failed"), timeout=3000)
    assert rec.of("command_failed")[0][:2] == ("cam0", "load_recordings")


def test_download_progress_and_finished(make_env, qtbot, tmp_path):
    env = make_env(1)
    rec = _all_signals(env.controller)
    clip = default_recordings()[RECORDING_DAY][0]
    out = tmp_path / "clip.mp4"
    env.controller.download_recording("cam0", clip, out)
    qtbot.waitUntil(lambda: rec.of("download_finished"), timeout=5000)
    progress = [p for (c, p) in rec.of("download_progress") if c == "cam0"]
    assert progress and progress[-1] == pytest.approx(1.0)
    assert progress == sorted(progress) and all(0.0 <= p <= 1.0 for p in progress)
    assert rec.of("download_finished") == [("cam0", str(out), "")]
    assert out.is_file()
    call = env.svc("cam0").calls_to("download_recording")[0]
    assert call.args == (clip, out)
    assert rec.tags().index("download_finished") > max(i for i, t in enumerate(rec.tags())
                                                       if t == "download_progress")


def test_download_failure_reports_error(make_env, qtbot, tmp_path):
    env = make_env(1)
    env.factory.fail("cam0", "download_recording")
    rec = _all_signals(env.controller)
    clip = default_recordings()[RECORDING_DAY][0]
    env.controller.download_recording("cam0", clip, tmp_path / "x.mp4")
    qtbot.waitUntil(lambda: rec.of("download_finished"), timeout=5000)
    cam_id, path, err = rec.of("download_finished")[0]
    assert cam_id == "cam0" and path == "" and err


def test_wait_idle_true_when_nothing_running(make_env, qtbot):
    env = make_env(1)
    assert env.controller.wait_idle(1000) is True
