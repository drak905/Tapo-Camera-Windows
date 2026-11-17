"""System tray integration (contract: docs/CONTRACT_UI.md).

``TrayController(window, controller, *, clock=time.monotonic)`` owns a QSystemTrayIcon (works even
when no tray is available, e.g. the offscreen platform: then the balloon is simply not shown).
``controller.motion_event`` with ``event.active`` True produces a toast through ``self.notify(title,
message)`` (title = camera name) when setting ``"notifications"`` is True, at most one per camera
per ``TOAST_INTERVAL_S`` seconds measured with ``clock()``. Inactive (motion ended) events and
events for unknown camera ids never toast. Tests replace ``notify`` on the instance, so motion
handling MUST call ``self.notify``. Tray menu: Show / Quit (Quit -> ``window.quit()``).
"""
from __future__ import annotations

import time
from collections.abc import Callable

from PyQt6.QtCore import QObject

TOAST_INTERVAL_S = 10.0


class TrayController(QObject):
    def __init__(self, window, controller, *, clock: Callable[[], float] = time.monotonic):
        super().__init__(window)
        raise NotImplementedError

    def notify(self, title: str, message: str) -> None:
        """Show a tray balloon/toast (unthrottled)."""
        raise NotImplementedError
