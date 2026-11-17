"""Drag and drop: camera rows from ``cameraList`` onto tiles, tiles onto tiles (swap)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtCore import QMimeData, QPointF, Qt  # noqa: E402
from PyQt6.QtGui import QDragEnterEvent, QDropEvent  # noqa: E402

from tests.phase5._grid_helpers import SLOW, camera_list, grid, open_window, streaming  # noqa: E402

pytestmark = [pytest.mark.ui, pytest.mark.timeout(120)]


def _drop_event(widget, mime: QMimeData, cls=QDropEvent):
    pos = QPointF(widget.width() / 2, widget.height() / 2)
    return cls(pos.toPoint() if cls is QDragEnterEvent else pos, Qt.DropAction.MoveAction, mime,
               Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)


def test_camera_mime_round_trip():
    from tapo_desktop.ui.video_tile import CAMERA_MIME, camera_id_from_mime, camera_mime_data

    mime = camera_mime_data("cam3")
    assert mime.hasFormat(CAMERA_MIME)
    assert camera_id_from_mime(mime) == "cam3"
    assert camera_id_from_mime(QMimeData()) is None
    bad = QMimeData()
    bad.setData(CAMERA_MIME, b"\xff\xfe")
    assert camera_id_from_mime(bad) is None
    text_only = QMimeData()
    text_only.setText("cam3")
    assert camera_id_from_mime(text_only) is None  # plain text drags from other apps are ignored


def test_camera_list_drag_carries_camera_id(make_env, qtbot):
    from tapo_desktop.ui.video_tile import camera_id_from_mime

    env = make_env(3)
    w = open_window(env, qtbot)
    lst = camera_list(w)
    assert lst.dragEnabled()
    mime = lst.mimeData([lst.item(2)])
    assert camera_id_from_mime(mime) == "cam2"


def test_drop_list_camera_onto_tile_replaces_it(make_env, qtbot, tmp_path):
    from tapo_desktop.services.config_store import ConfigStore
    from tapo_desktop.ui.video_tile import camera_mime_data

    env = make_env(6, **SLOW)  # 2x2, cam4/cam5 hidden
    w = open_window(env, qtbot)
    g = grid(w)
    assert g.handle_drop(1, camera_mime_data("cam5")) is True
    assert g.visible_slots() == ["cam0", "cam5", "cam2", "cam3"]
    env.idle(qtbot)
    assert streaming(env) == {"cam0", "cam5", "cam2", "cam3"}
    assert ConfigStore(tmp_path / "config.json").get_setting("grid_slots")[:4] == ["cam0", "cam5", "cam2", "cam3"]


def test_drop_visible_camera_swaps(make_env, qtbot):
    from tapo_desktop.ui.video_tile import camera_mime_data

    env = make_env(4, **SLOW)
    w = open_window(env, qtbot)
    g = grid(w)
    assert g.handle_drop(3, camera_mime_data("cam0")) is True
    assert g.visible_slots() == ["cam3", "cam1", "cam2", "cam0"]


def test_drop_onto_empty_slot_moves_camera(make_env, qtbot):
    from tapo_desktop.ui.video_tile import camera_mime_data

    env = make_env(2, **SLOW)
    w = open_window(env, qtbot)
    g = grid(w)
    assert g.handle_drop(3, camera_mime_data("cam0")) is True
    assert g.visible_slots() == [None, "cam1", None, "cam0"]
    env.idle(qtbot)
    assert streaming(env) == {"cam0", "cam1"}


@pytest.mark.parametrize("payload", [None, "ghost"])
def test_invalid_drops_are_rejected(make_env, qtbot, payload):
    from tapo_desktop.ui.video_tile import camera_mime_data

    env = make_env(2, **SLOW)
    w = open_window(env, qtbot)
    g = grid(w)
    before = g.slots()
    mime = QMimeData() if payload is None else camera_mime_data(payload)
    assert g.handle_drop(0, mime) is False
    assert g.handle_drop(99, camera_mime_data("cam1")) is False
    assert g.slots() == before


def test_tile_drop_event_swaps_tiles(make_env, qtbot):
    from tapo_desktop.ui.video_tile import camera_mime_data

    env = make_env(4, **SLOW)
    w = open_window(env, qtbot)
    g = grid(w)
    target = g.tile("cam1")
    assert target.acceptDrops()
    enter = _drop_event(target, camera_mime_data("cam2"), QDragEnterEvent)
    target.dragEnterEvent(enter)
    assert enter.isAccepted()
    drop = _drop_event(target, camera_mime_data("cam2"))
    target.dropEvent(drop)
    assert drop.isAccepted()
    assert g.visible_slots() == ["cam0", "cam2", "cam1", "cam3"]
    # a tile dropped on itself changes nothing
    target = g.tile("cam2")
    target.dropEvent(_drop_event(target, camera_mime_data("cam2")))
    assert g.visible_slots() == ["cam0", "cam2", "cam1", "cam3"]


def test_empty_slot_drop_event_and_foreign_mime_ignored(make_env, qtbot):
    from tapo_desktop.ui.video_tile import camera_mime_data

    env = make_env(1, **SLOW)
    w = open_window(env, qtbot)
    g = grid(w)
    empty = g.slot_widget(2)
    assert empty.acceptDrops()
    foreign = QMimeData()
    foreign.setText("hello")
    enter = _drop_event(empty, foreign, QDragEnterEvent)
    enter.ignore()
    empty.dragEnterEvent(enter)
    assert not enter.isAccepted()
    empty.dropEvent(_drop_event(empty, camera_mime_data("cam0")))
    assert g.visible_slots() == [None, None, "cam0", None]


def test_tile_drag_payload(make_env, qtbot):
    from tapo_desktop.ui.video_tile import camera_id_from_mime

    env = make_env(2, **SLOW)
    w = open_window(env, qtbot)
    tile = grid(w).tile("cam1")
    assert camera_id_from_mime(tile.drag_mime_data()) == "cam1"
