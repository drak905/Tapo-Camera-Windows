"""Small helpers the camera tile and camera list use for a Kami Home camera (unofficial).

Nothing here draws or talks to a network. The widgets ask these functions what to show, so no widget
needs its own ``source == "kami"`` check:

* :func:`control_enabled` reads the service capability flags (``supports_live_view``, ``supports_ptz``,
  ``supports_controls``, ``supports_playback``, ``supports_events``, ``supports_rename``). A service
  without a flag (every Tapo service) counts as "supported", so Tapo cameras keep every control.
* :func:`live_view_overlay_text` is the fixed tile text for a camera whose service cannot show live video.
  It is plain text: no spinner, no retry, nothing that changes with the stream state.
"""
from __future__ import annotations

from typing import Any

KAMI_SOURCE = "kami"
LIVE_VIEW_UNSUPPORTED_TEXT = "Live view is not supported for this Kami camera"
LIST_TOOLTIP = "Kami (unofficial)"
#: Setting: country code last chosen in the Kami sign-in dialog (not secret).
SETTING_KAMI_LAST_COUNTRY = "kami_last_country"

#: Control name -> service flag name. Names are the part after ``supports_``.
CONTROLS = ("live_view", "ptz", "controls", "playback", "events", "rename")


def is_kami(config: Any) -> bool:
    """True for a ``CameraConfig`` of a Kami camera."""
    return getattr(config, "source", "") == KAMI_SOURCE


def control_enabled(service: Any, control: str) -> bool:
    """True if ``control`` (one of :data:`CONTROLS`) should be enabled for this service."""
    if control not in CONTROLS:
        raise ValueError(f"unknown control {control!r}; expected one of {CONTROLS}")
    return bool(getattr(service, f"supports_{control}", True))


def capability_service(config: Any) -> Any:
    """Object whose ``supports_*`` flags apply to this camera: the ``KamiCameraService`` class for a Kami
    camera, ``None`` for everything else (a Tapo camera has no flags, so every control stays enabled)."""
    if not is_kami(config):
        return None
    from tapo_desktop.services.kami_camera_service import KamiCameraService

    return KamiCameraService


def live_view_overlay_text(service: Any) -> str:
    """The tile text for a camera without live view; an empty string when live view is supported."""
    return "" if control_enabled(service, "live_view") else LIVE_VIEW_UNSUPPORTED_TEXT


def online_label(online: bool) -> str:
    return "Online" if online else "Offline"


def list_tooltip(config: Any) -> str:
    """Tooltip for a camera list item: ``"Kami (unofficial)"`` for Kami, empty for everything else."""
    return LIST_TOOLTIP if is_kami(config) else ""
