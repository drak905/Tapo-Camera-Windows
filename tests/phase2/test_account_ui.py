"""MainWindow account button/menu, camera row status and the snapshot button."""
from __future__ import annotations

import os
import pathlib

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtGui import QImage  # noqa: E402
from PyQt6.QtWidgets import QAbstractButton, QListWidget, QToolButton, QWidget  # noqa: E402

from tests.e2e.fakes import DEFAULT_COLOR  # noqa: E402
from tapo_desktop.core.errors import TapoConnectionError  # noqa: E402
from tests.phase2._app_helpers import app_env, show_main_window, sign_in  # noqa: E402,F401
from tests.phase2.app_fakes import EMAIL, PASSWORD, cloud_device, lan_for  # noqa: E402

pytestmark = pytest.mark.ui


def _rows(w) -> list[str]:
    lst = w.findChild(QListWidget, "cameraList")
    return [lst.item(i).text() for i in range(lst.count())]


def _row_for(w, camera_id):
    lst = w.findChild(QListWidget, "cameraList")
    for i in range(lst.count()):
        if lst.item(i).data(Qt.ItemDataRole.UserRole) == camera_id:
            return lst.item(i)
    raise AssertionError(camera_id)


def _account_actions(w) -> dict:
    btn = w.findChild(QToolButton, "accountButton")
    assert btn is not None
    menu = btn.menu()
    assert menu is not None
    return {a.text().replace("&", ""): a for a in menu.actions() if not a.isSeparator()}


def test_account_menu_refresh_and_sign_out(app_env, qtbot):
    d0, d1 = cloud_device(0), cloud_device(1)
    env = app_env(1, devices=[d0, d1], lan=[lan_for(d0, "192.168.1.20"), lan_for(d1, "192.168.1.21")])
    w = show_main_window(env, qtbot)
    btn = w.findChild(QToolButton, "accountButton")
    sign_in(env, qtbot, EMAIL, PASSWORD)
    qtbot.waitUntil(lambda: bool(len(_rows(w)) == 3), timeout=3000)
    assert EMAIL in btn.text() or EMAIL in btn.toolTip()

    actions = _account_actions(w)
    assert "Refresh cameras" in actions and "Sign out" in actions
    assert actions["Refresh cameras"].isEnabled() and actions["Sign out"].isEnabled()
    calls = env.discover.calls
    actions["Refresh cameras"].trigger()
    env.idle(qtbot)
    assert env.discover.calls == calls + 1
    assert "list_cameras" in env.cloud.call_names()

    actions["Sign out"].trigger()
    env.idle(qtbot)
    qtbot.waitUntil(lambda: bool(_rows(w) == ["Cam 0"]), timeout=3000)
    assert not _account_actions(w)["Sign out"].isEnabled()
    assert EMAIL not in btn.text()


def test_account_menu_without_manager_is_harmless(app_env, qtbot):
    from tapo_desktop.ui.main_window import MainWindow

    env = app_env(1)
    w = MainWindow(env.controller, tray_enabled=False)
    qtbot.addWidget(w)
    env.windows.append(w)
    actions = _account_actions(w)
    assert not actions["Sign out"].isEnabled() and not actions["Refresh cameras"].isEnabled()


def test_camera_rows_show_offline_and_not_on_network(app_env, qtbot):
    d_ok, d_off, d_away = cloud_device(0, alias="Hall"), cloud_device(1, alias="Shed", online=False), \
        cloud_device(2, alias="Attic")
    env = app_env(1, devices=[d_ok, d_off, d_away], lan=[lan_for(d_ok, "192.168.1.30")])
    w = show_main_window(env, qtbot)
    sign_in(env, qtbot, EMAIL, PASSWORD)
    qtbot.waitUntil(lambda: bool(len(_rows(w)) == 4), timeout=3000)
    assert _row_for(w, "cam0").text() == "Cam 0"
    assert _row_for(w, d_ok.device_id).text() == "Hall"
    assert "Shed" in _row_for(w, d_off.device_id).text() and "Offline" in _row_for(w, d_off.device_id).text()
    away = _row_for(w, d_away.device_id).text()
    assert "Attic" in away and "Not on this network" in away
    env.idle(qtbot)


def test_sign_in_dialog_with_real_manager_lists_cameras(app_env, qtbot):
    from PyQt6.QtWidgets import QDialog, QLineEdit

    from tapo_desktop.ui.sign_in_dialog import SignInDialog

    dev = cloud_device(0, alias="Porch")
    env = app_env(devices=[dev], lan=[lan_for(dev, "192.168.1.44")])
    w = show_main_window(env, qtbot)
    dlg = SignInDialog(env.accounts, w)
    qtbot.addWidget(dlg)
    dlg.show()
    dlg.findChild(QLineEdit, "emailEdit").setText(EMAIL)
    dlg.findChild(QLineEdit, "passwordEdit").setText("wrong")
    dlg.findChild(QAbstractButton, "signInButton").click()
    qtbot.waitUntil(lambda: bool(dlg.findChild(QWidget, "errorLabel").isVisible()), timeout=3000)
    assert "password" in dlg.findChild(QWidget, "errorLabel").text().lower()
    dlg.findChild(QLineEdit, "passwordEdit").setText(PASSWORD)
    dlg.findChild(QAbstractButton, "signInButton").click()
    qtbot.waitUntil(lambda: bool(dlg.result() == QDialog.DialogCode.Accepted), timeout=3000)
    qtbot.waitUntil(lambda: bool(_rows(w) == ["Porch"]), timeout=3000)
    env.idle(qtbot)
    # the visible tile was asked to stream right away (live view starts automatically)
    qtbot.waitUntil(lambda: bool(dev.device_id in env.factory.services
                                 and env.factory.services[dev.device_id].frames_sent > 0), timeout=3000)


def test_silent_sign_in_mfa_challenge_opens_dialog_on_mfa(app_env, qtbot, monkeypatch):
    from PyQt6.QtWidgets import QDialog

    from tapo_desktop.models import ACCOUNT_VAULT_ID, CredentialKind, Credentials
    from tapo_desktop.ui import sign_in_dialog

    opened = []

    class RecordingDialog:
        def __init__(self, accounts, parent=None):
            self.mfa = None
            opened.append(self)

        def show_mfa(self, types):
            self.mfa = list(types)

        def exec(self):
            return QDialog.DialogCode.Rejected

        def deleteLater(self):
            pass

    monkeypatch.setattr(sign_in_dialog, "SignInDialog", RecordingDialog)
    env = app_env()
    env.cloud.mfa_types = [2, 4]
    env.vault.set(ACCOUNT_VAULT_ID, CredentialKind.CLOUD, Credentials(EMAIL, PASSWORD))
    show_main_window(env, qtbot)
    assert env.accounts.restore_session() is True
    qtbot.waitUntil(lambda: bool(opened), timeout=3000)
    assert opened[0].mfa == [2, 4]


def test_snapshot_saves_png_of_current_frame(app_env, qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "profile"))
    env = app_env(1, frame_interval=0.02)
    w = show_main_window(env, qtbot)
    qtbot.waitUntil(lambda: bool(env.controller.latest_image("cam0") is not None), timeout=3000)
    w.findChild(QWidget, "cameraGrid").select_camera("cam0")
    button = w.findChild(QAbstractButton, "snapshotButton")
    assert button is not None and button.isEnabled()
    button.click()
    folder = pathlib.Path(tmp_path / "profile" / "Pictures" / "Tepo Desktop")
    files = list(folder.glob("*.png"))
    assert len(files) == 1 and files[0].name.startswith("Cam 0_")
    image = QImage(str(files[0]))
    assert not image.isNull() and image.width() > 0
    pixel = image.pixelColor(image.width() // 2, image.height() // 2)
    assert (pixel.red(), pixel.green(), pixel.blue()) == DEFAULT_COLOR
    assert str(files[0].name) in w.statusBar().currentMessage()
    button.click()  # a second snapshot in the same second gets a distinct name
    assert len(list(folder.glob("*.png"))) == 2


def test_snapshot_without_frame_reports_and_writes_nothing(app_env, qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "profile"))
    dev = cloud_device(0)
    env = app_env(devices=[dev])  # not on the LAN -> never a frame
    env.factory.fail(dev.device_id, "start_stream", TapoConnectionError("Not on this network"))
    w = show_main_window(env, qtbot)
    sign_in(env, qtbot, EMAIL, PASSWORD)
    w.findChild(QWidget, "cameraGrid").select_camera(dev.device_id)
    w.findChild(QAbstractButton, "snapshotButton").click()
    assert not (tmp_path / "profile" / "Pictures" / "Tepo Desktop").exists() or \
        not list((tmp_path / "profile" / "Pictures" / "Tepo Desktop").glob("*.png"))
    assert "no video" in w.statusBar().currentMessage().lower()


def test_snapshot_filename_is_sanitised(tmp_path, monkeypatch, qapp):
    from datetime import datetime

    from tapo_desktop.ui.snapshot import save_snapshot, snapshot_dir

    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    img = QImage(8, 4, QImage.Format.Format_RGB888)
    img.fill(Qt.GlobalColor.blue)
    path = save_snapshot(img, 'Front: door/"1"?', now=datetime(2026, 1, 2, 3, 4, 5))
    assert path.parent == snapshot_dir() == tmp_path / "Pictures" / "Tepo Desktop"
    assert path.name == "Front_ door__1_20260102_030405.png"
    assert QImage(str(path)).width() == 8
