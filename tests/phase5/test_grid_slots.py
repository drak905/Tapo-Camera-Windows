"""Choosing which camera is in each grid position: setting "grid_slots", empty slots, tile and camera-list
context menus, list marks, stream starts/stops, remote gate, fullscreen, new/removed cameras."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QMenu  # noqa: E402

from tapo_desktop.models import StreamQuality  # noqa: E402
from tapo_desktop.services.config_store import ConfigStore  # noqa: E402
from tapo_desktop.services.remote import REMOTE_CLICK_TO_VIEW  # noqa: E402
from tests.e2e.fakes import CAMERA, CLOUD, make_config  # noqa: E402
from tests.phase5._grid_helpers import (  # noqa: E402
    SLOW,
    action,
    camera_list,
    grid,
    open_window,
    streaming,
    submenu,
    visible_ids,
)

pytestmark = [pytest.mark.ui, pytest.mark.timeout(120)]


def _pad(*items):
    return list(items) + [None] * (25 - len(items))


def _saved_slots(tmp_path):
    return ConfigStore(tmp_path / "config.json").get_setting("grid_slots", None)


def _row(w, cid):
    lst = camera_list(w)
    for i in range(lst.count()):
        if lst.item(i).data(Qt.ItemDataRole.UserRole) == cid:
            return lst.item(i)
    raise AssertionError(cid)


# --------------------------------------------------------------------------- defaults + persistence
def test_first_run_slots_are_cameras_in_list_order(make_env, qtbot):
    env = make_env(3, **SLOW)
    w = open_window(env, qtbot)
    g = grid(w)
    assert g.slots() == _pad("cam0", "cam1", "cam2")
    assert g.visible_slots() == ["cam0", "cam1", "cam2", None]
    assert [e.slot_index for e in g.empty_tiles()] == [3]
    assert streaming(env) == {"cam0", "cam1", "cam2"}


def test_saved_slots_are_restored(make_env, qtbot):
    env = make_env(4, settings={"grid_slots": ["cam3", None, "cam0"]}, **SLOW)
    w = open_window(env, qtbot)
    g = grid(w)
    assert g.visible_slots() == ["cam3", None, "cam0", None]
    assert sorted(visible_ids(w)) == ["cam0", "cam3"]
    assert streaming(env) == {"cam0", "cam3"}
    assert not env.has_svc("cam1") or env.svc("cam1").stream_args is None
    # layout order follows the slots, not the camera list
    pos = {cid: g.tile(cid).mapTo(g, g.tile(cid).rect().topLeft()) for cid in ("cam3", "cam0")}
    assert pos["cam3"].y() < pos["cam0"].y() and pos["cam3"].x() == pos["cam0"].x()  # slot 0 vs slot 2 (row 1)
    assert g.slot_widget(1) is g.empty_tiles()[0]


@pytest.mark.parametrize("garbage", ["cam0,cam1", 17, {"a": 1}, [1, 2, {"x": None}], ["ghost", "cam9", 3.5]])
def test_garbage_grid_slots_setting_is_handled(make_env, qtbot, garbage):
    env = make_env(2, settings={"grid_slots": garbage}, **SLOW)
    w = open_window(env, qtbot)
    g = grid(w)
    slots = g.slots()
    assert len(slots) == 25
    assert all(s in (None, "cam0", "cam1") for s in slots)
    if not isinstance(garbage, list):
        assert g.visible_slots() == ["cam0", "cam1", None, None]  # not a list: default layout
    assert streaming(env) == {s for s in g.visible_slots() if s}


def test_unknown_ids_dropped_and_layout_change_persists(make_env, qtbot, tmp_path):
    env = make_env(3, settings={"grid_slots": ["ghost", "cam2", "cam2", "cam1"]}, **SLOW)
    w = open_window(env, qtbot)
    g = grid(w)
    assert g.visible_slots() == [None, "cam2", None, "cam1"]
    g.place_camera(0, "cam0")
    assert _saved_slots(tmp_path) == _pad("cam0", "cam2", None, "cam1")


# --------------------------------------------------------------------------- empty slots
def test_empty_slot_tile_text_and_no_stream(make_env, qtbot):
    from tapo_desktop.ui.video_tile import EmptySlotTile

    env = make_env(1, settings={"grid_size": 2}, **SLOW)
    w = open_window(env, qtbot)
    empties = grid(w).empty_tiles()
    assert len(empties) == 3
    for e in empties:
        assert isinstance(e, EmptySlotTile)
        assert e.objectName() == "emptySlot"
        assert e.status_text() == "Empty"
        assert e.hint_text() == "Right-click or drag a camera here"
    assert streaming(env) == {"cam0"}
    assert [s.config.id for s in env.factory.created if s.calls_to("start_stream")] == ["cam0"]


def test_empty_slot_menu_offers_show_camera_only(make_env, qtbot):
    env = make_env(1, settings={"grid_size": 2}, **SLOW)
    w = open_window(env, qtbot)
    g = grid(w)
    menu = g.slot_menu(2)
    assert isinstance(menu, QMenu)
    assert not action(menu, "removeFromViewAction").isEnabled()
    assert not action(menu, "fullScreenAction").isEnabled()
    action(submenu(menu, "showCameraMenu"), "showCamera_cam0").trigger()
    assert g.visible_slots() == [None, None, "cam0", None]
    env.idle(qtbot)
    assert streaming(env) == {"cam0"}  # moved, still streaming exactly once


# --------------------------------------------------------------------------- tile menu
def test_remove_from_view_empties_slot_and_stops_stream(make_env, qtbot, tmp_path):
    env = make_env(3, **SLOW)
    w = open_window(env, qtbot)
    g = grid(w)
    menu = g.slot_menu(1)
    assert action(menu, "removeFromViewAction").text() == "Remove from view"
    action(menu, "removeFromViewAction").trigger()
    assert g.visible_slots() == ["cam0", None, "cam2", None]
    assert not g.tile("cam1").isVisible()
    assert g.slot_widget(1).status_text() == "Empty"
    env.idle(qtbot)
    assert streaming(env) == {"cam0", "cam2"}
    assert env.svc("cam1").calls_to("stop_stream")
    assert _saved_slots(tmp_path) == _pad("cam0", None, "cam2")
    assert [c.id for c in env.controller.cameras()] == ["cam0", "cam1", "cam2"]  # the camera itself stays


def test_show_camera_submenu_lists_all_cameras_and_replaces(make_env, qtbot):
    env = make_env(6, **SLOW)  # 2x2: cam4, cam5 hidden
    w = open_window(env, qtbot)
    g = grid(w)
    menu = g.slot_menu(1)
    sub = submenu(menu, "showCameraMenu")
    assert sub.title() == "Show camera"
    names = [a.text() for a in sub.actions() if a.objectName().startswith("showCamera_")]
    assert names == [f"Cam {i}" for i in range(6)]
    assert action(sub, "showCamera_cam1").isChecked()
    action(sub, "showCamera_cam5").trigger()
    assert g.visible_slots() == ["cam0", "cam5", "cam2", "cam3"]
    env.idle(qtbot)
    assert streaming(env) == {"cam0", "cam5", "cam2", "cam3"}
    assert env.svc("cam1").calls_to("stop_stream")
    assert env.svc("cam5").calls_to("start_stream")[-1].args == (StreamQuality.SD, 640, 360)


def test_show_camera_already_visible_swaps(make_env, qtbot):
    env = make_env(4, **SLOW)
    w = open_window(env, qtbot)
    g = grid(w)
    starts_before = {c: len(env.svc(c).calls_to("start_stream")) for c in ("cam0", "cam3")}
    action(submenu(g.slot_menu(0), "showCameraMenu"), "showCamera_cam3").trigger()
    assert g.visible_slots() == ["cam3", "cam1", "cam2", "cam0"]
    env.idle(qtbot)
    assert streaming(env) == {"cam0", "cam1", "cam2", "cam3"}
    # a swap between visible slots does not restart either stream
    assert {c: len(env.svc(c).calls_to("start_stream")) for c in ("cam0", "cam3")} == starts_before
    assert not env.svc("cam0").calls_to("stop_stream") and not env.svc("cam3").calls_to("stop_stream")


def test_full_screen_action_and_double_click_with_slots(make_env, qtbot):
    env = make_env(4, settings={"grid_slots": ["cam2", "cam0"]}, **SLOW)
    w = open_window(env, qtbot)
    g = grid(w)
    fs = action(g.slot_menu(0), "fullScreenAction")
    assert fs.text() == "Full screen"
    fs.trigger()
    assert g.fullscreen_camera_id() == "cam2"
    assert visible_ids(w) == ["cam2"]
    assert not any(e.isVisible() for e in g.empty_tiles())
    qtbot.waitUntil(lambda: bool(env.svc("cam2").calls_to("start_stream")[-1].args[0] is StreamQuality.HD),
                    timeout=3000)
    env.idle(qtbot)
    assert streaming(env) == {"cam2"}
    qtbot.mouseDClick(g.tile("cam2"), Qt.MouseButton.LeftButton)
    assert g.fullscreen_camera_id() is None
    env.idle(qtbot)
    assert streaming(env) == {"cam0", "cam2"}
    assert env.svc("cam2").calls_to("start_stream")[-1].args[0] is StreamQuality.SD
    qtbot.mouseDClick(g.tile("cam0"), Qt.MouseButton.LeftButton)
    assert g.fullscreen_camera_id() == "cam0"
    g.remove_from_view("cam0")  # leaving the view also leaves fullscreen
    assert g.fullscreen_camera_id() is None
    assert g.visible_slots() == ["cam2", None, None, None]


def test_slots_follow_grid_size(make_env, qtbot):
    env = make_env(12, settings={"grid_size": 3}, **SLOW)
    w = open_window(env, qtbot)
    g = grid(w)
    g.place_camera(0, "cam8")  # swap cam0 <-> cam8
    w.set_grid_size(2)
    assert g.visible_slots() == ["cam8", "cam1", "cam2", "cam3"]
    env.idle(qtbot)
    assert streaming(env) == {"cam8", "cam1", "cam2", "cam3"}
    w.set_grid_size(3)
    assert g.visible_slots() == ["cam8", "cam1", "cam2", "cam3", "cam4", "cam5", "cam6", "cam7", "cam0"]


# --------------------------------------------------------------------------- camera list menu + marks
def test_list_menu_add_to_view_uses_first_empty_slot(make_env, qtbot, tmp_path):
    env = make_env(3, settings={"grid_slots": ["cam0", None, "cam1"]}, **SLOW)
    w = open_window(env, qtbot)
    g = grid(w)
    menu = w.camera_list_menu("cam2")
    add, remove = action(menu, "addToViewAction"), action(menu, "removeFromViewAction")
    assert add.text() == "Add to view" and remove.text() == "Remove from view"
    assert add.isEnabled() and not remove.isEnabled()
    add.trigger()
    assert g.visible_slots() == ["cam0", "cam2", "cam1", None]
    env.idle(qtbot)
    assert "cam2" in streaming(env)
    assert _saved_slots(tmp_path)[:4] == ["cam0", "cam2", "cam1", None]
    menu = w.camera_list_menu("cam2")
    assert not action(menu, "addToViewAction").isEnabled()
    action(menu, "removeFromViewAction").trigger()
    assert g.visible_slots() == ["cam0", None, "cam1", None]
    env.idle(qtbot)
    assert "cam2" not in streaming(env)


def test_list_menu_add_to_full_view_replaces_selected_tile(make_env, qtbot):
    env = make_env(6, **SLOW)  # 2x2 full; cam4/cam5 hidden
    w = open_window(env, qtbot)
    g = grid(w)
    qtbot.mouseClick(g.tile("cam2"), Qt.MouseButton.LeftButton)
    assert g.selected_camera_id() == "cam2"
    camera_list(w).setCurrentRow(5)  # selecting the hidden row (as a right-click does) keeps the tile target
    assert g.selected_camera_id() == "cam5"
    action(w.camera_list_menu("cam5"), "addToViewAction").trigger()
    assert g.visible_slots() == ["cam0", "cam1", "cam5", "cam3"]
    env.idle(qtbot)
    assert streaming(env) == {"cam0", "cam1", "cam5", "cam3"}


def test_hidden_cameras_are_marked_in_list(make_env, qtbot):
    from tapo_desktop.ui.main_window import IN_VIEW_ROLE, STATUS_ROLE

    cams = [make_config(0), make_config(1), make_config(2, host="", lan_found=False)]
    env = make_env(cameras=cams, settings={"grid_size": 1}, **SLOW)
    w = open_window(env, qtbot)
    r0, r1, r2 = (_row(w, c) for c in ("cam0", "cam1", "cam2"))
    assert r0.data(IN_VIEW_ROLE) is True and not r0.font().italic()
    assert r1.data(IN_VIEW_ROLE) is False and r1.font().italic()
    assert r1.text() == "Cam 1"  # dimmed/italic, text unchanged
    assert r2.font().italic() and r2.data(STATUS_ROLE) and r2.text().startswith("Cam 2 (")  # status kept
    assert r1.foreground().color() != r0.foreground().color()
    w.set_grid_size(2)
    qtbot.waitUntil(lambda: bool(_row(w, "cam1").data(IN_VIEW_ROLE) is True), timeout=2000)
    assert not _row(w, "cam1").font().italic()
    grid(w).remove_from_view("cam0")
    qtbot.waitUntil(lambda: bool(_row(w, "cam0").font().italic()), timeout=2000)
    assert "not in the current view" in _row(w, "cam0").toolTip().lower()


def test_list_context_menu_policy(make_env, qtbot):
    env = make_env(2)
    w = open_window(env, qtbot)
    assert camera_list(w).contextMenuPolicy() == Qt.ContextMenuPolicy.CustomContextMenu
    for t in grid(w).tiles() + grid(w).empty_tiles():
        assert t.contextMenuPolicy() == Qt.ContextMenuPolicy.CustomContextMenu


# --------------------------------------------------------------------------- new / removed cameras
def test_new_camera_fills_first_empty_slot(make_env, qtbot, monkeypatch):
    env = make_env(3, settings={"grid_slots": ["cam0", None, "cam1"]}, **SLOW)
    w = open_window(env, qtbot)
    g = grid(w)
    env.controller.add_camera(make_config(7, name="Shed"), CLOUD, CAMERA)
    qtbot.waitUntil(lambda: bool(g.visible_slots() == ["cam0", "cam7", "cam1", None]), timeout=3000)
    env.idle(qtbot)
    assert "cam7" in streaming(env)
    assert g.slot_of("cam2") is None  # known hidden camera stays hidden


def test_new_camera_without_free_visible_slot_does_not_stream(make_env, qtbot):
    env = make_env(4, **SLOW)  # 2x2 full; slots 4.. empty
    w = open_window(env, qtbot)
    g = grid(w)
    env.controller.add_camera(make_config(9), CLOUD, CAMERA)
    qtbot.waitUntil(lambda: bool(g.slot_of("cam9") == 4), timeout=3000)
    env.idle(qtbot)
    assert "cam9" not in streaming(env)
    assert not _row(w, "cam9").data(Qt.ItemDataRole.UserRole + 2)


def test_removed_camera_leaves_empty_slot(make_env, qtbot, tmp_path):
    env = make_env(3, **SLOW)
    w = open_window(env, qtbot)
    g = grid(w)
    env.controller.remove_camera("cam1")
    qtbot.waitUntil(lambda: bool(g.visible_slots() == ["cam0", None, "cam2", None]), timeout=3000)
    assert [e.slot_index for e in g.empty_tiles()] == [1, 3]
    assert _saved_slots(tmp_path)[:3] == ["cam0", None, "cam2"]
    env.controller.add_camera(make_config(1), CLOUD, CAMERA)  # back again: new -> first empty slot
    qtbot.waitUntil(lambda: bool(g.visible_slots() == ["cam0", "cam1", "cam2", None]), timeout=3000)


def test_removed_from_view_camera_stays_hidden_after_restart(make_env, qtbot, tmp_path):
    env = make_env(3, **SLOW)
    w = open_window(env, qtbot)
    grid(w).remove_from_view("cam0")
    w.hide()
    env.controller.stop_all()
    env2 = make_env(3, settings={"grid_slots": _saved_slots(tmp_path)}, **SLOW)
    w2 = open_window(env2, qtbot)
    assert grid(w2).visible_slots() == [None, "cam1", "cam2", None]
    assert "cam0" not in streaming(env2)


# --------------------------------------------------------------------------- remote gate with slots
def test_remote_click_to_view_tile_in_moved_slot_still_promotes(make_env, qtbot):
    env = make_env(3, **SLOW)
    w = open_window(env, qtbot)
    g = grid(w)
    promoted: list[str] = []
    env.controller.view_remote_now = promoted.append  # type: ignore[method-assign]
    g.place_camera(3, "cam1")  # move cam1 into the empty slot 3
    env.idle(qtbot)
    tile = g.tile("cam1")
    env.controller.stream_state.emit("cam1", "FAILED", REMOTE_CLICK_TO_VIEW)
    assert tile.status_text() == REMOTE_CLICK_TO_VIEW
    qtbot.mouseClick(tile, Qt.MouseButton.LeftButton)
    assert promoted == ["cam1"]
    g.remove_from_view("cam1")
    env.controller.stream_state.emit("cam1", "FAILED", REMOTE_CLICK_TO_VIEW)
    g.select_camera("cam0")
    g.select_camera("cam1")  # a hidden camera is not promoted (it has no relay request)
    assert promoted == ["cam1"]
