"""Camera grid (contract: docs/CONTRACT_UI.md). objectName ``cameraGrid``.

* ``set_grid_size(n)`` (n in 1..5; anything else -> ValueError) shows ``n*n`` positions.
* Phase 5: the positions are the first ``n*n`` slots of a :class:`ViewLayout` (25 slots, setting
  ``"grid_slots"``: camera id or null each; see :mod:`tapo_desktop.services.view_layout`). A slot with a
  camera shows its :class:`VideoTile`; an empty slot shows an :class:`EmptySlotTile` and never streams.
  There is one VideoTile per camera (``tiles()``, camera order); tiles of cameras that are not in a
  visible slot are hidden.
* Visible tiles are streamed: ``controller.start_view(id, config.grid_quality, w, h)``; tiles that
  become hidden get ``controller.stop_view(id)``. Moving a camera between visible slots does not
  restart its stream. At most ``n*n`` streams run (a camera is never in two slots).
* Phase 6 view-driven streaming: what streams is decided by
  :func:`~tapo_desktop.services.stream_policy.plan_streams` - every camera in a visible slot, local and
  remote, streams at the same time in every grid size; the controller reserves relay slots for them, so
  the remote stream limit no longer leaves visible tiles on "Remote - click to view".
* ``controller.frame_ready`` -> ``tile.set_frame``; ``controller.stream_state`` ->
  ``tile.set_stream_state``. ``controller.cameras_changed`` rebuilds the tiles and syncs the slots
  (removed cameras leave an empty slot, new cameras fill the first empty slot).
* Click on a tile selects it (``camera_selected`` emitted). Double click (or "Full screen" in the
  tile menu) -> single-camera view: only that tile is visible and it is re-requested with
  ``config.fullscreen_quality``; double click again -> back to the grid with ``config.grid_quality``.
* Phase 3: a tile showing "Remote - click to view" (remote stream limit reached) is started with
  ``controller.view_remote_now(id)`` when it is clicked or selected.
* Phase 5 tile menu (right click, :meth:`slot_menu`): "Remove from view", "Show camera" submenu (every
  camera; choosing one puts it into this slot, swapping with its previous slot) and "Full screen".
  Drag and drop: a camera (row of ``cameraList`` or another tile) dropped on a position is placed
  there (:meth:`handle_drop`). ``view_changed`` is emitted after every slot / size / fullscreen change.
* Phase 7: the tile menu also has "Rename..." (``renameCameraAction``), which emits ``rename_requested(id)``
  (the main window opens the rename dialog). Renaming never moves a camera to another slot.

Decode sizes are fixed per quality (:func:`view_size`) rather than following the tile's pixel size,
so resizing the window never restarts ffmpeg; tiles scale the frame aspect-fit.
"""
from __future__ import annotations

import logging

from PyQt6.QtCore import QMimeData, QPoint, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QImage
from PyQt6.QtWidgets import QGridLayout, QMenu, QWidget

from tapo_desktop.core.imou.texts import CLICK_TEXT
from tapo_desktop.models import CameraConfig, StreamQuality
from tapo_desktop.services.stream_policy import StreamRequest, plan_streams, view_size
from tapo_desktop.services.view_layout import MAX_SLOTS, SETTING_KEY, ViewLayout
from tapo_desktop.ui import imou_status
from tapo_desktop.ui.kami_status import LIVE_VIEW_UNSUPPORTED_TEXT, control_enabled
from tapo_desktop.ui.vendor_status import capability_service
from tapo_desktop.ui.video_tile import EmptySlotTile, VideoTile, camera_id_from_mime

log = logging.getLogger(__name__)

GRID_SIZES: tuple[int, ...] = (1, 2, 3, 4, 5)

__all__ = ["GRID_SIZES", "CameraGrid", "validate_grid_size", "view_size"]

def validate_grid_size(n: object) -> int:
    """Return ``n`` if it is one of :data:`GRID_SIZES`, else raise ValueError."""
    if isinstance(n, bool) or not isinstance(n, int) or n not in GRID_SIZES:
        raise ValueError(f"grid size must be one of {GRID_SIZES}, got {n!r}")
    return n


class CameraGrid(QWidget):
    camera_selected = pyqtSignal(str)   # lane addition: emitted when the selection changes ("" = none)
    view_changed = pyqtSignal()         # Phase 5: slots, grid size or fullscreen changed
    rename_requested = pyqtSignal(str)  # Phase 7: "Rename..." chosen in a tile menu (camera_id)

    def __init__(self, controller, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("cameraGrid")
        self._ctrl = controller
        self._store = getattr(controller, "config_store", None)
        self._n = 2
        self._tiles: dict[str, VideoTile] = {}
        self._configs: dict[str, CameraConfig] = {}
        self._selected: str | None = None
        self._active_slot: int | None = None  # slot of the last selected visible tile ("Add to view" target)
        self._fullscreen: str | None = None
        self._requested: dict[str, tuple[StreamQuality, int, int]] = {}
        self._manual: set[str] = set()  # click-to-start cameras (iMou) the user started: only these stream
        self._empty: dict[int, EmptySlotTile] = {}
        self._layout = QGridLayout(self)
        self._layout.setContentsMargins(4, 4, 4, 4)
        self._layout.setSpacing(4)
        cams = controller.cameras()
        self._view = ViewLayout([c.id for c in cams], self._load_slots())
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
        self._remember_active_slot()
        if new is not None:
            self._promote(new)
        self.camera_selected.emit(new or "")

    def toggle_fullscreen(self, camera_id: str) -> None:
        """Show ``camera_id`` alone in fullscreen quality, or return to the grid. Only cameras in a
        visible slot can go fullscreen."""
        if camera_id not in self._tiles:
            return
        if self._fullscreen == camera_id:
            self._fullscreen = None
        elif camera_id in self._visible_cameras():
            self._fullscreen = camera_id
            self._start_on_click(camera_id)  # full screen of an idle iMou tile is a click
        else:
            return
        self._apply()

    # ------------------------------------------------------------------ Phase 5: slots
    def slots(self) -> list[str | None]:
        """All 25 slots (camera id or None)."""
        return self._view.slots()

    def visible_slots(self) -> list[str | None]:
        """The ``n*n`` slots of the current grid size (fullscreen does not change them)."""
        return self._view.visible(self._n * self._n)

    def slot_of(self, camera_id: str) -> int | None:
        """Slot index of ``camera_id`` (visible or not), or None if it is not in the view."""
        return self._view.index_of(camera_id)

    def in_view(self, camera_id: str) -> bool:
        """True if ``camera_id`` is in one of the visible slots."""
        return camera_id in self._visible_cameras()

    def empty_tiles(self) -> list[EmptySlotTile]:
        """Empty-slot tiles of the current grid, in slot order (none in fullscreen)."""
        if self._fullscreen is not None:
            return []
        return [self._empty[i] for i, cid in enumerate(self.visible_slots()) if cid is None and i in self._empty]

    def slot_widget(self, index: int) -> VideoTile | EmptySlotTile | None:
        """Widget shown for visible slot ``index`` (None outside the current grid)."""
        visible = self.visible_slots()
        if not 0 <= index < len(visible):
            return None
        cid = visible[index]
        return self._tiles.get(cid) if cid is not None else self._empty.get(index)

    def place_camera(self, index: int, camera_id: str) -> bool:
        """Put ``camera_id`` into slot ``index`` (swap if it already has a slot). False if nothing
        changed or the arguments are invalid."""
        if camera_id not in self._tiles or not 0 <= index < MAX_SLOTS:
            return False
        return self._commit(self._view.place(index, camera_id))

    def swap_slots(self, a: int, b: int) -> bool:
        """Exchange the contents of slots ``a`` and ``b``."""
        if not (0 <= a < MAX_SLOTS and 0 <= b < MAX_SLOTS):
            return False
        return self._commit(self._view.swap(a, b))

    def clear_slot(self, index: int) -> bool:
        """Empty slot ``index`` (its camera's stream stops)."""
        if not 0 <= index < MAX_SLOTS:
            return False
        return self._commit(self._view.clear(index))

    def remove_from_view(self, camera_id: str) -> bool:
        """Take ``camera_id`` out of the view; the camera itself is kept."""
        return self._commit(self._view.remove(camera_id))

    def add_to_view(self, camera_id: str) -> int | None:
        """Show ``camera_id``: first empty visible slot, else the slot of the last selected visible tile,
        else the last visible slot. Returns the slot used (None for an unknown camera)."""
        if camera_id not in self._tiles:
            return None
        before = self._view.slots()
        index = self._view.add(camera_id, self._n * self._n, self._active_slot)
        self._commit(self._view.slots() != before)
        return index

    def handle_drop(self, index: int, mime: QMimeData) -> bool:
        """A drag payload dropped on visible slot ``index``: place its camera there (swap if needed)."""
        camera_id = camera_id_from_mime(mime)
        if camera_id is None or camera_id not in self._tiles or not 0 <= index < self._n * self._n:
            return False
        self.place_camera(index, camera_id)
        return True

    def slot_menu(self, index: int) -> QMenu:
        """Context menu for visible slot ``index`` (the caller shows it; it is deleted on close)."""
        cid = self._view.visible(MAX_SLOTS)[index] if 0 <= index < MAX_SLOTS else None
        menu = QMenu(self)
        menu.setObjectName("slotMenu")
        remove = menu.addAction("Remove from view")
        remove.setObjectName("removeFromViewAction")
        remove.setEnabled(cid is not None)
        remove.triggered.connect(lambda _checked=False, i=index: self.clear_slot(i))
        show = menu.addMenu("Show camera")
        show.setObjectName("showCameraMenu")
        for cfg in self._configs.values():
            act = QAction(cfg.name or cfg.id, show)
            act.setObjectName(f"showCamera_{cfg.id}")
            act.setData(cfg.id)
            act.setCheckable(True)
            act.setChecked(cfg.id == cid)
            act.triggered.connect(lambda _checked=False, i=index, c=cfg.id: self.place_camera(i, c))
            show.addAction(act)
        show.setEnabled(bool(self._configs))
        menu.addSeparator()
        full = menu.addAction("Full screen")
        full.setObjectName("fullScreenAction")
        full.setCheckable(True)
        full.setEnabled(cid is not None)
        full.setChecked(cid is not None and cid == self._fullscreen)
        if cid is not None:
            full.triggered.connect(lambda _checked=False, c=cid: self._full_screen_from_menu(c))
        rename = menu.addAction("Rename\u2026")
        rename.setObjectName("renameCameraAction")
        rename.setEnabled(cid is not None)
        if cid is not None:
            rename.triggered.connect(lambda _checked=False, c=cid: self.rename_requested.emit(c))
        return menu

    # ------------------------------------------------------------------ tile events
    def _on_tile_clicked(self, camera_id: str) -> None:
        self._promote(camera_id)
        self._start_on_click(camera_id)
        self.select_camera(camera_id)

    def _on_double_click(self, camera_id: str) -> None:
        self.select_camera(camera_id)
        self.toggle_fullscreen(camera_id)

    def _full_screen_from_menu(self, camera_id: str) -> None:
        self.select_camera(camera_id)
        self.toggle_fullscreen(camera_id)

    def _on_tile_dropped(self, camera_id: str, target: QWidget) -> None:
        index = self._index_of_widget(target)
        if index is not None:
            self.place_camera(index, camera_id)

    def _show_menu(self, widget: QWidget, pos: QPoint) -> None:
        index = self._index_of_widget(widget)
        if index is None:
            return
        menu = self.slot_menu(index)
        try:
            menu.exec(widget.mapToGlobal(pos))
        finally:
            menu.deleteLater()

    def _index_of_widget(self, widget: QWidget) -> int | None:
        if isinstance(widget, EmptySlotTile):
            index = widget.slot_index
            return index if self.slot_widget(index) is widget else None
        if isinstance(widget, VideoTile):
            index = self._view.index_of(widget.camera_id)
            return index if index is not None and index < self._n * self._n else None
        return None

    def _start_on_click(self, camera_id: str) -> None:
        """A click on a tile of a click-to-start camera (iMou) starts its stream; nothing else ever does."""
        cfg = self._configs.get(camera_id)
        if cfg is None or not imou_status.click_to_start(cfg) or camera_id in self._manual:
            return
        if camera_id in self._visible_cameras():
            self._manual.add(camera_id)
            self._apply()

    def _promote(self, camera_id: str) -> None:
        """Start a remote tile showing "Remote - click to view" now (the controller hands it the relay
        slot of the remote stream that has been running longest)."""
        tile = self._tiles.get(camera_id)
        promote = getattr(self._ctrl, "view_remote_now", None)
        if tile is None or not tile.waiting_for_click() or camera_id not in self._requested or not callable(promote):
            return
        tile.set_stream_state("CONNECTING")
        promote(camera_id)

    # ------------------------------------------------------------------ controller signals
    def _on_frame(self, camera_id: str, image: QImage) -> None:
        tile = self._tiles.get(camera_id)
        if tile is not None and camera_id in self._requested:
            tile.set_frame(image)

    def _on_state(self, camera_id: str, state: str, reason: str) -> None:
        tile = self._tiles.get(camera_id)
        if tile is not None:
            tile.set_stream_state(state, reason)
        if state in ("FAILED", "STOPPED") and camera_id in self._manual:  # an iMou stream ended: ask for a click
            self._manual.discard(camera_id)
            self._requested.pop(camera_id, None)
            self._ctrl.stop_view(camera_id)

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
                tile = self._new_tile(cfg)
            else:
                tile.set_name(cfg.name)
            tiles[cfg.id] = tile
        self._tiles = tiles
        if self._view.sync(list(tiles)):
            self._save_slots()
        if self._fullscreen not in tiles:
            self._fullscreen = None
        self._apply()
        if self._selected is not None and self._selected not in tiles:
            self.select_camera(None)

    def _new_tile(self, cfg: CameraConfig) -> VideoTile:
        tile = VideoTile(cfg.id, cfg.name, self)
        tile.hide()
        tile.set_drag_enabled(True)
        tile.setAcceptDrops(True)
        tile.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tile.clicked.connect(self._on_tile_clicked)
        tile.double_clicked.connect(self._on_double_click)
        tile.camera_dropped.connect(lambda cid, t=tile: self._on_tile_dropped(cid, t))
        tile.customContextMenuRequested.connect(lambda pos, t=tile: self._show_menu(t, pos))
        if self._no_live_view(cfg):
            tile.set_stream_state("FAILED", LIVE_VIEW_UNSUPPORTED_TEXT)
        elif imou_status.click_to_start(cfg):
            tile.set_stream_state("FAILED", CLICK_TEXT)
        return tile

    @staticmethod
    def _no_live_view(cfg: CameraConfig) -> bool:
        """True for a camera whose service cannot show live video (Kami): its tile shows a fixed text and
        never requests a stream (plan-review finding 8). Tapo cameras have no flag, so always False."""
        return not control_enabled(capability_service(cfg), "live_view")

    def _empty_tile(self, index: int) -> EmptySlotTile:
        tile = self._empty.get(index)
        if tile is None:
            tile = EmptySlotTile(index, self)
            tile.hide()
            tile.setAcceptDrops(True)
            tile.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            tile.camera_dropped.connect(lambda cid, t=tile: self._on_tile_dropped(cid, t))
            tile.customContextMenuRequested.connect(lambda pos, t=tile: self._show_menu(t, pos))
            self._empty[index] = tile
        return tile

    # ------------------------------------------------------------------ persistence
    def _load_slots(self) -> object:
        if self._store is None:
            return None
        try:
            return self._store.get_setting(SETTING_KEY, None)
        except Exception:  # a broken store must not break the grid; the default layout is used
            log.warning("could not read the grid_slots setting", exc_info=True)
            return None

    def _save_slots(self) -> None:
        if self._store is None:
            return
        try:
            self._store.set_setting(SETTING_KEY, self._view.to_setting())
        except OSError as exc:
            log.warning("could not save the grid layout: %s", exc)

    def _commit(self, changed: bool) -> bool:
        """After a slot change: persist, leave fullscreen if its camera left the view, re-layout."""
        if not changed:
            return False
        self._save_slots()
        if self._fullscreen is not None and self._fullscreen not in self._visible_cameras():
            self._fullscreen = None
        self._apply()
        return True

    # ------------------------------------------------------------------ layout + streams
    def _visible_cameras(self) -> list[str]:
        return [cid for cid in self.visible_slots() if cid is not None and cid in self._tiles]

    def _remember_active_slot(self) -> None:
        if self._selected is None:
            return
        index = self._view.index_of(self._selected)
        if index is not None and index < self._n * self._n:
            self._active_slot = index

    def _apply(self) -> None:
        self._remember_active_slot()
        if self._fullscreen is not None:
            self._relayout([(0, self._tiles[self._fullscreen])], 1)
        else:
            widgets: list[tuple[int, QWidget]] = []
            for index, cid in enumerate(self.visible_slots()):
                tile = self._tiles.get(cid) if cid is not None else None
                widgets.append((index, tile if tile is not None else self._empty_tile(index)))
            self._relayout(widgets, self._n)
        self._forget_idle_manual()
        visible = [cid if cid in self._tiles and not self._no_live_view(self._configs[cid])
                   and self._may_stream(cid) else None for cid in self.visible_slots()]
        self._sync_streams(plan_streams(visible, self._configs, self._fullscreen))
        self.view_changed.emit()

    def _may_stream(self, camera_id: str) -> bool:
        """Every camera streams in view except click-to-start ones (iMou), which need a click first."""
        return camera_id in self._manual or not imou_status.click_to_start(self._configs[camera_id])

    def _forget_idle_manual(self) -> None:
        """A click-started camera that left the view starts again only on a new click."""
        for cid in self._manual - set(self._visible_cameras()):
            self._manual.discard(cid)
            tile = self._tiles.get(cid)
            if tile is not None:
                tile.set_stream_state("FAILED", CLICK_TEXT)

    def _relayout(self, widgets: list[tuple[int, QWidget]], cols: int) -> None:
        while self._layout.count():
            self._layout.takeAt(0)
        shown = {id(w) for _, w in widgets}
        for index, widget in widgets:
            self._layout.addWidget(widget, *divmod(index, cols))
            widget.show()
        for widget in [*self._tiles.values(), *self._empty.values()]:
            if id(widget) not in shown:
                widget.hide()
        for i in range(max(GRID_SIZES)):
            self._layout.setRowStretch(i, 1 if i < cols else 0)
            self._layout.setColumnStretch(i, 1 if i < cols else 0)

    def _sync_streams(self, plan: dict[str, StreamRequest]) -> None:
        """Start / switch / stop streams so that exactly the cameras of ``plan`` stream (view-driven
        streaming, :func:`~tapo_desktop.services.stream_policy.plan_streams`)."""
        for cid, tile in self._tiles.items():
            request = plan.get(cid)
            want = request.as_args() if request is not None else None
            if want == self._requested.get(cid):
                continue
            if want is None:
                del self._requested[cid]
                self._ctrl.stop_view(cid)
                if cid in self._manual:  # an iMou stream the view no longer shows: a new click starts it again
                    self._manual.discard(cid)
                    tile.set_stream_state("FAILED", CLICK_TEXT)
            else:
                if cid not in self._requested:  # fresh start (a quality switch keeps the picture)
                    tile.set_stream_state("CONNECTING")
                self._requested[cid] = want
                self._ctrl.start_view(cid, *want)

