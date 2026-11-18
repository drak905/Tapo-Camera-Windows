"""Camera names: validation, rename modes and list sorting (Phase 7, spec ``specs/rename_spec.json``)."""
from __future__ import annotations

from tapo_desktop.models import CameraConfig

MAX_NAME_BYTES = 32
PENDING_RENAME_TTL_S = 24 * 3600
ERR_ALIAS_FORMAT = -63103
SETTING_CAMERA_SORT = "camera_sort"
SORT_NAME_ASC = "name_asc"
SORT_NAME_DESC = "name_desc"
SORT_ACCOUNT = "account"
SORT_MODES = (SORT_NAME_ASC, SORT_NAME_DESC, SORT_ACCOUNT)


class InvalidCameraName(ValueError):
    """The name breaks the app's rules; ``str(exc)`` is the user-facing message."""


def validate_camera_name(text: str) -> str:
    raise NotImplementedError


def name_error(text: str) -> str:
    raise NotImplementedError


def renames_on_camera(cfg: CameraConfig) -> bool:
    raise NotImplementedError


def resolve_cloud_name(base: CameraConfig, alias: str, now: float) -> tuple[str, str, float]:
    raise NotImplementedError


def sort_mode(value) -> str:
    raise NotImplementedError


def sort_cameras(cameras: list[CameraConfig], mode: str, locale=None) -> list[CameraConfig]:
    raise NotImplementedError
