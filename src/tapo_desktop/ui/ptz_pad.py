"""PTZ direction pad (contract: docs/CONTRACT_UI.md). objectName ``ptzPad``.

Buttons ``ptzUp``, ``ptzDown``, ``ptzLeft``, ``ptzRight`` are hold-to-move: press emits
``move_requested(dx, dy)``, release emits ``stop_requested()``. ``ptzStop`` click emits
``stop_requested()``. Convention: dx > 0 right, dx < 0 left, dy > 0 up, dy < 0 down; the other
axis is 0. Magnitude is implementation-defined (non-zero).
"""
from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QWidget


class PtzPad(QWidget):
    move_requested = pyqtSignal(int, int)
    stop_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        raise NotImplementedError
