"""The call guard: one place that stops every iMou call after the free quota is used or a signature error.

* Quota codes (FL1002/FL1004/FL1005, OP1013/OP1014): all iMou calls stop until the next UTC midnight, with the text
  "free quota used". The state can be saved (``save``) so a restart does not forget it.
* Signature / App ID errors (SN1001...): no silent retry; calls stop for 120 s and the user has
  to click Test again.
Pure Python; the services layer plugs in ``load`` / ``save`` (a settings key).
"""
from __future__ import annotations

import math
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from tapo_desktop.core.imou.errors import (
    AUTH_CODES, AUTH_MESSAGE, QUOTA_CODES, QUOTA_MESSAGE, ImouAuthError, ImouBlockedError, ImouError, ImouQuotaError,
)

AUTH_BLOCK_SECONDS = 120.0


def next_utc_midnight(now: float) -> float:
    """Epoch of the next 00:00 UTC strictly after ``now``."""
    day = datetime.fromtimestamp(now, tz=timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return (day + timedelta(days=1)).timestamp()


class ImouGuard:
    def __init__(self, clock: Callable[[], float] = time.time, load: Callable[[], object] | None = None,
                 save: Callable[[dict | None], None] | None = None):
        self._clock = clock
        self._load = load
        self._save = save
        self._until = 0.0
        self._message = ""
        self._restore()

    def _restore(self) -> None:
        raw = self._load() if self._load is not None else None
        if not isinstance(raw, dict):
            return
        until, message = raw.get("until"), raw.get("message")
        if isinstance(until, (int, float)) and not isinstance(until, bool) and math.isfinite(until) \
                and isinstance(message, str):
            self._until, self._message = float(until), message

    def blocked(self) -> tuple[float, str] | None:
        """``(until, message)`` while calls are stopped, else ``None``."""
        if self._until > self._clock():
            return self._until, self._message
        return None

    def check(self) -> None:
        state = self.blocked()
        if state is not None:
            raise ImouBlockedError(state[1], state[0])

    def note(self, exc: BaseException) -> None:
        """Called by the client for every vendor error; starts a block when the code asks for it."""
        if not isinstance(exc, ImouError):
            return
        if isinstance(exc, ImouQuotaError) or exc.code in QUOTA_CODES:
            self._set(next_utc_midnight(self._clock()), QUOTA_MESSAGE)
        elif isinstance(exc, ImouAuthError) and exc.code in AUTH_CODES:
            self._set(self._clock() + AUTH_BLOCK_SECONDS, AUTH_MESSAGE)

    def clear(self) -> None:
        self._until, self._message = 0.0, ""
        if self._save is not None:
            self._save(None)

    def _set(self, until: float, message: str) -> None:
        self._until, self._message = until, message
        if self._save is not None:
            self._save({"until": until, "message": message})
