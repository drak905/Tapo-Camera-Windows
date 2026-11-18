"""Camera names (Phase 7): validation, where a rename goes, cloud-refresh name resolution and list order.

Rules of the Tapo app [spec:rename.validation]: the text is trimmed and must be 1..32 bytes in UTF-8.
This app also composes the text to Unicode NFC first (a decomposed "ổ" would otherwise count 5 bytes
instead of 3) and refuses control characters (line breaks, tabs), which no camera name can show.

Where a rename goes [spec:rename.flow], [spec:rename.shared_permission]:

* owned cloud cameras (``source == "cloud"`` and ``role == 0``): ``setDeviceAlias`` on the camera (LAN, or
  the cloud passthrough when away), so the new name also shows in the Tapo app. The app makes no separate
  cloud call; the cloud picks the alias up from the camera. Until the cloud device list reports it, the
  name is kept as ``CameraConfig.pending_name`` (dropped when the cloud alias matches or after
  :data:`PENDING_RENAME_TTL_S`);
* shared cloud cameras (``role != 0``): a local ``CameraConfig.display_name`` that cloud refreshes never
  overwrite ("Reset to camera name" removes it);
* manual cameras (added by IP): ``CameraConfig.name`` itself (there is no cloud name).

List order: :func:`sort_cameras` with a :class:`QCollator` (locale-aware, natural numbers, case-insensitive),
so "Cam 2" < "Cam 10" and Vietnamese letters with diacritics sort next to their base letters.
"""
from __future__ import annotations

import functools
import unicodedata

from PyQt6.QtCore import QCollator, QLocale, Qt

from tapo_desktop.models import CameraConfig

#: Longest camera name in UTF-8 bytes (DeviceNameEditorViewModel.f134487a) [spec:rename.validation].
MAX_NAME_BYTES = 32
#: How long a name set on the camera is kept while the cloud still reports the old alias.
PENDING_RENAME_TTL_S = 24 * 3600
#: DEVICE_NAME_EXCESSIVE: the app's code for a refused name ("Incorrect alias format").
ERR_ALIAS_FORMAT = -63103
SETTING_CAMERA_SORT = "camera_sort"
SORT_NAME_ASC = "name_asc"
SORT_NAME_DESC = "name_desc"
SORT_ACCOUNT = "account"
SORT_MODES = (SORT_NAME_ASC, SORT_NAME_DESC, SORT_ACCOUNT)

MSG_EMPTY = "Enter a name."
MSG_CONTROL = "The name cannot contain line breaks or tabs."
MSG_REFUSED = "The camera refused this name. Use a shorter name with letters, numbers and spaces."


class InvalidCameraName(ValueError):
    """The name breaks the app's rules; ``str(exc)`` is the user-facing message."""


def _too_long(size: int) -> str:
    return (f"The name is too long ({size} of {MAX_NAME_BYTES} bytes). "
            "Letters with accents count as 2-3 bytes.")


def validate_camera_name(text: str) -> str:
    """The trimmed, NFC-composed name; :class:`InvalidCameraName` with a user-facing message otherwise."""
    name = unicodedata.normalize("NFC", str(text or "")).strip()
    if not name:
        raise InvalidCameraName(MSG_EMPTY)
    if any(unicodedata.category(ch) == "Cc" for ch in name):
        raise InvalidCameraName(MSG_CONTROL)
    size = len(name.encode("utf-8", "surrogatepass"))
    if size > MAX_NAME_BYTES:
        raise InvalidCameraName(_too_long(size))
    return name


def name_error(text: str) -> str:
    """The message :func:`validate_camera_name` would raise for ``text``, or ``""`` when it is valid."""
    try:
        validate_camera_name(text)
    except InvalidCameraName as exc:
        return str(exc)
    return ""


def renames_on_camera(cfg: CameraConfig) -> bool:
    """True if renaming ``cfg`` changes the camera's own name (owned cloud camera), False if the new name
    is kept in this app only (shared or manually added camera)."""
    return cfg.source == "cloud" and cfg.role == 0


def resolve_cloud_name(base: CameraConfig, alias: str, now: float) -> tuple[str, str, float]:
    """``(name, pending_name, pending_since)`` of a cloud camera whose cloud alias is ``alias`` at epoch
    ``now``: the local ``display_name`` wins, then a pending rename that the cloud has not reported yet
    (younger than :data:`PENDING_RENAME_TTL_S`), then ``alias``."""
    pending, since = base.pending_name, float(base.pending_since or 0.0)
    if pending and (pending == alias or not 0.0 <= now - since <= PENDING_RENAME_TTL_S):
        pending, since = "", 0.0
    if not pending:
        since = 0.0
    return base.display_name or pending or alias, pending, since


def sort_mode(value) -> str:
    """A valid :data:`SORT_MODES` value for the stored setting ``value`` (default :data:`SORT_NAME_ASC`)."""
    return value if isinstance(value, str) and value in SORT_MODES else SORT_NAME_ASC


def collator(locale: QLocale | None = None) -> QCollator:
    """Natural, case-insensitive collator for ``locale`` (None = ``QLocale()``, the system locale unless
    the application set another default)."""
    col = QCollator(locale if locale is not None else QLocale())
    col.setNumericMode(True)
    col.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
    return col


def sort_cameras(cameras: list[CameraConfig], mode: str, locale: QLocale | None = None) -> list[CameraConfig]:
    """``cameras`` in list order for ``mode``: by display name A-Z / Z-A (ties keep the account order) or,
    for :data:`SORT_ACCOUNT`, the account order itself. Unknown modes sort A-Z."""
    cams = list(cameras)
    mode = sort_mode(mode)
    if mode == SORT_ACCOUNT:
        return cams
    col = collator(locale)
    order = {id(c): i for i, c in enumerate(cams)}
    sign = -1 if mode == SORT_NAME_DESC else 1

    def compare(a: CameraConfig, b: CameraConfig) -> int:
        by_name = col.compare(a.name or a.id, b.name or b.id) * sign
        return by_name if by_name else order[id(a)] - order[id(b)]

    return sorted(cams, key=functools.cmp_to_key(compare))
