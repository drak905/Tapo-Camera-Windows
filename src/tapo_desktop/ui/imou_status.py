"""Small helpers the camera tile, grid and camera list use for an iMou (Open Platform) camera.

Nothing here draws or talks to a network. ``source == "imou"`` cameras are *click to start*: the grid never starts
their stream by itself (every minute of live view uses the iMou free quota); a click starts it, and it stops by itself
after the idle limit.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from tapo_desktop.core.imou import notes
from tapo_desktop.core.imou.errors import PREFIX, is_no_card_text
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


# ====================================================================== memory-card playback: how the panels behave
#: The recording list is read from the camera only when the user asks (Refresh); nothing polls.
REFRESH_HINT = "Press Refresh to read the recordings from the iMou camera"
NO_DOWNLOAD_TEXT = "Downloads are not available for iMou cameras"
SPEED_TIP = "iMou recordings play at 1x"
#: How long the list of one camera and day is shown again without asking the cloud.
LIST_TTL_S = 60.0


@dataclass(frozen=True)
class ListEntry:
    """A remembered answer: the clips, or the error text (``clips`` is None)."""

    clips: list | None = None
    error: str = ""


class ImouListCache:
    """Clip lists by camera and day, kept ``ttl`` seconds, and the "no memory card" answer of a camera, kept for every
    day until :meth:`forget` (a Refresh). Not thread-safe: the widgets use it on the GUI thread."""

    def __init__(self, ttl: float = LIST_TTL_S, clock: Callable[[], float] = time.monotonic):
        self.ttl, self.clock = ttl, clock
        self._lists: dict[tuple[str, date], tuple[float, list]] = {}
        self._no_card: dict[str, str] = {}

    def put_clips(self, camera_id: str, day: date, clips: list) -> None:
        self._lists[(camera_id, day)] = (self.clock(), list(clips))

    def put_no_card(self, camera_id: str, message: str) -> None:
        self._no_card[camera_id] = message

    def get(self, camera_id: str, day: date) -> ListEntry | None:
        if camera_id in self._no_card:
            return ListEntry(None, self._no_card[camera_id])
        hit = self._lists.get((camera_id, day))
        if hit is None:
            return None
        if self.clock() - hit[0] >= self.ttl:
            del self._lists[(camera_id, day)]
            return None
        return ListEntry(list(hit[1]))

    def forget(self, camera_id: str) -> None:
        self._no_card.pop(camera_id, None)
        for key in [k for k in self._lists if k[0] == camera_id]:
            del self._lists[key]

    def clear(self) -> None:
        self._lists.clear()
        self._no_card.clear()


#: The one cache the recordings panel and the player window share (a Refresh in the panel serves the first Play).
IMOU_LIST_CACHE = ImouListCache()


@dataclass(frozen=True)
class PlaybackProfile:
    """What the Playback tab and the player window do differently for a camera whose recordings are read on demand."""

    on_demand_list: bool = True
    allow_download: bool = False
    speeds: tuple = ("1/1",)
    hint_text: str = REFRESH_HINT
    no_download_text: str = NO_DOWNLOAD_TEXT
    speed_tip: str = SPEED_TIP
    cache: ImouListCache = field(default_factory=lambda: IMOU_LIST_CACHE, compare=False, repr=False)
    is_no_card: Callable[[str], bool] = field(default=is_no_card_text, compare=False, repr=False)
    note: Callable[[str], str] = field(default=notes.get, compare=False, repr=False)


PLAYBACK_PROFILE = PlaybackProfile()
