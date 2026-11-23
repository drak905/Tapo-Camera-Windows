"""One place for "which capability flags apply to this camera" now that there is more than one non-Tapo camera type.

``capability_service(config)`` is what the controls panel, the playback panel, the grid and the main window ask:

* an iMou camera -> the :class:`~tapo_desktop.services.imou_camera_service.ImouCameraService` class (live view on,
  everything else off);
* any other camera -> whatever :func:`tapo_desktop.ui.kami_status.capability_service` says (unchanged: the Kami
  service class for a Kami camera, ``None`` for a Tapo camera, which keeps every control).
"""
from __future__ import annotations

from typing import Any

from tapo_desktop.ui import imou_status

IMOU_RECORDINGS_TEXT = "Recordings are not supported for this iMou camera yet"


def capability_service(config: Any) -> Any:
    if imou_status.is_imou(config):
        from tapo_desktop.services.imou_camera_service import ImouCameraService

        return ImouCameraService
    from tapo_desktop.ui.kami_status import capability_service as other_vendors

    return other_vendors(config)


def playback_profile(config: Any) -> imou_status.PlaybackProfile | None:
    """How the Playback tab and the player window treat this camera's recordings: ``None`` for a Tapo or Kami camera
    (their own code paths run, unchanged), a profile (read on Refresh, 1x, no download) for an iMou camera."""
    return imou_status.PLAYBACK_PROFILE if imou_status.is_imou(config) else None


def recordings_unsupported_text(config: Any, default: str) -> str:
    """The "no recordings" text for ``config``: iMou says iMou, everything else keeps ``default``."""
    return IMOU_RECORDINGS_TEXT if imou_status.is_imou(config) else default
