"""Single camera video tile (contract: docs/CONTRACT_UI.md).

objectName ``videoTile_<camera_id>``. Paints the latest frame aspect-fit (letterboxed) and an
overlay headline for non-playing states. ``status_text()`` returns exactly the overlay headline:

* ``CONNECTING`` -> ``"Connecting..."``
* ``RECONNECTING`` -> ``"Reconnecting..."``
* ``FAILED`` / ``STOPPED`` -> ``"Offline"``
* ``PLAYING`` / ``IDLE`` -> ``""`` (overlay hidden; the frame is fully visible)

The reason string (if any) may be shown as a second line / tooltip, never inside ``status_text()``.
Single click emits ``clicked(camera_id)``; double click emits ``double_clicked(camera_id)``.
"""
from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QWidget

OVERLAY_TEXT: dict[str, str] = {
    "IDLE": "",
    "CONNECTING": "Connecting...",
    "PLAYING": "",
    "RECONNECTING": "Reconnecting...",
    "STOPPED": "Offline",
    "FAILED": "Offline",
}


class VideoTile(QWidget):
    clicked = pyqtSignal(str)
    double_clicked = pyqtSignal(str)

    def __init__(self, camera_id: str, name: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        raise NotImplementedError

    @property
    def camera_id(self) -> str:
        raise NotImplementedError

    def set_frame(self, image: QImage) -> None:
        """Show ``image`` (repaint scheduled)."""
        raise NotImplementedError

    def current_image(self) -> QImage | None:
        """The last image passed to ``set_frame`` (lane addition; None before the first frame)."""
        raise NotImplementedError

    def set_stream_state(self, state_name: str, reason: str = "") -> None:
        """Update the overlay from a ``StreamState.name`` (see module docstring)."""
        raise NotImplementedError

    def status_text(self) -> str:
        raise NotImplementedError
