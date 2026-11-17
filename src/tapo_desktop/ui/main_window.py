"""Main window (contract: docs/CONTRACT_UI.md).

objectNames: ``cameraList`` (QListWidget, one row per camera in ``controller.cameras()`` order,
text = camera name, ``Qt.UserRole`` data = camera id), ``addCameraButton``, ``removeCameraButton``,
``layout1x1Button``, ``layout2x2Button``, ``layout3x3Button``, ``cameraGrid``, ``controlsPanel``,
``playbackPanel``.

* Selection: selecting a row in ``cameraList`` selects that camera in the grid and vice versa; the
  selected camera is given to the controls/playback panels (``set_camera``).
* Grid size is read from setting ``"grid_size"`` at start and persisted by ``set_grid_size``
  (``ValueError`` for anything but 1, 2, 3). The layout buttons call ``set_grid_size``.
* ``addCameraButton`` runs ``CameraDialog(self).exec()``; on Accepted it calls
  ``controller.add_camera(*dialog.result_values())``. ``removeCameraButton`` asks
  ``QMessageBox.question(...)`` and on Yes calls ``controller.remove_camera(selected_id)``.
  ``cameras_changed`` refreshes ``cameraList`` and the grid.
* Keyboard (handled in the window's ``keyPressEvent``/``keyReleaseEvent``, auto-repeat ignored):
  W/Up -> ``ptz_move(id, 0, +d)``, S/Down -> ``(0, -d)``, A/Left -> ``(-d, 0)``, D/Right -> ``(+d, 0)``
  for the selected camera (d > 0); releasing the key -> ``ptz_stop(id)``. No selection -> no call.
* ``closeEvent``: if setting ``"minimize_to_tray"`` is True the event is ignored and the window is
  hidden (streams keep running); otherwise ``quit()``. ``quit()`` calls ``controller.stop_all()``,
  hides the tray icon and calls ``QApplication.quit()``.

Lane additions: ``editCameraButton`` (and double-click on a list row) opens the dialog in edit mode
and calls ``controller.update_camera``.
"""
from __future__ import annotations

import logging

from PyQt6.QtCore import QEvent, QSignalBlocker, Qt
from PyQt6.QtGui import QAction, QActionGroup, QCloseEvent, QKeyEvent
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QDockWidget,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from tapo_desktop.ui import _theme, camera_dialog
from tapo_desktop.ui.controls_panel import ControlsPanel
from tapo_desktop.ui.grid import CameraGrid, validate_grid_size
from tapo_desktop.ui.playback_panel import PlaybackPanel
from tapo_desktop.ui.ptz_pad import PTZ_UNIT

log = logging.getLogger(__name__)

#: Keyboard PTZ: key -> (dx, dy) with dx > 0 right, dy > 0 up.
PTZ_KEYS: dict[Qt.Key, tuple[int, int]] = {
    Qt.Key.Key_W: (0, PTZ_UNIT), Qt.Key.Key_Up: (0, PTZ_UNIT),
    Qt.Key.Key_S: (0, -PTZ_UNIT), Qt.Key.Key_Down: (0, -PTZ_UNIT),
    Qt.Key.Key_A: (-PTZ_UNIT, 0), Qt.Key.Key_Left: (-PTZ_UNIT, 0),
    Qt.Key.Key_D: (PTZ_UNIT, 0), Qt.Key.Key_Right: (PTZ_UNIT, 0),
}
_STATUS_MS = 6000


class MainWindow(QMainWindow):
    def __init__(self, controller, tray_enabled: bool = True, accounts=None):
        super().__init__()
        self._ctrl = controller
        self._store = controller.config_store
        self._selected: str | None = None
        self._held_key: int | None = None
        self._quitting = False
        self.setWindowTitle("Tapo Desktop")
        self.setWindowIcon(_theme.app_icon())
        self.resize(1280, 800)

        self._grid = CameraGrid(controller, self)
        self.setCentralWidget(self._grid)
        self._build_toolbar()
        self._build_camera_dock()
        self._build_side_dock()
        self.statusBar().showMessage("Ready")

        self._tray = None
        if tray_enabled:
            from tapo_desktop.ui.tray import TrayController

            self._tray = TrayController(self, controller)

        self._grid.camera_selected.connect(lambda cid: self._select(cid or None))
        self._list.currentRowChanged.connect(self._on_row_changed)
        controller.cameras_changed.connect(self._refresh_list)
        controller.command_failed.connect(self._on_command_failed)
        controller.stream_state.connect(self._on_stream_state)

        self._grid.set_grid_size(self._initial_grid_size())
        self._sync_layout_buttons()
        self._refresh_list()

    # ================================================================== public API
    @property
    def tray(self):
        """The TrayController, or None when ``tray_enabled`` is False."""
        return self._tray

    def grid_size(self) -> int:
        return self._grid.grid_size()

    def set_grid_size(self, n: int) -> None:
        n = validate_grid_size(n)
        self._grid.set_grid_size(n)
        self._store.set_setting("grid_size", n)
        self._sync_layout_buttons()

    def selected_camera_id(self) -> str | None:
        return self._selected

    def quit(self) -> None:
        """Stop every stream/event listener, remove the tray icon and quit the application."""
        if self._quitting:
            return
        self._quitting = True
        try:
            self._ctrl.stop_all()
        finally:
            if self._tray is not None:
                self._tray.hide()
            QApplication.quit()

    # ================================================================== construction
    def _build_toolbar(self) -> None:
        bar = QToolBar("Layout", self)
        bar.setObjectName("layoutToolbar")
        bar.setMovable(False)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, bar)
        group = QActionGroup(self)
        group.setExclusive(True)
        self._layout_actions: dict[int, QAction] = {}
        for n in (1, 2, 3):
            action = QAction(f"{n}\u00d7{n}", self)
            action.setCheckable(True)
            action.setToolTip(f"Show {n * n} camera{'s' if n > 1 else ''}")
            action.triggered.connect(lambda _checked, n=n: self.set_grid_size(n))
            group.addAction(action)
            bar.addAction(action)
            button = bar.widgetForAction(action)
            if isinstance(button, QToolButton):
                button.setObjectName(f"layout{n}x{n}Button")
            self._layout_actions[n] = action

    def _build_camera_dock(self) -> None:
        panel = QWidget(self)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(6, 6, 6, 6)
        self._list = QListWidget(panel)
        self._list.setObjectName("cameraList")
        self._list.itemDoubleClicked.connect(lambda _item: self._edit_camera())
        layout.addWidget(self._list, 1)
        buttons = QHBoxLayout()
        self._add_btn = self._button(panel, "addCameraButton", "Add", "Add a camera", self._add_camera)
        self._edit_btn = self._button(panel, "editCameraButton", "Edit", "Edit the selected camera",
                                      self._edit_camera)
        self._remove_btn = self._button(panel, "removeCameraButton", "Remove", "Remove the selected camera",
                                        self._remove_camera)
        for b in (self._add_btn, self._edit_btn, self._remove_btn):
            buttons.addWidget(b)
        layout.addLayout(buttons)
        dock = QDockWidget("Cameras", self)
        dock.setObjectName("cameraDock")
        dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable)
        dock.setWidget(panel)
        dock.setMinimumWidth(200)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)

    def _build_side_dock(self) -> None:
        self._controls = ControlsPanel(self._ctrl, self)
        self._playback = PlaybackPanel(self._ctrl, self)
        tabs = QTabWidget(self)
        tabs.setObjectName("sideTabs")
        tabs.addTab(self._controls, "Controls")
        tabs.addTab(self._playback, "Playback")
        dock = QDockWidget("Camera", self)
        dock.setObjectName("sideDock")
        dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable)
        dock.setWidget(tabs)
        dock.setMinimumWidth(300)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)

    @staticmethod
    def _button(parent: QWidget, name: str, text: str, tip: str, slot) -> QPushButton:
        btn = QPushButton(text, parent)
        btn.setObjectName(name)
        btn.setToolTip(tip)
        btn.clicked.connect(lambda _checked=False: slot())
        return btn

    def _initial_grid_size(self) -> int:
        try:
            return validate_grid_size(self._store.get_setting("grid_size"))
        except ValueError:
            log.warning("ignoring invalid grid_size setting")
            return 2

    def _sync_layout_buttons(self) -> None:
        action = self._layout_actions.get(self._grid.grid_size())
        if action is not None:
            action.setChecked(True)

    # ================================================================== selection
    def _select(self, camera_id: str | None, *, force: bool = False) -> None:
        if camera_id == self._selected and not force:
            return
        self._selected = camera_id
        self._grid.select_camera(camera_id)
        with QSignalBlocker(self._list):
            self._list.setCurrentRow(self._row_of(camera_id))
        self._controls.set_camera(camera_id)
        self._playback.set_camera(camera_id)
        self._edit_btn.setEnabled(camera_id is not None)
        self._remove_btn.setEnabled(camera_id is not None)

    def _on_row_changed(self, row: int) -> None:
        item = self._list.item(row) if row >= 0 else None
        self._select(item.data(Qt.ItemDataRole.UserRole) if item is not None else None)

    def _row_of(self, camera_id: str | None) -> int:
        for row in range(self._list.count()):
            if self._list.item(row).data(Qt.ItemDataRole.UserRole) == camera_id:
                return row
        return -1

    def _refresh_list(self) -> None:
        cams = self._ctrl.cameras()
        with QSignalBlocker(self._list):
            self._list.clear()
            for cfg in cams:
                item = QListWidgetItem(cfg.name)
                item.setData(Qt.ItemDataRole.UserRole, cfg.id)
                item.setToolTip(cfg.host)
                self._list.addItem(item)
        ids = [c.id for c in cams]
        keep = self._selected if self._selected in ids else (ids[0] if ids else None)
        self._select(keep, force=True)  # re-sync list row, grid and panels after the rebuild

    # ================================================================== camera management
    def _add_camera(self) -> None:
        dialog = camera_dialog.CameraDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            config, cloud, camera = dialog.result_values()
            try:
                self._ctrl.add_camera(config, cloud, camera)
            except (ValueError, OSError) as exc:
                QMessageBox.warning(self, "Add camera", f"Could not add the camera: {exc}")
                return
            self._select(config.id)

    def _edit_camera(self) -> None:
        cfg = next((c for c in self._ctrl.cameras() if c.id == self._selected), None)
        if cfg is None:
            return
        dialog = camera_dialog.CameraDialog(self, cfg)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            config, cloud, camera = dialog.result_values()
            try:
                self._ctrl.update_camera(config, cloud, camera)
            except (KeyError, OSError) as exc:
                QMessageBox.warning(self, "Edit camera", f"Could not save the camera: {exc}")

    def _remove_camera(self) -> None:
        cfg = next((c for c in self._ctrl.cameras() if c.id == self._selected), None)
        if cfg is None:
            return
        answer = QMessageBox.question(self, "Remove camera",
                                      f"Remove \u201c{cfg.name}\u201d and forget its passwords?")
        if answer == QMessageBox.StandardButton.Yes:
            self._ctrl.remove_camera(cfg.id)

    # ================================================================== feedback
    def _camera_name(self, camera_id: str) -> str:
        return next((c.name for c in self._ctrl.cameras() if c.id == camera_id), camera_id)

    def _on_command_failed(self, camera_id: str, action: str, message: str) -> None:
        what = action.replace("_", " ")
        self.statusBar().showMessage(f"{self._camera_name(camera_id)}: {what} failed: {message}", _STATUS_MS)

    def _on_stream_state(self, camera_id: str, state: str, reason: str) -> None:
        if state == "FAILED" and reason:
            self.statusBar().showMessage(f"{self._camera_name(camera_id)}: {reason}", _STATUS_MS)

    # ================================================================== keyboard PTZ
    @staticmethod
    def _ptz_delta(event: QKeyEvent) -> tuple[int, int] | None:
        """(dx, dy) for an unmodified W/A/S/D/arrow key, else None."""
        mods = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        if mods != Qt.KeyboardModifier.NoModifier:
            return None
        try:
            return PTZ_KEYS.get(Qt.Key(event.key()))
        except ValueError:  # key code without a Qt.Key member
            return None

    def keyPressEvent(self, event: QKeyEvent) -> None:
        delta = self._ptz_delta(event)
        if delta is None:
            super().keyPressEvent(event)
            return
        event.accept()
        if event.isAutoRepeat() or self._held_key is not None or self._selected is None:
            return
        self._held_key = event.key()
        self._ctrl.ptz_move(self._selected, *delta)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        if self._ptz_delta(event) is None and event.key() != self._held_key:
            super().keyReleaseEvent(event)
            return
        event.accept()
        if not event.isAutoRepeat() and event.key() == self._held_key:
            self._release_ptz_key()

    def _release_ptz_key(self) -> None:
        self._held_key = None
        if self._selected is not None:
            self._ctrl.ptz_stop(self._selected)

    def changeEvent(self, event: QEvent) -> None:
        # A key released while another window has focus never reaches us: stop moving on deactivate.
        if (event.type() == QEvent.Type.ActivationChange and not self.isActiveWindow()
                and self._held_key is not None):
            self._release_ptz_key()
        super().changeEvent(event)

    # ================================================================== window lifecycle
    def closeEvent(self, event: QCloseEvent) -> None:
        if self._quitting:
            event.accept()
            return
        tray_usable = self._tray is None or self._tray.is_available()
        if self._store.get_setting("minimize_to_tray") and tray_usable:
            event.ignore()
            self.hide()
            return
        event.accept()
        self.quit()
