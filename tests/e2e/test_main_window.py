"""MainWindow: camera list, layouts, keyboard PTZ, add/remove, close-to-tray (docs/CONTRACT_UI.md)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QAbstractButton, QDialog, QListWidget, QMessageBox, QWidget  # noqa: E402

from tapo_desktop.models import StreamQuality  # noqa: E402
from tapo_desktop.services.config_store import ConfigStore  # noqa: E402
from tests.e2e.fakes import CAMERA, CLOUD, make_config, show_window  # noqa: E402

pytestmark = pytest.mark.ui


class _AcceptedCode(int):
    """exec() result that compares equal to both 1 and QDialog.DialogCode.Accepted."""

    def __eq__(self, other):
        if other is QDialog.DialogCode.Accepted:
            return True
        return int(self) == other

    __hash__ = int.__hash__


_ACCEPTED = _AcceptedCode(1)


def _list(w) -> QListWidget:
    lst = w.findChild(QListWidget, "cameraList")
    assert lst is not None
    return lst


def _btn(w, name) -> QAbstractButton:
    b = w.findChild(QAbstractButton, name)
    assert b is not None, name
    return b


def _grid(w):
    g = w.findChild(QWidget, "cameraGrid")
    assert g is not None
    return g


def _visible_ids(w) -> list[str]:
    return [t.camera_id for t in _grid(w).tiles() if t.isVisible()]


def test_object_names_present(make_env, qtbot):
    env = make_env(1)
    w = show_window(env, qtbot)
    for name in ("addCameraButton", "removeCameraButton", "layout1x1Button", "layout2x2Button",
                 "layout3x3Button"):
        _btn(w, name)
    for name in ("cameraGrid", "controlsPanel", "playbackPanel"):
        assert w.findChild(QWidget, name) is not None, name
    assert w.tray is None  # tray_enabled=False


def test_camera_list_shows_cameras(make_env, qtbot):
    env = make_env(3)
    w = show_window(env, qtbot)
    lst = _list(w)
    assert [lst.item(i).text() for i in range(lst.count())] == ["Cam 0", "Cam 1", "Cam 2"]
    assert [lst.item(i).data(Qt.ItemDataRole.UserRole) for i in range(lst.count())] == ["cam0", "cam1", "cam2"]


@pytest.mark.parametrize("button,n,visible", [("layout1x1Button", 1, 1), ("layout2x2Button", 2, 4),
                                              ("layout3x3Button", 3, 9)])
def test_layout_buttons_set_tile_count_and_persist(make_env, qtbot, tmp_path, button, n, visible):
    env = make_env(10, settings={"grid_size": 2 if n != 2 else 3})
    w = show_window(env, qtbot)
    _btn(w, button).click()
    qtbot.waitUntil(lambda: bool(len(_visible_ids(w)) == visible), timeout=3000)
    assert w.grid_size() == n
    assert _visible_ids(w) == [f"cam{i}" for i in range(visible)]
    assert len(_grid(w).tiles()) == 10
    assert ConfigStore(tmp_path / "config.json").get_setting("grid_size") == n


def test_grid_size_read_from_settings_and_validated(make_env, qtbot):
    env = make_env(10, settings={"grid_size": 3})
    w = show_window(env, qtbot)
    assert w.grid_size() == 3
    qtbot.waitUntil(lambda: bool(len(_visible_ids(w)) == 9), timeout=3000)
    for bad in (0, 4, -1):
        with pytest.raises(ValueError):
            w.set_grid_size(bad)
    assert w.grid_size() == 3


def test_visible_tiles_streamed_hidden_tiles_stopped(make_env, qtbot):
    env = make_env(4, settings={"grid_size": 2})
    w = show_window(env, qtbot)
    qtbot.waitUntil(lambda: bool(all(env.has_svc(f"cam{i}") and env.svc(f"cam{i}").calls_to("start_stream")
                                for i in range(4))), timeout=3000)
    assert all(env.svc(f"cam{i}").calls_to("start_stream")[-1].args[0] is StreamQuality.SD for i in range(4))
    w.set_grid_size(1)
    env.idle(qtbot)
    for i in (1, 2, 3):
        assert env.svc(f"cam{i}").calls_to("stop_stream"), f"cam{i} should be stopped"
    assert env.svc("cam0").stream_args is not None


def test_list_selection_syncs_grid(make_env, qtbot):
    env = make_env(3)
    w = show_window(env, qtbot)
    _list(w).setCurrentRow(2)
    qtbot.waitUntil(lambda: bool(_grid(w).selected_camera_id() == "cam2"), timeout=2000)


@pytest.mark.parametrize("key,signs", [
    (Qt.Key.Key_W, (0, 1)), (Qt.Key.Key_S, (0, -1)), (Qt.Key.Key_A, (-1, 0)), (Qt.Key.Key_D, (1, 0)),
    (Qt.Key.Key_Up, (0, 1)), (Qt.Key.Key_Down, (0, -1)), (Qt.Key.Key_Left, (-1, 0)), (Qt.Key.Key_Right, (1, 0)),
])
def test_keyboard_ptz_press_moves_release_stops(make_env, qtbot, key, signs):
    env = make_env(2)
    w = show_window(env, qtbot)
    _list(w).setCurrentRow(1)
    qtbot.waitUntil(lambda: bool(_grid(w).selected_camera_id() == "cam1"), timeout=2000)
    qtbot.keyPress(w, key)
    env.idle(qtbot)
    moves = env.svc("cam1").calls_to("ptz_move")
    assert len(moves) == 1
    dx, dy = moves[0].args

    def sign(v):
        return (v > 0) - (v < 0)

    assert (sign(dx), sign(dy)) == signs
    assert not env.svc("cam1").calls_to("ptz_stop")
    qtbot.keyRelease(w, key)
    env.idle(qtbot)
    names = [n for n in env.svc("cam1").call_names() if n.startswith("ptz")]
    assert names == ["ptz_move", "ptz_stop"]
    assert not (env.has_svc("cam0") and env.svc("cam0").calls_to("ptz_move"))


def test_keyboard_ptz_without_selection_does_nothing(make_env, qtbot):
    env = make_env(1)
    w = show_window(env, qtbot)
    _grid(w).select_camera(None)
    _list(w).clearSelection()
    _list(w).setCurrentRow(-1)
    qtbot.keyPress(w, Qt.Key.Key_D)
    qtbot.keyRelease(w, Qt.Key.Key_D)
    env.idle(qtbot)
    assert not any(s.calls_to("ptz_move") or s.calls_to("ptz_stop") for s in env.factory.created)


def test_add_camera_button(make_env, qtbot, monkeypatch):
    from tapo_desktop.ui import camera_dialog

    env = make_env(1)
    w = show_window(env, qtbot)
    new = make_config(5, name="Shed")
    monkeypatch.setattr(camera_dialog.CameraDialog, "exec", lambda self: _ACCEPTED)
    monkeypatch.setattr(camera_dialog.CameraDialog, "result_values", lambda self: (new, CLOUD, CAMERA))
    _btn(w, "addCameraButton").click()
    qtbot.waitUntil(lambda: bool(_list(w).count() == 2), timeout=2000)
    assert [c.id for c in env.controller.cameras()] == ["cam0", "cam5"]
    assert _list(w).item(1).text() == "Shed"
    assert env.vault.get("cam5", "cloud") == CLOUD
    qtbot.waitUntil(lambda: bool("cam5" in [t.camera_id for t in _grid(w).tiles()]), timeout=2000)


def test_remove_camera_button_confirms(make_env, qtbot, monkeypatch):
    env = make_env(2)
    w = show_window(env, qtbot)
    _list(w).setCurrentRow(1)
    qtbot.waitUntil(lambda: bool(_grid(w).selected_camera_id() == "cam1"), timeout=2000)
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.No))
    _btn(w, "removeCameraButton").click()
    qtbot.wait(50)
    assert len(env.controller.cameras()) == 2
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    _btn(w, "removeCameraButton").click()
    qtbot.waitUntil(lambda: bool(_list(w).count() == 1), timeout=2000)
    assert [c.id for c in env.controller.cameras()] == ["cam0"]
    assert "cam1" not in env.vault.camera_ids()
    assert [t.camera_id for t in _grid(w).tiles()] == ["cam0"]


def test_close_minimizes_to_tray(make_env, qtbot, no_app_quit):
    env = make_env(1, settings={"minimize_to_tray": True})
    w = show_window(env, qtbot)
    qtbot.waitUntil(lambda: bool(env.has_svc("cam0") and env.svc("cam0").frames_sent > 0), timeout=3000)
    assert w.close() is False  # close event ignored
    qtbot.wait(50)
    assert not w.isVisible()
    assert no_app_quit == []
    assert "stop_stream" not in env.svc("cam0").call_names()  # streams keep running


def test_close_quits_when_tray_disabled(make_env, qtbot, no_app_quit):
    env = make_env(1, settings={"minimize_to_tray": False})
    w = show_window(env, qtbot)
    qtbot.waitUntil(lambda: bool(env.has_svc("cam0") and env.svc("cam0").frames_sent > 0), timeout=3000)
    w.close()
    qtbot.waitUntil(lambda: bool(no_app_quit != []), timeout=2000)
    names = env.svc("cam0").call_names()
    assert "stop_stream" in names and "stop_events" in names


def test_quit_stops_everything(make_env, qtbot, no_app_quit):
    env = make_env(2)
    w = show_window(env, qtbot)
    qtbot.waitUntil(lambda: bool(all(env.has_svc(c) and env.svc(c).frames_sent > 0 for c in ("cam0", "cam1"))),
                    timeout=3000)
    w.quit()
    for c in ("cam0", "cam1"):
        assert "stop_stream" in env.svc(c).call_names()
    assert no_app_quit
