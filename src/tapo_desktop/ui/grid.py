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
* Phase 3: a tile showing "Remote - click to view" (remote stream limit reached) is started with
  ``controller.view_remote_now(id)`` when it is clicked or selected.

Decode sizes are fixed per quality (:func:`view_size`) rather than following the tile's pixel size,
so resizing the window never restarts ffmpeg; tiles scale the frame aspect-fit.
"""
from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QGridLayout, QWidget

from tapo_desktop.models import CameraConfig, StreamQuality
from tapo_desktop.ui.video_tile import VideoTile

GRID_SIZES: tuple[int, ...] = (1, 2, 3)
_HD_QUALITIES = (StreamQuality.HD, StreamQuality.TELE_HD)


def view_size(quality: StreamQuality) -> tuple[int, int]:
    """Decoded frame size requested for ``quality`` (16:9; the supervisor letterboxes)."""
    return (1280, 720) if quality in _HD_QUALITIES else (640, 360)


def validate_grid_size(n: object) -> int:
    """Return ``n`` if it is one of :data:`GRID_SIZES`, else raise ValueError."""
    if isinstance(n, bool) or not isinstance(n, int) or n not in GRID_SIZES:
        raise ValueError(f"grid size must be one of {GRID_SIZES}, got {n!r}")
    return n


class CameraGrid(QWidget):
    camera_selected = pyqtSignal(str)   # lane addition: emitted when the selection changes ("" = none)

    def __init__(self, controller, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("cameraGrid")
        self._ctrl = controller
        self._n = 2
        self._tiles: dict[str, VideoTile] = {}
        self._configs: dict[str, CameraConfig] = {}
        self._selected: str | None = None
        self._fullscreen: str | None = None
        self._requested: dict[str, tuple[StreamQuality, int, int]] = {}
        self._placeholders: list[QWidget] = []
        self._layout = QGridLayout(self)
        self._layout.setContentsMargins(4, 4, 4, 4)
        self._layout.setSpacing(4)
        controller.frame_ready.connect(self._on_frame)
        controller.stream_state.connect(self._on_state)
        controller.cameras_changed.connect(self._rebuild)
        self._rebuild()

    # ------------------------------------------------------------------ public API
    def tiles(self) -> list[VideoTile]:
        """All tiles, one per camera, in camera order (hidden ones included)."""
        return list(self._tiles.values())

    def tile(self, camera_id: str) -> VideoTile | None:
        return self._tiles.get(camera_id)

    def set_grid_size(self, n: int) -> None:
        self._n = validate_grid_size(n)
        self._fullscreen = None
        self._apply()

    def grid_size(self) -> int:
        return self._n

    def fullscreen_camera_id(self) -> str | None:
        """Camera shown alone (double-click view), or None in grid mode."""
        return self._fullscreen

    def selected_camera_id(self) -> str | None:
        return self._selected

    def select_camera(self, camera_id: str | None) -> None:
        """Programmatic selection (used to sync with ``cameraList``); emits ``camera_selected``.
        Selecting a remote tile that waits for a relay slot starts it (:meth:`_promote`)."""
        new = camera_id if camera_id in self._tiles else None
        if new == self._selected:
            return
        self._selected = new
        for cid, tile in self._tiles.items():
            tile.set_selected(cid == new)
        if new is not None:
            self._promote(new)
        self.camera_selected.emit(new or "")

    def _on_tile_clicked(self, camera_id: str) -> None:
        self._promote(camera_id)
        self.select_camera(camera_id)

    def _promote(self, camera_id: str) -> None:
        """Start a remote tile showing "Remote - click to view" now (the controller hands it the relay
        slot of the remote stream that has been running longest)."""
        tile = self._tiles.get(camera_id)
        promote = getattr(self._ctrl, "view_remote_now", None)
        if tile is None or not tile.waiting_for_click() or camera_id not in self._requested or not callable(promote):
            return
        tile.set_stream_state("CONNECTING")
        promote(camera_id)

    def toggle_fullscreen(self, camera_id: str) -> None:
        """Show ``camera_id`` alone in fullscreen quality, or return to the grid."""
        if camera_id not in self._tiles:
            return
        self._fullscreen = None if self._fullscreen == camera_id else camera_id
        self._apply()

    # ------------------------------------------------------------------ controller signals
    def _on_frame(self, camera_id: str, image: QImage) -> None:
        tile = self._tiles.get(camera_id)
        if tile is not None and camera_id in self._requested:
            tile.set_frame(image)

    def _on_state(self, camera_id: str, state: str, reason: str) -> None:
        tile = self._tiles.get(camera_id)
        if tile is not None:
            tile.set_stream_state(state, reason)

    def _rebuild(self) -> None:
        cams = self._ctrl.cameras()
        self._configs = {c.id: c for c in cams}
        for cid in [c for c in self._tiles if c not in self._configs]:
            tile = self._tiles.pop(cid)
            self._requested.pop(cid, None)  # the controller stopped it while removing the camera
            tile.hide()
            tile.setParent(None)
            tile.deleteLater()
        tiles: dict[str, VideoTile] = {}
        for cfg in cams:
            tile = self._tiles.get(cfg.id)
            if tile is None:
                tile = VideoTile(cfg.id, cfg.name, self)
                tile.hide()
                tile.clicked.connect(self._on_tile_clicked)
                tile.double_clicked.connect(self._on_double_click)
            else:
                tile.set_name(cfg.name)
            tiles[cfg.id] = tile
        self._tiles = tiles
        if self._fullscreen not in tiles:
            self._fullscreen = None
        self._apply()
        if self._selected is not None and self._selected not in tiles:
            self.select_camera(None)

    def _on_double_click(self, camera_id: str) -> None:
        self.select_camera(camera_id)
        self.toggle_fullscreen(camera_id)

    # ------------------------------------------------------------------ layout + streams
    def _visible_ids(self) -> list[str]:
        if self._fullscreen is not None:
            return [self._fullscreen]
        return list(self._tiles)[: self._n * self._n]

    def _apply(self) -> None:
        visible = self._visible_ids()
        self._relayout(visible)
        self._sync_streams(set(visible))

    def _relayout(self, visible: list[str]) -> None:
        while self._layout.count():
            self._layout.takeAt(0)
        for ph in self._placeholders:
            ph.setParent(None)
            ph.deleteLater()
        self._placeholders.clear()
        cols = 1 if self._fullscreen is not None else self._n
        for index, cid in enumerate(visible):
            row, col = divmod(index, cols)
            self._layout.addWidget(self._tiles[cid], row, col)
            self._tiles[cid].show()
        for index in range(len(visible), cols * cols):
            ph = QWidget(self)
            ph.setObjectName("emptySlot")
            ph.setMinimumSize(160, 90)
            self._placeholders.append(ph)
            self._layout.addWidget(ph, *divmod(index, cols))
        for cid, tile in self._tiles.items():
            if cid not in visible:
                tile.hide()
        for i in range(max(GRID_SIZES)):
            self._layout.setRowStretch(i, 1 if i < cols else 0)
            self._layout.setColumnStretch(i, 1 if i < cols else 0)

    def _sync_streams(self, visible: set[str]) -> None:
        for cid, tile in self._tiles.items():
            want = None
            if cid in visible:
                cfg = self._configs[cid]
                quality = cfg.fullscreen_quality if cid == self._fullscreen else cfg.grid_quality
                want = (quality, *view_size(quality))
            if want == self._requested.get(cid):
                continue
            if want is None:
                del self._requested[cid]
                self._ctrl.stop_view(cid)
            else:
                if cid not in self._requested:  # fresh start (a quality switch keeps the picture)
                    tile.set_stream_state("CONNECTING")
                self._requested[cid] = want
                self._ctrl.start_view(cid, *want)

    # ------------------------------------------------------------------ Phase 5 stubs (red)
    def slots(self):
        raise NotImplementedError

    def visible_slots(self):
        raise NotImplementedError

    def empty_tiles(self):
        raise NotImplementedError

    def slot_widget(self, index):
        raise NotImplementedError

    def slot_of(self, camera_id):
        raise NotImplementedError

    def place_camera(self, index, camera_id):
        raise NotImplementedError

    def remove_from_view(self, camera_id):
        raise NotImplementedError

    def add_to_view(self, camera_id):
        raise NotImplementedError

    def slot_menu(self, index):
        raise NotImplementedError

    def handle_drop(self, index, mime):
        raise NotImplementedError
