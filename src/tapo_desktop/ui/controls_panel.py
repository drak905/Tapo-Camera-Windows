"""Camera controls (contract: docs/CONTRACT_UI.md). objectName ``controlsPanel``.

Children: ``privacyToggle``, ``ledToggle``, ``alarmToggle``, ``motionToggle`` (checkable buttons /
check boxes; user toggles call ``controller.set_privacy/set_led/set_alarm/set_motion_detection``),
``dayNightCombo`` (3 items whose ``itemData`` are ``DayNightMode.AUTO, ON, OFF`` in that order; a
user change calls ``controller.set_day_night``), ``ptzPad`` (wired to ``controller.ptz_move`` /
``ptz_stop``), ``presetCombo`` (item text = preset name, ``itemData`` = preset id) and
``gotoPresetButton`` (calls ``controller.goto_preset`` with the current preset id).

Lane P2-app: ``snapshotButton`` saves the selected camera's newest frame (``controller.latest_image``)
as PNG via :func:`tapo_desktop.ui.snapshot.save_snapshot` and emits ``snapshot_taken(path, error)``
(exactly one of the two is non-empty).

``set_camera(id)`` calls ``controller.refresh_status(id)`` and ``controller.load_presets(id)``.
``status_changed`` for the current camera updates the widgets WITHOUT calling the controller back;
``command_failed`` leaves the widgets showing the rolled-back status that follows it.

User actions are detected with ``clicked`` / ``activated`` (emitted only for user interaction), so
programmatic updates from ``status_changed`` never echo back to the camera.
"""
from __future__ import annotations

import logging

from PyQt6.QtCore import QSignalBlocker, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from tapo_desktop.models import DayNightMode
from tapo_desktop.ui import snapshot
from tapo_desktop.ui.ptz_pad import PtzPad

log = logging.getLogger(__name__)

#: (objectName, label, status key, controller method, tooltip)
_TOGGLES: tuple[tuple[str, str, str, str, str], ...] = (
    ("privacyToggle", "Privacy mode", "privacy", "set_privacy", "Lens mask: stops video and recording"),
    ("ledToggle", "Status LED", "led", "set_led", "Camera status light"),
    ("alarmToggle", "Siren", "alarm", "set_alarm", "Sound the camera's alarm"),
    ("motionToggle", "Motion detection", "motion_detection", "set_motion_detection",
     "Detect motion (events and recordings)"),
)
_DAY_NIGHT: tuple[tuple[str, DayNightMode], ...] = (
    ("Auto", DayNightMode.AUTO), ("Night vision (IR on)", DayNightMode.ON), ("Day mode (IR off)", DayNightMode.OFF),
)


class ControlsPanel(QWidget):
    snapshot_taken = pyqtSignal(str, str)  # path, error (lane P2-app)

    def __init__(self, controller, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("controlsPanel")
        self._ctrl = controller
        self._camera_id: str | None = None
        self._toggles: dict[str, QPushButton] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(8)
        self._title = QLabel("No camera selected", self)
        self._title.setObjectName("controlsTitle")
        root.addWidget(self._title)
        root.addWidget(self._build_ptz())
        root.addWidget(self._build_settings())
        root.addStretch(1)

        controller.status_changed.connect(self._on_status)
        controller.presets_ready.connect(self._on_presets)
        controller.cameras_changed.connect(self._on_cameras_changed)
        self._set_enabled(False)

    # ------------------------------------------------------------------ public API
    def camera_id(self) -> str | None:
        return self._camera_id

    def set_camera(self, camera_id: str | None) -> None:
        """Show controls for ``camera_id`` (None = nothing selected) and fetch its state."""
        self._camera_id = camera_id or None
        self._presets.clear()
        self._goto.setEnabled(False)
        self._update_title()
        self._set_enabled(self._camera_id is not None)
        if self._camera_id is not None:
            self._ctrl.refresh_status(self._camera_id)
            self._ctrl.load_presets(self._camera_id)

    def take_snapshot(self) -> str:
        """Save the current camera's newest frame as PNG; returns the path ("" on failure)."""
        if self._camera_id is None:
            return ""
        image = self._ctrl.latest_image(self._camera_id)
        if image is None or image.isNull():
            self.snapshot_taken.emit("", "No video yet - wait for the picture, then try again.")
            return ""
        name = next((c.name for c in self._ctrl.cameras() if c.id == self._camera_id), self._camera_id)
        try:
            path = str(snapshot.save_snapshot(image, name))
        except (OSError, ValueError) as exc:
            log.warning("snapshot failed: %s", exc)
            self.snapshot_taken.emit("", f"Could not save the snapshot: {exc}")
            return ""
        self.snapshot_taken.emit(path, "")
        return path

    # ------------------------------------------------------------------ construction
    def _build_ptz(self) -> QGroupBox:
        box = QGroupBox("Pan / tilt", self)
        layout = QVBoxLayout(box)
        self._pad = PtzPad(box)
        self._pad.move_requested.connect(self._on_move)
        self._pad.stop_requested.connect(self._on_stop)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self._pad)
        row.addStretch(1)
        layout.addLayout(row)
        presets = QHBoxLayout()
        self._presets = QComboBox(box)
        self._presets.setObjectName("presetCombo")
        self._presets.setToolTip("Saved positions")
        self._goto = QPushButton("Go", box)
        self._goto.setObjectName("gotoPresetButton")
        self._goto.setToolTip("Move to the selected preset")
        self._goto.clicked.connect(self._on_goto)
        self._goto.setEnabled(False)
        presets.addWidget(self._presets, 1)
        presets.addWidget(self._goto)
        layout.addLayout(presets)
        return box

    def _build_settings(self) -> QGroupBox:
        box = QGroupBox("Camera", self)
        layout = QVBoxLayout(box)
        grid = QGridLayout()
        for index, (name, label, key, method, tip) in enumerate(_TOGGLES):
            btn = QPushButton(label, box)
            btn.setObjectName(name)
            btn.setCheckable(True)
            btn.setToolTip(tip)
            btn.clicked.connect(lambda checked, m=method: self._on_toggle(m, checked))
            self._toggles[key] = btn
            grid.addWidget(btn, *divmod(index, 2))
        layout.addLayout(grid)
        form = QFormLayout()
        self._day_night = QComboBox(box)
        self._day_night.setObjectName("dayNightCombo")
        for text, mode in _DAY_NIGHT:
            self._day_night.addItem(text, mode)
        self._day_night.activated.connect(self._on_day_night)
        form.addRow("Night vision", self._day_night)
        layout.addLayout(form)
        self._snapshot = QPushButton("Snapshot", box)
        self._snapshot.setObjectName("snapshotButton")
        self._snapshot.setToolTip("Save the current picture to Pictures\\Tapo Desktop")
        self._snapshot.clicked.connect(lambda _checked=False: self.take_snapshot())
        layout.addWidget(self._snapshot)
        return box

    # ------------------------------------------------------------------ user actions
    def _on_toggle(self, method: str, checked: bool) -> None:
        if self._camera_id is not None:
            getattr(self._ctrl, method)(self._camera_id, bool(checked))

    def _on_day_night(self, index: int) -> None:
        mode = self._day_night.itemData(index)
        if self._camera_id is not None and mode is not None:
            self._ctrl.set_day_night(self._camera_id, DayNightMode(mode))

    def _on_move(self, dx: int, dy: int) -> None:
        if self._camera_id is not None:
            self._ctrl.ptz_move(self._camera_id, dx, dy)

    def _on_stop(self) -> None:
        if self._camera_id is not None:
            self._ctrl.ptz_stop(self._camera_id)

    def _on_goto(self) -> None:
        preset_id = self._presets.currentData()
        if self._camera_id is not None and preset_id is not None:
            self._ctrl.goto_preset(self._camera_id, str(preset_id))

    # ------------------------------------------------------------------ controller signals
    def _on_status(self, camera_id: str, status: dict) -> None:
        if camera_id != self._camera_id:
            return
        for key, btn in self._toggles.items():
            if key in status:
                with QSignalBlocker(btn):
                    btn.setChecked(bool(status[key]))
        mode = status.get("day_night")
        if mode is not None:
            index = self._day_night.findData(DayNightMode(mode))
            if index >= 0:
                with QSignalBlocker(self._day_night):
                    self._day_night.setCurrentIndex(index)

    def _on_presets(self, camera_id: str, presets: list) -> None:
        if camera_id != self._camera_id:
            return
        with QSignalBlocker(self._presets):
            self._presets.clear()
            for preset in presets:
                self._presets.addItem(preset.name, preset.id)
        self._goto.setEnabled(self._presets.count() > 0)

    def _on_cameras_changed(self) -> None:
        if self._camera_id is not None and all(c.id != self._camera_id for c in self._ctrl.cameras()):
            self.set_camera(None)
        else:
            self._update_title()

    # ------------------------------------------------------------------ helpers
    def _set_enabled(self, enabled: bool) -> None:
        for child in self.findChildren(QGroupBox):
            child.setEnabled(enabled)

    def _update_title(self) -> None:
        name = None
        if self._camera_id is not None:
            name = next((c.name for c in self._ctrl.cameras() if c.id == self._camera_id), None)
        self._title.setText(name or "No camera selected")
