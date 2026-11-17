"""Shared helpers for the Phase 5 grid tests (slots, menus, drag and drop)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QAction  # noqa: E402
from PyQt6.QtWidgets import QListWidget, QMenu, QWidget  # noqa: E402

from tests.e2e.fakes import show_window  # noqa: E402

#: Slow synthetic frames: up to 25 fake streams run at once in these tests.
SLOW = {"frame_interval": 0.25}


def grid(w):
    g = w.findChild(QWidget, "cameraGrid")
    assert g is not None
    return g


def camera_list(w) -> QListWidget:
    lst = w.findChild(QListWidget, "cameraList")
    assert lst is not None
    return lst


def visible_ids(w) -> list[str]:
    return [t.camera_id for t in grid(w).tiles() if t.isVisible()]


def streaming(env) -> set[str]:
    """Camera ids whose fake service currently runs a stream (start_stream without a later stop)."""
    return {cid for cid, svc in env.factory.services.items() if svc.stream_args is not None}


def action(menu: QMenu, name: str) -> QAction:
    found = menu.findChild(QAction, name)
    assert found is not None, f"no action {name!r} in menu"
    return found


def submenu(menu: QMenu, name: str) -> QMenu:
    found = menu.findChild(QMenu, name)
    assert found is not None, f"no submenu {name!r}"
    return found


def open_window(env, qtbot, **kw):
    w = show_window(env, qtbot, **kw)
    env.idle(qtbot)
    return w
