"""PlaybackPanel, CameraDialog, TrayController and app.main (docs/CONTRACT_UI.md)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import subprocess  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from PyQt6.QtCore import QDate, Qt  # noqa: E402
from PyQt6.QtWidgets import (  # noqa: E402
    QAbstractButton, QCheckBox, QDateEdit, QDialog, QLineEdit, QListWidget, QProgressBar, QWidget,
)

from tapo_desktop.models import CameraConfig, Credentials, MotionEvent, StreamQuality  # noqa: E402
from tests.e2e.fakes import RECORDING_DAY, default_recordings, show_window  # noqa: E402

pytestmark = pytest.mark.ui

REPO_ROOT = Path(__file__).resolve().parents[2]


# =========================================================================== PlaybackPanel
def _playback(env, qtbot):
    from tapo_desktop.ui.playback_panel import PlaybackPanel

    panel = PlaybackPanel(env.controller)
    qtbot.addWidget(panel)
    panel.resize(400, 400)
    panel.show()
    qtbot.waitExposed(panel)
    return panel


def _clip_text(clip) -> str:
    return f"{clip.start.astimezone():%H:%M:%S} - {clip.end.astimezone():%H:%M:%S}"


def _load_day(env, qtbot, panel):
    panel.set_camera("cam0")
    date_edit = panel.findChild(QDateEdit, "recordingDate")
    assert date_edit is not None
    date_edit.setDate(QDate(RECORDING_DAY.year, RECORDING_DAY.month, RECORDING_DAY.day))
    lst = panel.findChild(QListWidget, "recordingsList")
    assert lst is not None
    qtbot.waitUntil(lambda: lst.count() == 2, timeout=3000)
    return lst


def test_playback_lists_clips_for_date(make_env, qtbot):
    env = make_env(1)
    panel = _playback(env, qtbot)
    assert panel.objectName() == "playbackPanel"
    lst = _load_day(env, qtbot, panel)
    assert panel.camera_id() == "cam0"
    clips = default_recordings()[RECORDING_DAY]
    assert [lst.item(i).text() for i in range(2)] == [_clip_text(c) for c in clips]
    assert [lst.item(i).data(Qt.ItemDataRole.UserRole) for i in range(2)] == clips
    days = [c.args[0] for c in env.svc("cam0").calls_to("recordings_for_day")]
    assert days[-1] == RECORDING_DAY


def test_playback_empty_day_clears_list(make_env, qtbot):
    env = make_env(1)
    panel = _playback(env, qtbot)
    lst = _load_day(env, qtbot, panel)
    panel.findChild(QDateEdit, "recordingDate").setDate(QDate(2024, 3, 10))
    qtbot.waitUntil(lambda: lst.count() == 0, timeout=3000)


def test_playback_download_progress_reaches_100(make_env, qtbot, tmp_path):
    env = make_env(1, download_steps=5)
    panel = _playback(env, qtbot)
    lst = _load_day(env, qtbot, panel)
    bar = panel.findChild(QProgressBar, "downloadProgress")
    assert bar is not None
    values: list[int] = []
    bar.valueChanged.connect(values.append)
    out = tmp_path / "clip0.mp4"
    asked = []
    panel.choose_save_path = lambda clip: (asked.append(clip), out)[1]
    lst.setCurrentRow(1)
    panel.findChild(QAbstractButton, "downloadButton").click()
    qtbot.waitUntil(lambda: bar.value() == bar.maximum() and out.exists(), timeout=5000)
    clip = default_recordings()[RECORDING_DAY][1]
    assert asked == [clip]
    assert bar.maximum() == 100
    assert values == sorted(values) and values[-1] == 100
    assert len(set(values)) >= 2  # intermediate progress shown
    assert env.svc("cam0").calls_to("download_recording")[0].args == (clip, out)


def test_playback_download_cancelled_does_nothing(make_env, qtbot):
    env = make_env(1)
    panel = _playback(env, qtbot)
    lst = _load_day(env, qtbot, panel)
    panel.choose_save_path = lambda clip: None
    lst.setCurrentRow(0)
    panel.findChild(QAbstractButton, "downloadButton").click()
    env.idle(qtbot)
    assert env.svc("cam0").calls_to("download_recording") == []


# =========================================================================== CameraDialog
def _dialog(qtbot, config=None):
    from tapo_desktop.ui.camera_dialog import CameraDialog

    dlg = CameraDialog(None, config) if config is not None else CameraDialog()
    qtbot.addWidget(dlg)
    return dlg


def _fill(dlg, **values):
    for name, value in values.items():
        if name == "dualLensCheck":
            dlg.findChild(QCheckBox, name).setChecked(value)
        else:
            dlg.findChild(QLineEdit, name).setText(value)


_VALID = dict(nameEdit="  Porch ", hostEdit=" 192.168.1.50 ", cloudPasswordEdit=" cl oud ",
              cameraUserEdit=" viewer ", cameraPasswordEdit="pw!")


def test_dialog_fields_exist_and_passwords_hidden(qtbot):
    dlg = _dialog(qtbot)
    for name in ("nameEdit", "hostEdit", "cloudPasswordEdit", "cameraUserEdit", "cameraPasswordEdit"):
        assert dlg.findChild(QLineEdit, name) is not None, name
    assert dlg.findChild(QCheckBox, "dualLensCheck") is not None
    for name in ("cloudPasswordEdit", "cameraPasswordEdit"):
        assert dlg.findChild(QLineEdit, name).echoMode() == QLineEdit.EchoMode.Password


@pytest.mark.parametrize("host", ["", "   ", "192.168 .1.5"])
def test_dialog_rejects_bad_host(qtbot, host):
    dlg = _dialog(qtbot)
    _fill(dlg, **(_VALID | {"hostEdit": host}))
    dlg.accept()
    assert dlg.result() != QDialog.DialogCode.Accepted.value


def test_dialog_rejects_missing_password_in_add_mode(qtbot):
    dlg = _dialog(qtbot)
    _fill(dlg, **(_VALID | {"cameraPasswordEdit": ""}))
    dlg.accept()
    assert dlg.result() != QDialog.DialogCode.Accepted.value


def test_dialog_returns_values(qtbot):
    dlg = _dialog(qtbot)
    _fill(dlg, **_VALID, dualLensCheck=True)
    dlg.accept()
    assert dlg.result() == QDialog.DialogCode.Accepted.value
    cfg, cloud, cam = dlg.result_values()
    assert isinstance(cfg, CameraConfig)
    assert (cfg.name, cfg.host, cfg.dual_lens) == ("Porch", "192.168.1.50", True)
    assert cloud == Credentials("admin", " cl oud ")  # passwords are not stripped
    assert cam == Credentials("viewer", "pw!")


def test_dialog_empty_name_defaults_to_host(qtbot):
    dlg = _dialog(qtbot)
    _fill(dlg, **(_VALID | {"nameEdit": ""}))
    dlg.accept()
    assert dlg.result_values()[0].name == "192.168.1.50"


def test_dialog_edit_mode_keeps_id_and_unchanged_passwords(qtbot):
    existing = CameraConfig(name="Old", host="10.0.0.9", id="keep-me", rtsp_port=8554,
                            grid_quality=StreamQuality.TELE_SD)
    dlg = _dialog(qtbot, existing)
    assert dlg.findChild(QLineEdit, "nameEdit").text() == "Old"
    assert dlg.findChild(QLineEdit, "hostEdit").text() == "10.0.0.9"
    assert dlg.findChild(QLineEdit, "cloudPasswordEdit").text() == ""
    dlg.findChild(QLineEdit, "nameEdit").setText("New")
    dlg.accept()
    assert dlg.result() == QDialog.DialogCode.Accepted.value
    cfg, cloud, cam = dlg.result_values()
    assert (cfg.id, cfg.name, cfg.rtsp_port, cfg.grid_quality) == ("keep-me", "New", 8554, StreamQuality.TELE_SD)
    assert cloud is None and cam is None


# =========================================================================== TrayController
class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _evt(active=True) -> MotionEvent:
    return MotionEvent(camera_host="192.0.2.10", topic="tns1:RuleEngine/CellMotionDetector/Motion",
                       active=active, timestamp=datetime(2024, 3, 9, tzinfo=timezone.utc))


def _tray(env, qtbot, clock):
    from tapo_desktop.ui.tray import TrayController

    window = QWidget()
    qtbot.addWidget(window)
    tray = TrayController(window, env.controller, clock=clock)
    toasts: list[tuple[str, str]] = []
    tray.notify = lambda title, message: toasts.append((title, message))
    return tray, toasts


def test_tray_throttles_one_toast_per_camera_per_10s(make_env, qtbot):
    from tapo_desktop.ui import tray as tray_mod

    assert tray_mod.TOAST_INTERVAL_S == 10.0
    env = make_env(2)
    clock = _Clock()
    _tray_obj, toasts = _tray(env, qtbot, clock)
    emit = env.controller.motion_event.emit
    emit("cam0", _evt())
    qtbot.wait(10)
    assert len(toasts) == 1 and toasts[0][0] == "Cam 0" and toasts[0][1]
    clock.t += 5.0
    emit("cam0", _evt())
    clock.t += 4.9
    emit("cam0", _evt())
    qtbot.wait(10)
    assert len(toasts) == 1
    emit("cam1", _evt())  # other camera has its own budget
    qtbot.wait(10)
    assert [t[0] for t in toasts] == ["Cam 0", "Cam 1"]
    clock.t += 0.2  # 10.1 s after the first cam0 toast
    emit("cam0", _evt())
    qtbot.wait(10)
    assert [t[0] for t in toasts] == ["Cam 0", "Cam 1", "Cam 0"]


def test_tray_ignores_inactive_and_unknown(make_env, qtbot):
    env = make_env(1)
    _t, toasts = _tray(env, qtbot, _Clock())
    env.controller.motion_event.emit("cam0", _evt(active=False))
    env.controller.motion_event.emit("ghost", _evt())
    qtbot.wait(10)
    assert toasts == []


def test_tray_respects_notifications_setting(make_env, qtbot):
    env = make_env(1, settings={"notifications": False})
    _t, toasts = _tray(env, qtbot, _Clock())
    env.controller.motion_event.emit("cam0", _evt())
    qtbot.wait(10)
    assert toasts == []


def test_main_window_creates_tray_when_enabled(make_env, qtbot):
    from tapo_desktop.ui.tray import TrayController

    env = make_env(1)
    w = show_window(env, qtbot, tray_enabled=True)
    assert isinstance(w.tray, TrayController)


# =========================================================================== app.main
def test_app_main_selftest_in_process(qapp, tmp_path, monkeypatch):
    from tapo_desktop import app

    home = tmp_path / "home"
    monkeypatch.setenv("TAPO_DESKTOP_HOME", str(home))
    t0 = time.monotonic()
    assert app.main(["--selftest"]) == 0
    assert time.monotonic() - t0 < 15
    assert home.is_dir()


def test_app_main_unknown_argument(qapp, tmp_path, monkeypatch, capsys):
    from tapo_desktop import app

    monkeypatch.setenv("TAPO_DESKTOP_HOME", str(tmp_path / "home"))
    assert app.main(["--definitely-not-an-option"]) == 2
    assert capsys.readouterr().err.strip()


def test_python_m_tapo_desktop_selftest(tmp_path):
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", TAPO_DESKTOP_HOME=str(tmp_path / "home"),
               PYTHONPATH=os.pathsep.join([str(REPO_ROOT / "src"), str(REPO_ROOT)]))
    proc = subprocess.run([sys.executable, "-m", "tapo_desktop", "--selftest"], env=env, cwd=tmp_path,
                          capture_output=True, text=True, timeout=45)
    assert proc.returncode == 0, proc.stderr[-2000:]
