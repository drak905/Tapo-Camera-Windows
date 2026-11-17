"""Camera controls (contract: docs/CONTRACT_UI.md). objectName ``controlsPanel``.

Children: ``privacyToggle``, ``ledToggle``, ``alarmToggle``, ``motionToggle`` (checkable buttons /
check boxes; user toggles call ``controller.set_privacy/set_led/set_alarm/set_motion_detection``),
``dayNightCombo`` (3 items whose ``itemData`` are ``DayNightMode.AUTO, ON, OFF`` in that order; a
user change calls ``controller.set_day_night``), ``ptzPad`` (wired to ``controller.ptz_move`` /
``ptz_stop``), ``presetCombo`` (item text = preset name, ``itemData`` = preset id) and
``gotoPresetButton`` (calls ``controller.goto_preset`` with the current preset id).

``set_camera(id)`` calls ``controller.refresh_status(id)`` and ``controller.load_presets(id)``.
``status_changed`` for the current camera updates the widgets WITHOUT calling the controller back;
``command_failed`` leaves the widgets showing the rolled-back status that follows it.
"""
from __future__ import annotations

from PyQt6.QtWidgets import QWidget


class ControlsPanel(QWidget):
    def __init__(self, controller, parent: QWidget | None = None):
        super().__init__(parent)
        raise NotImplementedError

    def camera_id(self) -> str | None:
        raise NotImplementedError

    def set_camera(self, camera_id: str | None) -> None:
        raise NotImplementedError
