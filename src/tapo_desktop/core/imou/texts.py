"""Texts the camera tile shows for iMou cameras. Reasons that start with ``iMou: `` get the "click to retry" headline."""
from __future__ import annotations

from tapo_desktop.core.imou.errors import PREFIX

UNSUPPORTED = "Not supported for iMou cameras yet"
CLICK_TEXT = PREFIX + "click to start live view"
DELAY_TEXT = "iMou live starts with a delay (up to 30 s)"
NOT_CONNECTED = PREFIX + "not connected. Use the account button, iMou connection."
OFFLINE_IN_LIST = PREFIX + "the camera is offline. Use Refresh iMou cameras to check again."


def span(seconds: float) -> str:
    """``600 -> "10 minutes"``, ``0.3 -> "0 seconds"``."""
    if seconds >= 60:
        n = round(seconds / 60)
        return f"{n} minute{'s' if n != 1 else ''}"
    n = round(seconds)
    return f"{n} second{'s' if n != 1 else ''}"


def idle_stopped(idle_seconds: float) -> str:
    return f"{PREFIX}stopped after {span(idle_seconds)} of live view. Click to start again."


def budget_used(daily_seconds: float) -> str:
    return (f"{PREFIX}the daily live time for this camera ({span(daily_seconds)}) is used. "
            "Click again tomorrow (UTC) or raise the limit in the iMou connection window.")


def cap_reached(max_live: int) -> str:
    return f"{PREFIX}{max_live} live stream{'s are' if max_live != 1 else ' is'} already running. Stop one first."
