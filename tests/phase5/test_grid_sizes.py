"""4x4 and 5x5 grids: buttons, tile counts, persistence, decode sizes, compact tiles (docs/CONTRACT_UI.md)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtWidgets import QAbstractButton  # noqa: E402

from tapo_desktop.models import StreamQuality  # noqa: E402
from tapo_desktop.services.config_store import ConfigStore  # noqa: E402
from tests.e2e.fakes import make_config  # noqa: E402
from tests.phase5._grid_helpers import SLOW, grid, open_window, streaming, visible_ids  # noqa: E402

pytestmark = [pytest.mark.ui, pytest.mark.timeout(120)]


def _btn(w, name) -> QAbstractButton:
    b = w.findChild(QAbstractButton, name)
    assert b is not None, name
    return b


def test_grid_sizes_constant_and_validation():
    from tapo_desktop.ui.grid import GRID_SIZES, validate_grid_size

    assert GRID_SIZES == (1, 2, 3, 4, 5)
    for n in GRID_SIZES:
        assert validate_grid_size(n) == n
    for bad in (0, 6, 7, -1, True, "3", 2.0, None):
        with pytest.raises(ValueError):
            validate_grid_size(bad)


def test_all_layout_buttons_present_in_order(make_env, qtbot):
    env = make_env(1)
    w = open_window(env, qtbot)
    buttons = [_btn(w, f"layout{n}x{n}Button") for n in range(1, 6)]
    xs = [b.mapTo(w, b.rect().topLeft()).x() for b in buttons]
    assert xs == sorted(xs), "4x4/5x5 must sit next to (after) 1x1/2x2/3x3"
    assert [b.text() for b in buttons] == [f"{n}\u00d7{n}" for n in range(1, 6)]


@pytest.mark.parametrize("button,n", [("layout4x4Button", 4), ("layout5x5Button", 5)])
def test_big_layout_buttons_set_tile_count_and_persist(make_env, qtbot, tmp_path, button, n):
    env = make_env(26, settings={"grid_size": 2}, **SLOW)
    w = open_window(env, qtbot)
    _btn(w, button).click()
    qtbot.waitUntil(lambda: bool(len(visible_ids(w)) == n * n), timeout=3000)
    assert w.grid_size() == n
    assert _btn(w, button).isChecked()
    assert visible_ids(w) == [f"cam{i}" for i in range(n * n)]
    assert ConfigStore(tmp_path / "config.json").get_setting("grid_size") == n
    env.idle(qtbot)
    assert streaming(env) == {f"cam{i}" for i in range(n * n)}  # never more streams than tiles shown


def test_saved_5x5_is_restored(make_env, qtbot):
    env = make_env(3, settings={"grid_size": 5}, **SLOW)
    w = open_window(env, qtbot)
    assert w.grid_size() == 5
    assert _btn(w, "layout5x5Button").isChecked()
    g = grid(w)
    assert visible_ids(w) == ["cam0", "cam1", "cam2"]
    empties = g.empty_tiles()
    assert len(empties) == 22
    assert [e.slot_index for e in empties] == list(range(3, 25))
    assert all(e.isVisible() for e in empties)


def test_5x5_grid_streams_sd_and_only_visible_cameras(make_env, qtbot):
    env = make_env(10, settings={"grid_size": 5}, **SLOW)
    w = open_window(env, qtbot)
    qtbot.waitUntil(lambda: bool(streaming(env) == {f"cam{i}" for i in range(10)}), timeout=5000)
    for i in range(10):
        quality, width, height = env.svc(f"cam{i}").calls_to("start_stream")[-1].args
        assert quality is StreamQuality.SD
        assert (width, height) == (640, 360)  # grid tiles keep the SD/VGA decode size
    w.set_grid_size(1)
    env.idle(qtbot)
    assert streaming(env) == {"cam0"}
    w.set_grid_size(4)
    env.idle(qtbot)
    assert streaming(env) == {f"cam{i}" for i in range(10)}


def test_tiles_stay_usable_at_5x5(make_env, qtbot):
    long_name = "Backyard garage camera with a really long descriptive name"
    cams = [make_config(i, name=long_name if i == 0 else f"Cam {i}") for i in range(25)]
    env = make_env(cameras=cams, settings={"grid_size": 5}, **SLOW)
    w = open_window(env, qtbot)
    g = grid(w)
    tile = g.tile("cam0")
    qtbot.waitUntil(lambda: bool(tile.isVisible() and tile.width() > 0), timeout=3000)
    assert tile.width() < 300 and tile.height() < 200, "25 tiles must fit the window"
    assert all(t.isVisible() for t in g.tiles())
    small = tile.overlay_font_sizes()
    w.set_grid_size(1)
    qtbot.waitUntil(lambda: bool(tile.width() > 400), timeout=3000)
    big = tile.overlay_font_sizes()
    assert all(s <= b for s, b in zip(small, big)) and small[0] < big[0], (small, big)
    w.set_grid_size(5)
    qtbot.waitUntil(lambda: bool(tile.width() < 300), timeout=3000)
    shown = tile.display_name()
    assert shown != long_name and shown.endswith("\u2026"), "long names are elided in small tiles"
    tile.set_stream_state("FAILED", "Remote - click to view")
    assert tile.status_text() == "Remote - click to view"
    qtbot.wait(30)
    assert not tile.grab().toImage().isNull()  # paints without errors at the small size


def test_grid_widget_accepts_sizes_up_to_5(make_env, qtbot):
    from tapo_desktop.ui.grid import CameraGrid

    env = make_env(2)
    g = CameraGrid(env.controller)
    qtbot.addWidget(g)
    for n in (4, 5):
        g.set_grid_size(n)
        assert g.grid_size() == n
    with pytest.raises(ValueError):
        g.set_grid_size(6)
    assert g.grid_size() == 5
