"""Texts the camera tile shows for iMou cameras. Reasons that start with ``iMou: `` get a headline from :func:`tapo_desktop.ui.imou_status.headline_for`:\n"trying again", "daily time used" or "waiting" for the texts below that name those states, else "click to retry"."""
from __future__ import annotations

import time
from datetime import datetime

from tapo_desktop.core.imou.errors import AUTH_MESSAGE, PREFIX, QUOTA_MESSAGE, RATE_MESSAGE
from tapo_desktop.core.imou.guard import next_utc_midnight

UNSUPPORTED = "Not supported for iMou cameras yet"
CLICK_TEXT = PREFIX + "click to start live view"
DELAY_TEXT = "iMou live starts with a delay (up to 30 s)"
NOT_CONNECTED = PREFIX + "not connected. Use the account button, iMou connection."
OFFLINE_IN_LIST = PREFIX + "the camera is offline. Use Refresh iMou cameras to check again."
TRY_MARK = "Trying again in"                 # inside a text built by retrying()
BUDGET_MARK = "the daily live time for this camera"
#: Parts of the texts of states that only time, a Refresh or a changed setting can end (a click does not help at once).
WAIT_MARKS = (QUOTA_MESSAGE, RATE_MESSAGE, AUTH_MESSAGE, "the PC clock is more than 5 minutes off", "live views at once (limit)",
              "live view at once (limit)", "the camera is offline", "not connected")


def span(seconds: float) -> str:
    """``600 -> "10 minutes"``, ``0.3 -> "0 seconds"``."""
    if seconds >= 60:
        n = round(seconds / 60)
        return f"{n} minute{'s' if n != 1 else ''}"
    n = round(seconds)
    return f"{n} second{'s' if n != 1 else ''}"


def budget_used(daily_seconds: float, now: float | None = None) -> str:
    """The daily live time of one camera is used. The tile waits for a click; the day starts again at 00:00 UTC,
    shown in the PC's local time (``now`` is the runtime clock)."""
    again = datetime.fromtimestamp(next_utc_midnight(time.time() if now is None else now)).strftime("%H:%M")
    return (f"{PREFIX}the daily live time for this camera ({span(daily_seconds)}) is used. It starts again after "
            f"{again} (00:00 UTC) when you click the tile, or raise the limit in the iMou connection window.")


def cap_reached(max_live: int) -> str:
    return (f"{PREFIX}{max_live} live view{'s' if max_live != 1 else ''} at once (limit). "
            "Hide a tile or raise the limit in the iMou connection window.")


def retrying(text: str, seconds: float) -> str:
    """``text`` with the note that the app tries again by itself."""
    return f"{text} {TRY_MARK} {span(seconds)}."


AUTO_LIMIT_TEXT = PREFIX + "too many automatic restarts today. Click the tile to try again."
#: Shown on the live tile while a recording of the same camera plays (no "iMou: " prefix: no retry headline).
PLAYING_TEXT = "Playing a recording in the player window"
NO_RUN_AT = PREFIX + "no recording at that time"
#: The text PlayerWindow waits for at the end of the last recording.
END_OF_RECORDINGS = "end of recordings"


def run_limit(idle_seconds: float) -> str:
    return f"{PREFIX}playback stopped after {span(idle_seconds)}. Press Play to continue."
