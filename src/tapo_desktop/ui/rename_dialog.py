"""RenameDialog (Phase 7): one line edit with inline validation.

objectNames: ``renameInfo`` (where the name changes: on the camera, also visible in the Tapo app, or only
in this app), ``renameEdit`` (pre-filled with the current name, selected), ``renameError`` (hidden while the
text is valid), ``renameOkButton`` (enabled only for a valid name that differs from the current one, like
the Tapo app's Save button [spec:rename.validation]) and ``renameCancelButton``.
"""
from __future__ import annotations

from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from tapo_desktop.services.camera_names import MAX_NAME_BYTES, name_error, validate_camera_name

INFO_ON_CAMERA = ("The new name is saved on the camera, so it also shows in the Tapo app and for everyone "
                  "the camera is shared with.")
INFO_LOCAL = ("The new name is used only in this app. The camera keeps its own name in the Tapo app "
              "(it is not your camera, or it was added by IP address).")


class RenameDialog(QDialog):
    """Ask for a new camera name; :meth:`value` is the validated name after ``exec()`` returned Accepted."""

    def __init__(self, name: str, *, on_camera: bool, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("renameDialog")
        self.setWindowTitle("Rename camera")
        self.setModal(True)
        self._current = name or ""
        root = QVBoxLayout(self)
        info = QLabel(INFO_ON_CAMERA if on_camera else INFO_LOCAL, self)
        info.setObjectName("renameInfo")
        info.setWordWrap(True)
        root.addWidget(info)
        self._edit = QLineEdit(self._current, self)
        self._edit.setObjectName("renameEdit")
        self._edit.setPlaceholderText("Camera name")
        self._edit.setToolTip(f"1 to {MAX_NAME_BYTES} bytes (letters with accents count as 2-3)")
        self._edit.selectAll()
        root.addWidget(self._edit)
        self._error = QLabel("", self)
        self._error.setObjectName("renameError")
        self._error.setWordWrap(True)
        self._error.setStyleSheet("color: #e06c6c;")
        self._error.hide()
        root.addWidget(self._error)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self._ok = QPushButton("Rename", self)
        self._ok.setObjectName("renameOkButton")
        self._ok.setDefault(True)
        self._ok.clicked.connect(self.accept)
        cancel = QPushButton("Cancel", self)
        cancel.setObjectName("renameCancelButton")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(self._ok)
        buttons.addWidget(cancel)
        root.addLayout(buttons)
        self._edit.textChanged.connect(self._validate)
        self._validate(self._edit.text())
        self.resize(420, self.sizeHint().height())

    def value(self) -> str:
        """The trimmed, validated name (``InvalidCameraName`` if the text is not valid)."""
        return validate_camera_name(self._edit.text())

    def _validate(self, text: str) -> None:
        error = name_error(text)
        self._error.setText(error)
        self._error.setVisible(bool(error))
        self._ok.setEnabled(not error and validate_camera_name(text) != self._current)

    def accept(self) -> None:
        if self._ok.isEnabled():
            super().accept()
