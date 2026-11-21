"""Main window (contract: docs/CONTRACT_UI.md).

objectNames: ``cameraList`` (QListWidget, one row per camera in the order chosen with ``sortButton``, see
Phase 7 below; text = camera name, ``Qt.UserRole`` data = camera id), ``addCameraButton``, ``removeCameraButton``,
``layout1x1Button``, ``layout2x2Button``, ``layout3x3Button``, ``cameraGrid``, ``controlsPanel``,
``playbackPanel``.

* Selection: selecting a row in ``cameraList`` selects that camera in the grid and vice versa; the
  selected camera is given to the controls/playback panels (``set_camera``).
* Grid size is read from setting ``"grid_size"`` at start and persisted by ``set_grid_size``
  (``ValueError`` for anything but 1..5). The layout buttons call ``set_grid_size``.
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

Phase 2 (lane P2-app, docs/CONTRACT_PHASE2.md): ``MainWindow(controller, tray_enabled, accounts=None)``.
``accountButton`` (QToolButton in toolbar ``accountToolbar``) shows the signed-in email and opens a
menu with "Sign in...", "Refresh cameras" and "Sign out" (``accounts.refresh_cameras`` /
``accounts.sign_out``; "Sign in..." opens :class:`SignInDialog`). Without ``accounts`` the actions
are disabled. ``cameraList`` rows show the camera name, plus " (Offline)" for cameras the cloud
reports offline or " (Not on this network)" for cameras not found on the LAN; the bare status is
in :data:`STATUS_ROLE`. Snapshot results and account events are shown in the status bar.

Phase 3: cameras that are not on the LAN but online and viewable through the TP-Link cloud relay
(``controller.remote_available(id)``) show " (Remote)" instead of " (Not on this network)".

Phase 5: ``layout4x4Button`` and ``layout5x5Button`` (grid sizes 1..5). The grid shows the cameras of its
slot list (setting ``"grid_slots"``, see :mod:`tapo_desktop.ui.grid`). ``cameraList`` rows can be dragged
onto a tile; their context menu (:meth:`MainWindow.camera_list_menu`) has "Add to view" and "Remove from
view". Rows of cameras that are not in the current view are shown italic and dimmed (text and status
suffix unchanged, tooltip says so); :data:`IN_VIEW_ROLE` holds True/False.

Phase 7: ``sortButton`` (menu ``cameraSortMenu``: ``sortNameAscAction`` "Name A-Z" (default),
``sortNameDescAction`` "Name Z-A", ``sortAccountAction`` "Account order" = ``controller.cameras()`` order)
orders ``cameraList`` with :func:`~tapo_desktop.services.camera_names.sort_cameras` (QCollator: system locale,
natural numbers, case-insensitive) by the camera name, never by the status suffix; persisted in setting
``"camera_sort"`` (:meth:`MainWindow.set_camera_sort`). The list re-sorts on every ``cameras_changed`` (renames,
cloud refreshes, status changes) and keeps the selection; grid slots never move. "Rename..." in the row menu
(``renameAction``), F2 on the selected row and "Rename..." in the tile menu open :class:`RenameDialog`
(:meth:`MainWindow.rename_camera`); ``resetNameAction`` "Reset to camera name" drops a cloud camera's
local name. Failures arrive as ``command_failed(id, "rename_camera", message)`` in the status bar.
"""
from __future__ import annotations

import logging

from PyQt6.QtCore import QEvent, QMimeData, QPoint, QSignalBlocker, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QCloseEvent, QColor, QKeyEvent
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QDockWidget,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from tapo_desktop.services import camera_names
from tapo_desktop.services.camera_service import NOT_ON_NETWORK
from tapo_desktop.services.remote import REMOTE
from tapo_desktop.ui import _theme, camera_dialog, kami_sign_in_dialog, kami_status, rename_dialog, sign_in_dialog
from tapo_desktop.ui.controls_panel import ControlsPanel
from tapo_desktop.ui.grid import GRID_SIZES, CameraGrid, validate_grid_size
from tapo_desktop.ui.playback_panel import PlaybackPanel
from tapo_desktop.ui.ptz_pad import PTZ_UNIT
from tapo_desktop.ui.video_tile import camera_mime_data

log = logging.getLogger(__name__)

#: Keyboard PTZ: key -> (dx, dy) with dx > 0 right, dy > 0 up.
PTZ_KEYS: dict[Qt.Key, tuple[int, int]] = {
    Qt.Key.Key_W: (0, PTZ_UNIT), Qt.Key.Key_Up: (0, PTZ_UNIT),
    Qt.Key.Key_S: (0, -PTZ_UNIT), Qt.Key.Key_Down: (0, -PTZ_UNIT),
    Qt.Key.Key_A: (-PTZ_UNIT, 0), Qt.Key.Key_Left: (-PTZ_UNIT, 0),
    Qt.Key.Key_D: (PTZ_UNIT, 0), Qt.Key.Key_Right: (PTZ_UNIT, 0),
}
_STATUS_MS = 6000
#: ``cameraList`` item role holding the row status (``""``, ``"Offline"``, ``"Remote"`` or ``NOT_ON_NETWORK``).
STATUS_ROLE = Qt.ItemDataRole.UserRole + 1
#: ``cameraList`` item role: True if the camera is in a visible grid slot (Phase 5).
IN_VIEW_ROLE = Qt.ItemDataRole.UserRole + 2
_BASE_TIP_ROLE = Qt.ItemDataRole.UserRole + 3
_MUTED = QColor(140, 145, 152)
_HIDDEN = QColor(110, 114, 120)
_HIDDEN_TIP = "Not in the current view (right-click or drag it onto a tile to show it)"


def camera_row_status(cfg, remote: bool = False) -> str:
    """Status suffix of a camera row: "Offline" (cloud says offline), "Remote" (not on this LAN but
    viewable through the cloud relay: ``remote``), NOT_ON_NETWORK, or ""."""
    if cfg.source == kami_status.KAMI_SOURCE:  # a Kami camera has no host and is never "on this network"
        return "" if cfg.online else "Offline"
    if not cfg.online:
        return "Offline"
    if not cfg.lan_found or not cfg.host:
        return REMOTE if remote else NOT_ON_NETWORK
    return ""


class CameraListWidget(QListWidget):
    """``cameraList``: rows drag as :data:`~tapo_desktop.ui.video_tile.CAMERA_MIME` payloads; F2 asks to
    rename the current row (``rename_requested(camera_id)``)."""

    rename_requested = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setDragDropMode(QListWidget.DragDropMode.DragOnly)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 (Qt override)
        if event.key() == Qt.Key.Key_F2 and not event.isAutoRepeat():
            item = self.currentItem()
            camera_id = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
            if isinstance(camera_id, str) and camera_id:
                event.accept()
                self.rename_requested.emit(camera_id)
                return
        super().keyPressEvent(event)

    def mimeData(self, items) -> QMimeData:  # noqa: N802 (Qt override)
        for item in items:
            camera_id = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(camera_id, str) and camera_id:
                return camera_mime_data(camera_id)
        return QMimeData()


class MainWindow(QMainWindow):
    def __init__(self, controller, tray_enabled: bool = True, accounts=None, kami_accounts=None):
        super().__init__()
        self._ctrl = controller
        self._accounts = accounts
        self._kami = kami_accounts
        self._kami_dialog = None
        self._store = controller.config_store
        self._selected: str | None = None
        self._held_key: int | None = None
        self._quitting = False
        self._sign_in_dialog = None
        self._sort = camera_names.sort_mode(self._store.get_setting(camera_names.SETTING_CAMERA_SORT, None))
        self.setWindowTitle("Tapo Desktop")
        self.setWindowIcon(_theme.app_icon())
        self.resize(1280, 800)

        self._grid = CameraGrid(controller, self)
        self.setCentralWidget(self._grid)
        self._build_toolbar()
        self._build_account_toolbar()
        self._build_camera_dock()
        self._build_side_dock()
        self.statusBar().showMessage("Ready")

        self._tray = None
        if tray_enabled:
            from tapo_desktop.ui.tray import TrayController

            self._tray = TrayController(self, controller)

        self._grid.camera_selected.connect(lambda cid: self._select(cid or None))
        self._grid.view_changed.connect(self._sync_view_marks)
        self._grid.rename_requested.connect(self.rename_camera)
        self._list.currentRowChanged.connect(self._on_row_changed)
        self._list.rename_requested.connect(self.rename_camera)
        controller.cameras_changed.connect(self._refresh_list)
        controller.command_failed.connect(self._on_command_failed)
        controller.stream_state.connect(self._on_stream_state)
        self._controls.snapshot_taken.connect(self._on_snapshot)
        if accounts is not None:
            accounts.signed_in.connect(lambda _account: self._sync_account())
            accounts.signed_out.connect(self._on_signed_out)
            accounts.sign_in_failed.connect(self._on_sign_in_failed)
            accounts.cameras_refreshed.connect(self._on_cameras_refreshed)
            accounts.mfa_required.connect(self._on_mfa_required)
        self._sync_account()
        if kami_accounts is not None:
            kami_accounts.signed_in.connect(lambda _label: self._sync_kami())
            kami_accounts.signed_out.connect(self._on_kami_signed_out)
            kami_accounts.sign_in_failed.connect(self._on_kami_sign_in_failed)
        self._sync_kami()

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

    # ---------------------------------------------------------- Phase 7: sorting and renaming
    def camera_sort(self) -> str:
        """Order of ``cameraList``: ``"name_asc"`` (default), ``"name_desc"`` or ``"account"``."""
        return self._sort

    def set_camera_sort(self, mode: str) -> None:
        """Re-sort ``cameraList`` (selection kept, grid slots unchanged) and persist setting ``camera_sort``;
        ``ValueError`` for an unknown mode."""
        if mode not in camera_names.SORT_MODES:
            raise ValueError(f"unknown camera sort {mode!r}")
        self._sort = mode
        self._store.set_setting(camera_names.SETTING_CAMERA_SORT, mode)
        self._sync_sort_actions()
        self._refresh_list()

    def rename_camera(self, camera_id: str | None = None) -> None:
        """Ask for a new name of ``camera_id`` (default: the selected camera) in a :class:`RenameDialog` and
        rename it through the controller (owned cloud cameras on the camera, others in this app only)."""
        camera_id = camera_id or self._selected
        cfg = self._config(camera_id) if camera_id else None
        if cfg is None:
            return
        dialog = rename_dialog.RenameDialog(cfg.name, on_camera=camera_names.renames_on_camera(cfg), parent=self)
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            new = dialog.value()
        except camera_names.InvalidCameraName as exc:
            self.statusBar().showMessage(str(exc), _STATUS_MS)
            return
        finally:
            dialog.deleteLater()
        if new == cfg.name and not camera_names.renames_on_camera(cfg):
            return
        try:
            on_camera = self._ctrl.rename_camera(cfg.id, new)
        except (KeyError, ValueError, OSError) as exc:
            self.statusBar().showMessage(f"Could not rename the camera: {exc}", _STATUS_MS)
            return
        self.statusBar().showMessage(f"Renaming \u201c{cfg.name}\u201d to \u201c{new}\u201d\u2026" if on_camera
                                     else f"Renamed to \u201c{new}\u201d (in this app)", _STATUS_MS)

    def reset_camera_name(self, camera_id: str) -> None:
        """Drop this app's own name of a cloud camera and show the camera's name again."""
        try:
            self._ctrl.reset_camera_name(camera_id)
        except (KeyError, OSError) as exc:
            self.statusBar().showMessage(f"Could not reset the name: {exc}", _STATUS_MS)

    @property
    def accounts(self):
        """The AccountManager given to ``__init__`` (None = manual cameras only)."""
        return self._accounts

    def show_sign_in(self, mfa_types: list[int] | None = None) -> bool:
        """Run the SignInDialog modally; True when the user signed in. With ``mfa_types`` the
        dialog opens on its two-step verification section (a silent sign-in at startup that the
        cloud answered with an MFA challenge)."""
        if self._accounts is None or self._sign_in_dialog is not None:
            return False
        dialog = sign_in_dialog.SignInDialog(self._accounts, self)
        self._sign_in_dialog = dialog
        try:
            if mfa_types:
                dialog.show_mfa(list(mfa_types))
            return dialog.exec() == QDialog.DialogCode.Accepted
        finally:
            self._sign_in_dialog = None
            dialog.deleteLater()

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
        for n in GRID_SIZES:
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

    def _build_account_toolbar(self) -> None:
        bar = QToolBar("Account", self)
        bar.setObjectName("accountToolbar")
        bar.setMovable(False)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, bar)
        menu = QMenu(self)
        menu.setObjectName("accountMenu")
        self._sign_in_action = menu.addAction("Sign in\u2026")
        self._sign_in_action.triggered.connect(lambda _checked=False: self.show_sign_in())
        menu.addSeparator()
        self._refresh_action = menu.addAction("Refresh cameras")
        self._refresh_action.setToolTip("Reload the camera list and find the cameras on this network")
        self._refresh_action.triggered.connect(lambda _checked=False: self._refresh_cameras())
        self._sign_out_action = menu.addAction("Sign out")
        self._sign_out_action.triggered.connect(lambda _checked=False: self._sign_out())
        menu.addSeparator()
        self._kami_sign_in_action = menu.addAction("Kami account\u2026")
        self._kami_sign_in_action.setObjectName("kamiSignInAction")
        self._kami_sign_in_action.triggered.connect(lambda _checked=False: self.show_kami_sign_in())
        self._kami_sign_out_action = menu.addAction("Sign out of Kami")
        self._kami_sign_out_action.setObjectName("kamiSignOutAction")
        self._kami_sign_out_action.triggered.connect(lambda _checked=False: self._kami_sign_out())
        self._account_menu = menu
        button = QToolButton(bar)
        button.setObjectName("accountButton")
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        button.setMenu(menu)
        bar.addWidget(button)
        self._account_button = button

    def _build_camera_dock(self) -> None:
        panel = QWidget(self)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addLayout(self._build_sort_row(panel))
        self._list = CameraListWidget(panel)
        self._list.setObjectName("cameraList")
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._show_list_menu)
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

    _SORT_ACTIONS = ((camera_names.SORT_NAME_ASC, "sortNameAscAction", "Name A\u2013Z", "A\u2013Z"),
                     (camera_names.SORT_NAME_DESC, "sortNameDescAction", "Name Z\u2013A", "Z\u2013A"),
                     (camera_names.SORT_ACCOUNT, "sortAccountAction", "Account order", "Account"))

    def _build_sort_row(self, parent: QWidget) -> QHBoxLayout:
        """``sortButton``: a small menu choosing the order of ``cameraList`` (grid slots never move)."""
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addStretch(1)
        menu = QMenu(parent)
        menu.setObjectName("cameraSortMenu")
        group = QActionGroup(menu)
        group.setExclusive(True)
        self._sort_actions: dict[str, QAction] = {}
        for mode, name, text, _short in self._SORT_ACTIONS:
            action = menu.addAction(text)
            action.setObjectName(name)
            action.setCheckable(True)
            action.triggered.connect(lambda _checked=False, m=mode: self.set_camera_sort(m))
            group.addAction(action)
            self._sort_actions[mode] = action
        button = QToolButton(parent)
        button.setObjectName("sortButton")
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        button.setAutoRaise(True)
        button.setMenu(menu)
        row.addWidget(button)
        self._sort_button = button
        self._sync_sort_actions()
        return row

    def _sync_sort_actions(self) -> None:
        action = self._sort_actions.get(self._sort)
        if action is not None:
            action.setChecked(True)
        short = next(s for m, _n, _t, s in self._SORT_ACTIONS if m == self._sort)
        self._sort_button.setText(f"Sort: {short}")
        self._sort_button.setToolTip("Order of the camera list (the grid keeps its own layout)")

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
        cams = camera_names.sort_cameras(self._ctrl.cameras(), self._sort)  # by display name, never the suffix
        remote_available = getattr(self._ctrl, "remote_available", None)
        with QSignalBlocker(self._list):
            self._list.clear()
            for cfg in cams:
                remote = bool(callable(remote_available) and remote_available(cfg.id))
                status = camera_row_status(cfg, remote=remote)
                item = QListWidgetItem(f"{cfg.name} ({status})" if status else cfg.name)
                item.setData(Qt.ItemDataRole.UserRole, cfg.id)
                item.setData(STATUS_ROLE, status)
                tip = cfg.host or ("Not on this network: live view through the TP-Link cloud" if remote
                                   else "No address on this network")
                if kami_status.is_kami(cfg):  # no host: say what it is and whether the Kami cloud sees it
                    tip = f"{kami_status.list_tooltip(cfg)}, {kami_status.online_label(cfg.online)}"
                if cfg.model and not kami_status.is_kami(cfg):  # a Kami model is a server code, not a name
                    tip = f"{cfg.model} - {tip}"
                if cfg.source == "cloud":
                    uses = getattr(self._ctrl, "uses_local_token", None)
                    if callable(uses):  # yes/no only: the token itself never reaches the UI
                        tip += f"\nusing local access token: {'yes' if uses(cfg.id) else 'no'}"
                item.setToolTip(tip)
                item.setData(_BASE_TIP_ROLE, tip)
                self._list.addItem(item)
        self._sync_view_marks()
        ids = [c.id for c in cams]
        keep = self._selected if self._selected in ids else (ids[0] if ids else None)
        self._select(keep, force=True)  # re-sync list row, grid and panels after the rebuild

    def _sync_view_marks(self) -> None:
        """Mark rows of cameras that are not in a visible grid slot (italic, dimmed, tooltip)."""
        for row in range(self._list.count()):
            item = self._list.item(row)
            in_view = self._grid.in_view(item.data(Qt.ItemDataRole.UserRole))
            status = item.data(STATUS_ROLE)
            if item.data(IN_VIEW_ROLE) is in_view:
                continue
            item.setData(IN_VIEW_ROLE, in_view)
            font = item.font()
            font.setItalic(not in_view)
            item.setFont(font)
            if not in_view:
                item.setForeground(_HIDDEN)
            elif status and status != REMOTE:
                item.setForeground(_MUTED)  # muted: not streamable right now
            else:
                item.setData(Qt.ItemDataRole.ForegroundRole, None)
            tip = item.data(_BASE_TIP_ROLE) or ""
            item.setToolTip(tip if in_view else f"{tip}\n{_HIDDEN_TIP}" if tip else _HIDDEN_TIP)

    # ================================================================== view (Phase 5)
    def camera_list_menu(self, camera_id: str) -> QMenu:
        """Context menu of a ``cameraList`` row (the caller shows it; it is deleted on close)."""
        menu = QMenu(self)
        menu.setObjectName("cameraListMenu")
        in_view = self._grid.in_view(camera_id)
        add = menu.addAction("Add to view")
        add.setObjectName("addToViewAction")
        add.setEnabled(not in_view)
        add.triggered.connect(lambda _checked=False: self._grid.add_to_view(camera_id))
        remove = menu.addAction("Remove from view")
        remove.setObjectName("removeFromViewAction")
        remove.setEnabled(in_view)
        remove.triggered.connect(lambda _checked=False: self._grid.remove_from_view(camera_id))
        menu.addSeparator()
        cfg = self._config(camera_id)
        rename = menu.addAction("Rename\u2026")
        rename.setObjectName("renameAction")
        rename.setShortcut(Qt.Key.Key_F2)
        rename.setEnabled(cfg is not None)
        rename.triggered.connect(lambda _checked=False: self.rename_camera(camera_id))
        reset = menu.addAction("Reset to camera name")
        reset.setObjectName("resetNameAction")
        reset.setEnabled(cfg is not None and cfg.source == "cloud" and bool(cfg.display_name))
        if cfg is not None and cfg.cloud_alias:
            reset.setToolTip(f"Show \u201c{cfg.cloud_alias}\u201d again")
        reset.triggered.connect(lambda _checked=False: self.reset_camera_name(camera_id))
        return menu

    def _show_list_menu(self, pos: QPoint) -> None:
        item = self._list.itemAt(pos)
        camera_id = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if not camera_id:
            return
        menu = self.camera_list_menu(camera_id)
        try:
            menu.exec(self._list.viewport().mapToGlobal(pos))
        finally:
            menu.deleteLater()

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
    def _config(self, camera_id: str | None):
        return next((c for c in self._ctrl.cameras() if c.id == camera_id), None)

    def _camera_name(self, camera_id: str) -> str:
        return next((c.name for c in self._ctrl.cameras() if c.id == camera_id), camera_id)

    def _on_command_failed(self, camera_id: str, action: str, message: str) -> None:
        what = action.replace("_", " ")
        self.statusBar().showMessage(f"{self._camera_name(camera_id)}: {what} failed: {message}", _STATUS_MS)

    def _on_stream_state(self, camera_id: str, state: str, reason: str) -> None:
        if state == "FAILED" and reason:
            self.statusBar().showMessage(f"{self._camera_name(camera_id)}: {reason}", _STATUS_MS)

    def _on_snapshot(self, path: str, error: str) -> None:
        if path:
            self.statusBar().showMessage(f"Snapshot saved: {path}", _STATUS_MS)
        else:
            self.statusBar().showMessage(error or "Snapshot failed", _STATUS_MS)

    # ================================================================== account (Phase 2)
    def _sync_account(self) -> None:
        account = self._accounts.account() if self._accounts is not None else None
        if account is not None:
            label = account.nickname or account.email
            self._account_button.setText(label)
            self._account_button.setToolTip(f"Signed in as {account.email}")
        else:
            self._account_button.setText("Sign in")
            self._account_button.setToolTip("Sign in with your TP-Link ID to show your cameras"
                                            if self._accounts is not None else "Account features unavailable")
        self._sign_in_action.setEnabled(self._accounts is not None and account is None)
        self._refresh_action.setEnabled(account is not None)
        self._sign_out_action.setEnabled(account is not None)

    def _refresh_cameras(self) -> None:
        if self._accounts is not None and self._accounts.account() is not None:
            self.statusBar().showMessage("Looking for cameras\u2026", _STATUS_MS)
            self._accounts.refresh_cameras()

    def _sign_out(self) -> None:
        if self._accounts is not None and self._accounts.account() is not None:
            self._accounts.sign_out()

    def _on_signed_out(self) -> None:
        self._sync_account()
        self.statusBar().showMessage("Signed out", _STATUS_MS)

    def _on_sign_in_failed(self, message: str) -> None:
        self._sync_account()
        if self._sign_in_dialog is None:  # the dialog shows its own errors inline
            self.statusBar().showMessage(f"{message} Use the account button to sign in again.", _STATUS_MS * 2)

    def _on_cameras_refreshed(self, total: int, found: int) -> None:
        self.statusBar().showMessage(
            f"{total} camera{'s' if total != 1 else ''} in your account, {found} found on this network",
            _STATUS_MS)

    def _on_mfa_required(self, types) -> None:
        if self._sign_in_dialog is None:  # silent sign-in hit an MFA challenge: ask for the code
            mfa_types = [int(t) for t in (types or [])]
            QTimer.singleShot(0, lambda: self.show_sign_in(mfa_types or [3]))

    # ================================================================== Kami Home (unofficial)
    @property
    def kami_accounts(self):
        """The KamiAccountManager given to ``__init__`` (None = no Kami entries enabled)."""
        return self._kami

    def _ptz_supported(self, camera_id: str) -> bool:
        return kami_status.control_enabled(kami_status.capability_service(self._config(camera_id)), "ptz")

    def show_kami_sign_in(self) -> bool:
        """Run the KamiSignInDialog modally; True when the user signed in. The chosen country is remembered."""
        if self._kami is None or self._kami_dialog is not None:
            return False
        from tapo_desktop.core.kami import regions

        dialog = kami_sign_in_dialog.KamiSignInDialog(
            self._kami, regions.countries(), self,
            last_country=self._store.get_setting(kami_status.SETTING_KAMI_LAST_COUNTRY, None))
        dialog.country_chosen.connect(
            lambda code: self._store.set_setting(kami_status.SETTING_KAMI_LAST_COUNTRY, code))
        self._kami_dialog = dialog
        try:
            return dialog.exec() == QDialog.DialogCode.Accepted
        finally:
            self._kami_dialog = None
            dialog.deleteLater()

    def _sync_kami(self) -> None:
        signed = self._kami is not None and self._kami.is_signed_in()
        self._kami_sign_in_action.setEnabled(self._kami is not None and not signed)
        self._kami_sign_out_action.setEnabled(signed)
        self._kami_sign_in_action.setToolTip(f"Signed in to Kami as {self._kami.account_label()}" if signed
                                             else "Sign in to Kami Home (unofficial)")

    def _kami_sign_out(self) -> None:
        if self._kami is not None and self._kami.is_signed_in():
            self._kami.sign_out()

    def _on_kami_signed_out(self) -> None:
        self._sync_kami()
        self.statusBar().showMessage("Signed out of Kami", _STATUS_MS)

    def _on_kami_sign_in_failed(self, message: str) -> None:
        self._sync_kami()
        if self._kami_dialog is None:  # the dialog shows its own errors inline; this is e.g. a dead session
            self.statusBar().showMessage(f"{message} Use the account button to sign in to Kami again.",
                                         _STATUS_MS * 2)

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
        if event.key() == Qt.Key.Key_F2 and not event.isAutoRepeat() and self._selected is not None:
            event.accept()
            self.rename_camera(self._selected)
            return
        delta = self._ptz_delta(event)
        if delta is None:
            super().keyPressEvent(event)
            return
        event.accept()
        if event.isAutoRepeat() or self._held_key is not None or self._selected is None:
            return
        if not self._ptz_supported(self._selected):
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
