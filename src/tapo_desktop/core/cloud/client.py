"""TP-Link cloud client (stub)."""
from __future__ import annotations

from tapo_desktop.core.errors import TapoError
from tapo_desktop.models import AccountInfo, CloudDevice


class CloudError(TapoError):
    def __init__(self, message: str, code: int | str = "", result: dict | None = None):
        super().__init__(message)
        self.code = code
        self.result = result or {}


class CloudAuthError(CloudError):
    pass


class CloudMfaRequired(CloudError):
    def __init__(self, process_id: str, mfa_types: list[int]):
        super().__init__("verification code required", -20677)
        self.process_id = process_id
        self.mfa_types = mfa_types


def decode_alias(value: str) -> str:
    raise NotImplementedError


def make_ssl_context(verify_tls: bool = True):
    raise NotImplementedError


class TapoCloudClient:
    def __init__(self, *, terminal_uuid: str, base_url: str = "https://n-wap.i.tplinkcloud.com",
                 timeout: float = 15.0, app_version: str = "3.22.806", verify_tls: bool = True):
        self.terminal_uuid = terminal_uuid

    def login(self, email: str, password: str) -> AccountInfo:
        raise NotImplementedError

    def request_mfa_code(self, mfa_type: int) -> None:
        raise NotImplementedError

    def complete_mfa(self, code: str, mfa_type: int) -> AccountInfo:
        raise NotImplementedError

    def list_cameras(self) -> list[CloudDevice]:
        raise NotImplementedError

    @property
    def token(self) -> str | None:
        raise NotImplementedError

    @property
    def account(self) -> AccountInfo | None:
        raise NotImplementedError
