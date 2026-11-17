"""TP-Link ID account flow (contract: docs/CONTRACT_PHASE2.md, lane P2-app). STUB (red phase)."""
from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal

from tapo_desktop.models import AccountInfo


def _default_cloud_factory(terminal_uuid: str):
    raise NotImplementedError


def _default_discover():
    raise NotImplementedError


class AccountManager(QObject):
    signed_in = pyqtSignal(object)
    signed_out = pyqtSignal()
    mfa_required = pyqtSignal(object)
    sign_in_failed = pyqtSignal(str)
    cameras_refreshed = pyqtSignal(int, int)

    def __init__(self, controller, *, cloud_factory=None, discover=None, parent=None):
        super().__init__(parent)
        self._ctrl = controller

    def account(self) -> AccountInfo | None:
        raise NotImplementedError

    def sign_in(self, email: str, password: str, remember: bool = True) -> None:
        raise NotImplementedError

    def submit_mfa(self, code: str, mfa_type: int) -> None:
        raise NotImplementedError

    def request_mfa_code(self, mfa_type: int) -> None:
        raise NotImplementedError

    def restore_session(self) -> bool:
        raise NotImplementedError

    def refresh_cameras(self) -> None:
        raise NotImplementedError

    def sign_out(self) -> None:
        raise NotImplementedError

    def wait_idle(self, timeout_ms: int = 5000) -> bool:
        raise NotImplementedError
