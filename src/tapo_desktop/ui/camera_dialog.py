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
"""
from __future__ import annotations

from PyQt6.QtWidgets import QDialog, QWidget

from tapo_desktop.models import CameraConfig, Credentials

CLOUD_USERNAME = "admin"


class CameraDialog(QDialog):
    def __init__(self, parent: QWidget | None = None, config: CameraConfig | None = None):
        super().__init__(parent)
        raise NotImplementedError

    def accept(self) -> None:
        raise NotImplementedError

    def result_values(self) -> tuple[CameraConfig, Credentials | None, Credentials | None]:
        raise NotImplementedError
