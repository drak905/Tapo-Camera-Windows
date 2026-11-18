"""MainWindow: camera list sorting (``sortButton``, setting ``camera_sort``) and renaming (RenameDialog,
list context menu, F2, tile menu); every view of the name updates at once."""
from __future__ import annotations

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialog, QLabel, QLineEdit, QListWidget, QPushButton, QToolButton

from tapo_desktop.core.errors import TapoDeviceError
from tests.e2e.fakes import make_config, show_window

pytestmark = [pytest.mark.ui, pytest.mark.timeout(60)]

UID = Qt.ItemDataRole.UserRole


def _list(w) -> QListWidget:
    return w.findChild(QListWidget, "cameraList")


def _ids(w) -> list[str]:
    lst = _list(w)
    return [lst.item(i).data(UID) for i in range(lst.count())]


def _texts(w) -> list[str]:
    lst = _list(w)
    return [lst.item(i).text() for i in range(lst.count())]


def _sort_action(w, name: str):
    button = w.findChild(QToolButton, "sortButton")
    assert button is not None and button.menu() is not None
    return next(a for a in button.menu().actions() if a.objectName() == name)


NAMED = [make_config(0, name="Zebra"), make_config(1, name="apple"), make_config(2, name="Cam 10"),
         make_config(3, name="Cam 2")]


def _fake_dialog(monkeypatch, text: str | None, *, accept: bool = True, seen: list | None = None):
    """Replace RenameDialog.exec: type ``text`` and press OK (or cancel)."""
    from tapo_desktop.ui.rename_dialog import RenameDialog

    def fake_exec(self):
        if seen is not None:
            seen.append(self.findChild(QLabel, "renameInfo").text())
        if text is not None:
            self.findChild(QLineEdit, "renameEdit").setText(text)
        return QDialog.DialogCode.Accepted if accept else QDialog.DialogCode.Rejected

    monkeypatch.setattr(RenameDialog, "exec", fake_exec)


# ================================================================== sorting
def test_the_list_is_sorted_a_to_z_by_default_and_the_grid_keeps_its_order(make_env, qtbot):
    env = make_env(cameras=NAMED)
    w = show_window(env, qtbot)
    assert _texts(w) == ["apple", "Cam 2", "Cam 10", "Zebra"]
    assert w.camera_sort() == "name_asc"
    assert w._grid.slots()[:4] == ["cam0", "cam1", "cam2", "cam3"]


def test_z_to_a_and_account_order_from_the_sort_menu_are_persisted(make_env, qtbot):
    env = make_env(cameras=NAMED)
    w = show_window(env, qtbot)
    _sort_action(w, "sortNameDescAction").trigger()
    assert _texts(w) == ["Zebra", "Cam 10", "Cam 2", "apple"]
    assert env.store.get_setting("camera_sort") == "name_desc"
    assert _sort_action(w, "sortNameDescAction").isChecked()
    _sort_action(w, "sortAccountAction").trigger()
    assert _ids(w) == ["cam0", "cam1", "cam2", "cam3"]
    assert env.store.get_setting("camera_sort") == "account"
    w2 = show_window(env, qtbot)
    assert w2.camera_sort() == "account" and _ids(w2) == ["cam0", "cam1", "cam2", "cam3"]
    assert _sort_action(w2, "sortAccountAction").isChecked()


def test_an_invalid_sort_setting_falls_back_to_a_to_z(make_env, qtbot):
    env = make_env(cameras=NAMED, settings={"camera_sort": "random"})
    w = show_window(env, qtbot)
    assert w.camera_sort() == "name_asc" and _texts(w)[0] == "apple"
    with pytest.raises(ValueError):
        w.set_camera_sort("random")


def test_selection_survives_re_sorting(make_env, qtbot):
    env = make_env(cameras=NAMED)
    w = show_window(env, qtbot)
    _list(w).setCurrentRow(_ids(w).index("cam2"))
    assert w.selected_camera_id() == "cam2"
    w.set_camera_sort("name_desc")
    assert w.selected_camera_id() == "cam2"
    assert _list(w).currentItem().data(UID) == "cam2"


def test_the_status_suffix_is_not_part_of_the_sort_key(make_env, qtbot):
    cams = [make_config(0, name="Cam Front", source="cloud", device_id="d0"),
            make_config(1, name="Cam", source="cloud", device_id="d1", online=False)]
    env = make_env(cameras=cams)
    w = show_window(env, qtbot)
    assert _ids(w) == ["cam1", "cam0"]
    assert _texts(w)[0] == "Cam (Offline)"


def test_the_list_re_sorts_after_a_rename_and_keeps_the_selection(make_env, qtbot):
    env = make_env(cameras=NAMED)
    w = show_window(env, qtbot)
    _list(w).setCurrentRow(_ids(w).index("cam1"))
    env.controller.rename_camera("cam1", "zzz")
    assert _texts(w) == ["Cam 2", "Cam 10", "Zebra", "zzz"]
    assert w.selected_camera_id() == "cam1" and _list(w).currentItem().data(UID) == "cam1"


# ================================================================== RenameDialog
def test_rename_dialog_validates_inline(qtbot):
    from tapo_desktop.ui.rename_dialog import RenameDialog

    dialog = RenameDialog("Cam 0", on_camera=True)
    qtbot.addWidget(dialog)
    edit = dialog.findChild(QLineEdit, "renameEdit")
    ok = dialog.findChild(QPushButton, "renameOkButton")
    error = dialog.findChild(QLabel, "renameError")
    assert edit.text() == "Cam 0" and not ok.isEnabled()  # unchanged: nothing to save (like the app)
    edit.setText("   ")
    assert not ok.isEnabled() and error.isVisibleTo(dialog) and error.text()
    edit.setText("ổ" * 11)
    assert not ok.isEnabled() and error.isVisibleTo(dialog) and "32" in error.text()
    edit.setText("  Cổng trước ")
    assert ok.isEnabled() and not error.isVisibleTo(dialog)
    assert dialog.value() == "Cổng trước"


def test_rename_dialog_says_where_the_name_changes(qtbot):
    from tapo_desktop.ui.rename_dialog import RenameDialog

    on_camera = RenameDialog("A", on_camera=True)
    local = RenameDialog("A", on_camera=False)
    qtbot.addWidget(on_camera)
    qtbot.addWidget(local)
    assert "Tapo app" in on_camera.findChild(QLabel, "renameInfo").text()
    assert "only in this app" in local.findChild(QLabel, "renameInfo").text()


# ================================================================== rename from the UI
def test_rename_from_the_list_menu_updates_list_tile_and_controls_header(make_env, qtbot, monkeypatch):
    env = make_env(cameras=[make_config(0, name="Old")])
    w = show_window(env, qtbot)
    _list(w).setCurrentRow(0)
    _fake_dialog(monkeypatch, "Brand new")
    menu = w.camera_list_menu("cam0")
    rename = next(a for a in menu.actions() if a.objectName() == "renameAction")
    assert rename.isEnabled()
    rename.trigger()
    assert _texts(w) == ["Brand new"]
    assert w._grid.tile("cam0").name() == "Brand new"
    assert w.findChild(QLabel, "controlsTitle").text() == "Brand new"
    assert env.controller.camera("cam0").name == "Brand new"


def test_cancel_changes_nothing(make_env, qtbot, monkeypatch):
    env = make_env(cameras=[make_config(0, name="Old")])
    w = show_window(env, qtbot)
    _fake_dialog(monkeypatch, "Other", accept=False)
    w.rename_camera("cam0")
    assert env.controller.camera("cam0").name == "Old"


def test_f2_renames_the_selected_row(make_env, qtbot, monkeypatch):
    env = make_env(cameras=[make_config(0, name="Old"), make_config(1, name="Other")])
    w = show_window(env, qtbot)
    _list(w).setCurrentRow(_ids(w).index("cam1"))
    _fake_dialog(monkeypatch, "Renamed by F2")
    _list(w).setFocus()
    qtbot.keyClick(_list(w), Qt.Key.Key_F2)
    assert env.controller.camera("cam1").name == "Renamed by F2"
    assert env.controller.camera("cam0").name == "Old"


def test_tile_menu_offers_rename(make_env, qtbot, monkeypatch):
    env = make_env(cameras=[make_config(0, name="Old")])
    w = show_window(env, qtbot)
    _fake_dialog(monkeypatch, "From the tile")
    menu = w._grid.slot_menu(0)
    action = next(a for a in menu.actions() if a.objectName() == "renameCameraAction")
    assert action.isEnabled()
    action.trigger()
    assert env.controller.camera("cam0").name == "From the tile"


def test_dialog_text_follows_the_rename_mode(make_env, qtbot, monkeypatch):
    env = make_env(cameras=[make_config(0, name="Owned", source="cloud", role=0, device_id="d0", cloud_alias="Owned"),
                            make_config(1, name="Shared", source="cloud", role=1, device_id="d1", cloud_alias="Shared")])
    w = show_window(env, qtbot)
    seen: list[str] = []
    _fake_dialog(monkeypatch, None, accept=False, seen=seen)
    w.rename_camera("cam0")
    w.rename_camera("cam1")
    assert "Tapo app" in seen[0] and "only in this app" in seen[1]


def test_reset_to_camera_name_in_the_menu(make_env, qtbot, monkeypatch):
    env = make_env(cameras=[make_config(0, name="Shared", source="cloud", role=1, device_id="d1", cloud_alias="Shared")])
    w = show_window(env, qtbot)
    reset = next(a for a in w.camera_list_menu("cam0").actions() if a.objectName() == "resetNameAction")
    assert not reset.isEnabled()
    _fake_dialog(monkeypatch, "Mine")
    w.rename_camera("cam0")
    assert _texts(w) == ["Mine"]
    reset = next(a for a in w.camera_list_menu("cam0").actions() if a.objectName() == "resetNameAction")
    assert reset.isEnabled()
    reset.trigger()
    assert _texts(w) == ["Shared"] and env.controller.camera("cam0").display_name == ""


def test_a_failed_camera_rename_rolls_back_and_says_so(make_env, qtbot, monkeypatch):
    env = make_env(cameras=[make_config(0, name="Owned", source="cloud", role=0, device_id="d0", cloud_alias="Owned")])
    env.factory.fail("cam0", "rename", TapoDeviceError(-40210, "setDeviceAlias"))
    w = show_window(env, qtbot)
    _fake_dialog(monkeypatch, "New name")
    w.rename_camera("cam0")
    assert _texts(w) == ["New name"]  # optimistic
    env.idle(qtbot)
    qtbot.waitUntil(lambda: _texts(w) == ["Owned"], timeout=3000)
    assert "rename" in w.statusBar().currentMessage().lower()


def test_player_window_title_follows_a_rename(make_env, qtbot):
    from tapo_desktop.ui.player_window import PlayerWindow

    env = make_env(cameras=[make_config(0, name="Old")])
    player = PlayerWindow(env.controller, "cam0", clips=[])
    qtbot.addWidget(player)
    env.controller.rename_camera("cam0", "Garage")
    assert "Garage" in player.windowTitle()
    assert player.findChild(QLabel, "playbackCameraName").text() == "Garage"
