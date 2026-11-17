"""Add / edit camera dialog (contract: docs/CONTRACT_UI.md).

Fields: ``nameEdit``, ``hostEdit``, ``cloudPasswordEdit``, ``cameraUserEdit``, ``cameraPasswordEdit``
(password fields use echo mode Password), ``dualLensCheck``.

``accept()`` validates and stays open (result not Accepted) when: host is empty/whitespace or
contains whitespace inside, or (add mode) the cloud password, camera user or camera password is
empty. Validation errors are shown inline (e.g. a QLabel `errorLabel`), NEVER through a
modal QMessageBox (tests call `accept()` directly on an offscreen dialog). Name/host/user are stripped of surrounding whitespace (passwords are NOT stripped). An
empty name defaults to the host.

``result_values()`` -> ``(CameraConfig(name, host, dual_lens=...), Credentials("admin", cloud_pw),
Credentials(camera_user, camera_pw))``. In edit mode (``config`` given) the fields are pre-filled
from ``config`` (password fields empty), the returned config keeps ``config.id`` and its other
fields, and an empty password field means "unchanged" (that Credentials is then ``None``).
In edit mode the camera account user is not pre-filled (it lives in the vault); a new camera
password therefore also needs the user name.
"""
from __future__ import annotations

import dataclasses

from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from tapo_desktop.models import CameraConfig, Credentials

CLOUD_USERNAME = "admin"


class CameraDialog(QDialog):
    def __init__(self, parent: QWidget | None = None, config: CameraConfig | None = None):
        super().__init__(parent)
        self._config = config
        self.setWindowTitle("Edit camera" if config is not None else "Add camera")
        self.setModal(True)
        self.setMinimumWidth(420)

        root = QVBoxLayout(self)
        intro = QLabel(
            "Find the IP address in the Tapo app (Camera settings > Device info).\n"
            "The cloud password is your TP-Link account password. The camera account is set up in\n"
            "the Tapo app under Advanced settings > Camera account (used for RTSP and ONVIF).", self)
        intro.setObjectName("hintLabel")
        intro.setWordWrap(True)
        root.addWidget(intro)

        form = QFormLayout()
        self._name = self._edit("nameEdit", "e.g. Front door")
        self._host = self._edit("hostEdit", "e.g. 192.168.1.50")
        self._cloud_pw = self._edit("cloudPasswordEdit", "TP-Link account password", password=True)
        self._cam_user = self._edit("cameraUserEdit", "Camera account user name")
        self._cam_pw = self._edit("cameraPasswordEdit", "Camera account password", password=True)
        self._dual = QCheckBox("Dual-lens camera (second lens on stream6/stream7)", self)
        self._dual.setObjectName("dualLensCheck")
        form.addRow("Name", self._name)
        form.addRow("IP address / host", self._host)
        form.addRow("Cloud password", self._cloud_pw)
        form.addRow("Camera account user", self._cam_user)
        form.addRow("Camera account password", self._cam_pw)
        form.addRow("", self._dual)
        root.addLayout(form)

        self._error = QLabel("", self)
        self._error.setObjectName("errorLabel")
        self._error.setWordWrap(True)
        self._error.hide()
        root.addWidget(self._error)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
                                   self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        if config is not None:
            self._name.setText(config.name)
            self._host.setText(config.host)
            self._dual.setChecked(config.dual_lens)
            for edit in (self._cloud_pw, self._cam_pw):
                edit.setPlaceholderText("unchanged")

    def _edit(self, name: str, placeholder: str, *, password: bool = False) -> QLineEdit:
        edit = QLineEdit(self)
        edit.setObjectName(name)
        edit.setPlaceholderText(placeholder)
        if password:
            edit.setEchoMode(QLineEdit.EchoMode.Password)
        return edit

    # ------------------------------------------------------------------ validation
    def validation_errors(self) -> list[str]:
        """Human-readable problems with the current input (empty = valid)."""
        errors: list[str] = []
        host = self._host.text().strip()
        if not host:
            errors.append("Enter the camera's IP address or host name.")
        elif any(ch.isspace() for ch in host):
            errors.append("The host must not contain spaces.")
        user = self._cam_user.text().strip()
        if self._config is None:
            if not self._cloud_pw.text():
                errors.append("Enter the cloud (TP-Link account) password.")
            if not user:
                errors.append("Enter the camera account user name.")
            if not self._cam_pw.text():
                errors.append("Enter the camera account password.")
        elif self._cam_pw.text() and not user:
            errors.append("Enter the camera account user name for the new camera password.")
        return errors

    def accept(self) -> None:
        errors = self.validation_errors()
        if errors:
            self._error.setText("\n".join(errors))
            self._error.show()
            return
        self._error.hide()
        super().accept()

    def result_values(self) -> tuple[CameraConfig, Credentials | None, Credentials | None]:
        host = self._host.text().strip()
        name = self._name.text().strip() or host
        dual = self._dual.isChecked()
        if self._config is not None:
            config = dataclasses.replace(self._config, name=name, host=host, dual_lens=dual)
        else:
            config = CameraConfig(name=name, host=host, dual_lens=dual)
        cloud_pw = self._cloud_pw.text()
        cam_pw = self._cam_pw.text()
        cloud = Credentials(CLOUD_USERNAME, cloud_pw) if cloud_pw else None
        camera = Credentials(self._cam_user.text().strip(), cam_pw) if cam_pw else None
        return config, cloud, camera
