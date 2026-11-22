"""Small helpers the camera tile, grid and camera list use for an iMou (Open Platform) camera.

Nothing here draws or talks to a network. ``source == "imou"`` cameras are *click to start*: the grid never starts
their stream by itself (every minute of live view uses the iMou free quota); a click starts it, and it stops by itself
after the idle limit.
"""
from __future__ import annotations

from typing import Any

from tapo_desktop.core.imou.errors import PREFIX
from tapo_desktop.core.imou.texts import CLICK_TEXT

IMOU_SOURCE = "imou"
LIST_TOOLTIP = "iMou (Open Platform)"
CLICK_HEADLINE = "iMou - click to start"
RETRY_HEADLINE = "iMou - click to retry"
#: Message shown instead of the Tapo camera dialog (a manual camera form makes no sense for an iMou camera).
EDIT_TEXT = "iMou cameras come from the iMou connection (account button, iMou connection). Use Refresh iMou cameras."


def is_imou(config: Any) -> bool:
    """True for a ``CameraConfig`` of an iMou camera."""
    return getattr(config, "source", "") == IMOU_SOURCE


def click_to_start(config: Any) -> bool:
    """True for a camera whose live view starts only on a click (all iMou cameras)."""
    return is_imou(config)


def headline_for(reason: str) -> str | None:
    """The tile headline for an iMou reason text, or ``None`` for any other text."""
    if reason == CLICK_TEXT:
        return CLICK_HEADLINE
    if reason.startswith(PREFIX):
        return RETRY_HEADLINE
    return None


def online_label(online: bool) -> str:
    return "Online" if online else "Offline"


def list_tooltip(config: Any) -> str:
    """Tooltip for a camera list item: ``"iMou (Open Platform)"`` for iMou, empty for everything else."""
    return LIST_TOOLTIP if is_imou(config) else ""
