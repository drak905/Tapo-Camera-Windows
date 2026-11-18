"""RenameDialog (Phase 7): objectNames ``renameEdit``, ``renameOkButton``, ``renameError``."""
from __future__ import annotations

from PyQt6.QtWidgets import QDialog, QWidget


class RenameDialog(QDialog):
    def __init__(self, name: str, *, on_camera: bool, parent: QWidget | None = None):
        super().__init__(parent)
        raise NotImplementedError

    def value(self) -> str:
        raise NotImplementedError
