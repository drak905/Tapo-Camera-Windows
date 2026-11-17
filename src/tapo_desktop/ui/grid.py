"""Camera grid (contract: docs/CONTRACT_UI.md). objectName ``cameraGrid``.

* ``set_grid_size(n)`` (n in 1, 2, 3; anything else -> ValueError) shows ``n*n`` slots; the first
  ``n*n`` cameras of ``controller.cameras()`` get a visible :class:`VideoTile`, the rest are hidden.
* Visible tiles are streamed: ``controller.start_view(id, config.grid_quality, w, h)``; tiles that
  become hidden get ``controller.stop_view(id)``.
* ``controller.frame_ready`` -> ``tile.set_frame``; ``controller.stream_state`` ->
  ``tile.set_stream_state``. ``controller.cameras_changed`` rebuilds the tiles.
* Click on a tile selects it (``camera_selected`` emitted). Double click -> single-camera view:
  only that tile is visible and it is re-requested with ``config.fullscreen_quality``; double click
  again -> back to the grid with ``config.grid_quality``.
"""
from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QWidget

from tapo_desktop.ui.video_tile import VideoTile


class CameraGrid(QWidget):
    camera_selected = pyqtSignal(str)   # lane addition: emitted when the selection changes

    def __init__(self, controller, parent: QWidget | None = None):
        super().__init__(parent)
        raise NotImplementedError

    def tiles(self) -> list[VideoTile]:
        """All tiles, one per camera, in camera order (hidden ones included)."""
        raise NotImplementedError

    def set_grid_size(self, n: int) -> None:
        raise NotImplementedError

    def grid_size(self) -> int:
        raise NotImplementedError

    def selected_camera_id(self) -> str | None:
        raise NotImplementedError

    def select_camera(self, camera_id: str | None) -> None:
        """Programmatic selection (used to sync with ``cameraList``); emits ``camera_selected``."""
        raise NotImplementedError
