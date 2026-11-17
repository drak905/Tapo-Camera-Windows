"""PTZ direction pad (contract: docs/CONTRACT_UI.md). objectName ``ptzPad``.

Buttons ``ptzUp``, ``ptzDown``, ``ptzLeft``, ``ptzRight`` are hold-to-move: press emits
``move_requested(dx, dy)``, release emits ``stop_requested()``. ``ptzStop`` click emits
``stop_requested()``. Convention: dx > 0 right, dx < 0 left, dy > 0 up, dy < 0 down; the other
axis is 0. Magnitude is implementation-defined (non-zero).
"""
from __future__ import annotations

from PyQt6.QtCore import QSize, pyqtSignal
from PyQt6.QtWidgets import QGridLayout, QPushButton, QWidget

#: Magnitude of one pad step (the camera service scales it to camera units).
PTZ_UNIT = 1

_BUTTONS: tuple[tuple[str, str, str, int, int, int, int], ...] = (
    # objectName, label, tooltip, row, col, dx, dy
    ("ptzUp", "\u25b2", "Tilt up (W / Up)", 0, 1, 0, PTZ_UNIT),
    ("ptzLeft", "\u25c0", "Pan left (A / Left)", 1, 0, -PTZ_UNIT, 0),
    ("ptzRight", "\u25b6", "Pan right (D / Right)", 1, 2, PTZ_UNIT, 0),
    ("ptzDown", "\u25bc", "Tilt down (S / Down)", 2, 1, 0, -PTZ_UNIT),
)


class PtzPad(QWidget):
    move_requested = pyqtSignal(int, int)
    stop_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("ptzPad")
        layout = QGridLayout(self)
        layout.setSpacing(4)
        layout.setContentsMargins(0, 0, 0, 0)
        for name, label, tip, row, col, dx, dy in _BUTTONS:
            btn = self._button(name, label, tip)
            btn.pressed.connect(lambda dx=dx, dy=dy: self.move_requested.emit(dx, dy))
            btn.released.connect(self.stop_requested.emit)
            layout.addWidget(btn, row, col)
        stop = self._button("ptzStop", "\u25a0", "Stop moving")
        stop.clicked.connect(self.stop_requested.emit)
        layout.addWidget(stop, 1, 1)

    def _button(self, name: str, label: str, tip: str) -> QPushButton:
        btn = QPushButton(label, self)
        btn.setObjectName(name)
        btn.setToolTip(tip)
        btn.setFixedSize(QSize(40, 36))
        btn.setAutoRepeat(False)
        return btn
