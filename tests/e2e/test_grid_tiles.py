"""CameraGrid + VideoTile: frames, overlays, selection, fullscreen toggle (docs/CONTRACT_UI.md)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtGui import QColor, QImage  # noqa: E402
from PyQt6.QtWidgets import QListWidget, QWidget  # noqa: E402

from tapo_desktop.models import StreamQuality  # noqa: E402
from tapo_desktop.ui.video_tile import VideoTile  # noqa: E402
from tests.e2e.fakes import DEFAULT_COLOR, show_window  # noqa: E402

pytestmark = pytest.mark.ui

GREEN = (20, 200, 40)


def _rgb(c: QColor) -> tuple[int, int, int]:
    return (c.red(), c.green(), c.blue())


def _close(a, b, tol=6) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def _solid(w, h, rgb) -> QImage:
    img = QImage(w, h, QImage.Format.Format_RGB888)
    img.fill(QColor(*rgb))
    return img


def _grid(w):
    return w.findChild(QWidget, "cameraGrid")


def _tile(w, cam_id) -> VideoTile:
    t = w.findChild(VideoTile, f"videoTile_{cam_id}")
    assert t is not None, cam_id
    return t


# --------------------------------------------------------------------------- VideoTile alone
def test_tile_identity_and_signals(qtbot):
    tile = VideoTile("camX", "Hall")
    qtbot.addWidget(tile)
    tile.resize(200, 120)
    tile.show()
    qtbot.waitExposed(tile)
    assert tile.camera_id == "camX"
    assert tile.objectName() == "videoTile_camX"
    assert tile.current_image() is None
    with qtbot.waitSignal(tile.clicked, timeout=1000) as blocker:
        qtbot.mouseClick(tile, Qt.MouseButton.LeftButton)
    assert blocker.args == ["camX"]
    with qtbot.waitSignal(tile.double_clicked, timeout=1000) as blocker:
        qtbot.mouseDClick(tile, Qt.MouseButton.LeftButton)
    assert blocker.args == ["camX"]


def test_tile_paints_frame_pixels(qtbot):
    tile = VideoTile("camX", "Hall")
    qtbot.addWidget(tile)
    tile.resize(320, 180)
    tile.show()
    qtbot.waitExposed(tile)
    tile.set_stream_state("PLAYING")
    img = _solid(160, 90, GREEN)
    tile.set_frame(img)
    assert tile.current_image() is not None
    assert tile.current_image().size() == img.size()
    qtbot.wait(50)
    shot = tile.grab().toImage()
    assert _close(_rgb(shot.pixelColor(shot.width() // 2, shot.height() // 2)), GREEN)
    assert _close(_rgb(shot.pixelColor(shot.width() // 4, shot.height() // 2)), GREEN)


def test_tile_letterboxes_aspect_fit(qtbot):
    tile = VideoTile("camX")
    qtbot.addWidget(tile)
    tile.resize(400, 100)  # much wider than 16:9 -> bars left/right
    tile.show()
    qtbot.waitExposed(tile)
    tile.set_stream_state("PLAYING")
    tile.set_frame(_solid(160, 90, GREEN))
    qtbot.wait(50)
    shot = tile.grab().toImage()
    assert _close(_rgb(shot.pixelColor(shot.width() // 2, shot.height() // 2)), GREEN)
    assert not _close(_rgb(shot.pixelColor(3, shot.height() // 2)), GREEN), "expected letterbox bar"


@pytest.mark.parametrize("state,text", [
    ("CONNECTING", "Connecting..."), ("RECONNECTING", "Reconnecting..."), ("FAILED", "Offline"),
    ("STOPPED", "Offline"), ("PLAYING", ""), ("IDLE", ""),
])
def test_tile_overlay_text(qtbot, state, text):
    tile = VideoTile("camX")
    qtbot.addWidget(tile)
    tile.set_stream_state(state, "some reason")
    assert tile.status_text() == text


def test_tile_overlay_changes_pixels(qtbot):
    tile = VideoTile("camX")
    qtbot.addWidget(tile)
    tile.resize(320, 180)
    tile.show()
    qtbot.waitExposed(tile)
    tile.set_frame(_solid(160, 90, GREEN))
    tile.set_stream_state("PLAYING")
    qtbot.wait(30)
    playing = tile.grab().toImage()
    tile.set_stream_state("RECONNECTING", "timeout")
    qtbot.wait(30)
    reconnecting = tile.grab().toImage()
    assert playing != reconnecting, "overlay must be painted for RECONNECTING"


# --------------------------------------------------------------------------- grid via controller
def test_grid_tile_shows_controller_frames(make_env, qtbot):
    env = make_env(1, settings={"grid_size": 1})
    w = show_window(env, qtbot)
    tile = _tile(w, "cam0")
    qtbot.waitUntil(lambda: bool(tile.current_image() is not None), timeout=3000)
    quality, width, height = env.svc("cam0").calls_to("start_stream")[-1].args
    assert quality is StreamQuality.SD
    assert (tile.current_image().width(), tile.current_image().height()) == (width, height)
    qtbot.waitUntil(lambda: bool(tile.status_text() == ""), timeout=3000)
    qtbot.wait(60)
    shot = tile.grab().toImage()
    assert _close(_rgb(shot.pixelColor(shot.width() // 2, shot.height() // 2)), DEFAULT_COLOR)


@pytest.mark.parametrize("state,reason,text", [("RECONNECTING", "stall", "Reconnecting..."),
                                               ("FAILED", "auth failed", "Offline")])
def test_grid_tile_overlay_from_stream_state(make_env, qtbot, state, reason, text):
    env = make_env(1, settings={"grid_size": 1})
    w = show_window(env, qtbot)
    tile = _tile(w, "cam0")
    qtbot.waitUntil(lambda: bool(tile.current_image() is not None and tile.status_text() == ""), timeout=3000)
    env.svc("cam0").emit_state(state, reason)
    qtbot.waitUntil(lambda: bool(tile.status_text() == text), timeout=3000)


def test_click_tile_selects_camera(make_env, qtbot):
    env = make_env(4)
    w = show_window(env, qtbot)
    grid = _grid(w)
    with qtbot.waitSignal(grid.camera_selected, timeout=2000) as blocker:
        qtbot.mouseClick(_tile(w, "cam2"), Qt.MouseButton.LeftButton)
    assert blocker.args == ["cam2"]
    assert grid.selected_camera_id() == "cam2"
    lst = w.findChild(QListWidget, "cameraList")
    qtbot.waitUntil(lambda: bool(lst.currentRow() == 2), timeout=2000)


def test_double_click_toggles_fullscreen_quality(make_env, qtbot):
    env = make_env(4, settings={"grid_size": 2})
    w = show_window(env, qtbot)
    grid = _grid(w)
    qtbot.waitUntil(lambda: bool(env.has_svc("cam1") and env.svc("cam1").calls_to("start_stream")), timeout=3000)

    def visible():
        return [t.camera_id for t in grid.tiles() if t.isVisible()]

    assert visible() == ["cam0", "cam1", "cam2", "cam3"]
    qtbot.mouseDClick(_tile(w, "cam1"), Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: bool(env.svc("cam1").calls_to("start_stream")[-1].args[0] is StreamQuality.HD),
                    timeout=3000)
    qtbot.waitUntil(lambda: bool(visible() == ["cam1"]), timeout=2000)
    qtbot.mouseDClick(_tile(w, "cam1"), Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: bool(env.svc("cam1").calls_to("start_stream")[-1].args[0] is StreamQuality.SD),
                    timeout=3000)
    qtbot.waitUntil(lambda: bool(visible() == ["cam0", "cam1", "cam2", "cam3"]), timeout=2000)


def test_grid_set_grid_size_validates(make_env, qtbot):
    from tapo_desktop.ui.grid import CameraGrid

    env = make_env(2)
    grid = CameraGrid(env.controller)
    qtbot.addWidget(grid)
    assert grid.objectName() == "cameraGrid"
    assert [t.camera_id for t in grid.tiles()] == ["cam0", "cam1"]
    grid.set_grid_size(1)
    assert grid.grid_size() == 1
    with pytest.raises(ValueError):
        grid.set_grid_size(5)
