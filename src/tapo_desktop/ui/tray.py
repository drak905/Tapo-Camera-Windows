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
from PyQt6.QtWidgets import QMenu, QSystemTrayIcon

from tapo_desktop.ui import _theme

TOAST_INTERVAL_S = 10.0
_TOAST_MS = 5000


class TrayController(QObject):
    def __init__(self, window, controller, *, clock: Callable[[], float] = time.monotonic):
        super().__init__(window)
        self._window = window
        self._ctrl = controller
        self._clock = clock
        self._last_toast: dict[str, float] = {}

        self._menu = QMenu()
        show = self._menu.addAction("Show")
        show.triggered.connect(self.show_window)
        self._menu.addSeparator()
        quit_action = self._menu.addAction("Quit")
        quit_action.triggered.connect(self._quit)

        self._icon = QSystemTrayIcon(_theme.app_icon(), self)
        self._icon.setToolTip("Tepo Desktop")
        self._icon.setContextMenu(self._menu)
        self._icon.activated.connect(self._on_activated)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self._icon.show()

        controller.motion_event.connect(self._on_motion)

    # ------------------------------------------------------------------ public API
    def is_available(self) -> bool:
        """True when the platform shows tray icons (closing to the tray makes sense)."""
        return QSystemTrayIcon.isSystemTrayAvailable()

    def notify(self, title: str, message: str) -> None:
        """Show a tray balloon/toast (unthrottled)."""
        if self._icon.isVisible() and QSystemTrayIcon.supportsMessages():
            self._icon.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, _TOAST_MS)

    def hide(self) -> None:
        """Remove the icon from the tray (used on quit)."""
        self._icon.hide()

    def show_window(self) -> None:
        self._window.showNormal()
        self._window.raise_()
        self._window.activateWindow()

    # ------------------------------------------------------------------ slots
    def _quit(self) -> None:
        quit_fn = getattr(self._window, "quit", None)
        if callable(quit_fn):
            quit_fn()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show_window()

    def _on_motion(self, camera_id: str, event) -> None:
        if not getattr(event, "active", False):
            return
        name = next((c.name for c in self._ctrl.cameras() if c.id == camera_id), None)
        if name is None or not self._ctrl.config_store.get_setting("notifications"):
            return
        now = self._clock()
        last = self._last_toast.get(camera_id)
        if last is not None and now - last < TOAST_INTERVAL_S:
            return
        self._last_toast[camera_id] = now
        when = event.timestamp.astimezone().strftime("%H:%M:%S") if getattr(event, "timestamp", None) else ""
        self.notify(name, f"Motion detected at {when}" if when else "Motion detected")
