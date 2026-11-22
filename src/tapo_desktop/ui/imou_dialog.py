"""iMou (Open Platform) connection dialog and the account-menu section.

``ImouConnectDialog``: App ID, App secret (masked), data center (a fixed dropdown; no free URL), the daily minutes per
camera and the live streams at once, and a Test button. One click sends exactly one ``manager.test`` (one token call and
one device-list call). The button stays off while the call runs and, after a refused key, while the manager reports a
block (the seconds left are on the button). After a failure the secret field is cleared. The secret is never shown,
logged or stored here; the manager keeps it in the DPAPI vault.

``ImouMenuSection`` adds three entries below the existing account-menu sections: iMou connection..., Refresh iMou
cameras, Disconnect iMou.
"""
from __future__ import annotations

import math
import time
from collections.abc import Callable

from PyQt6.QtCore import QObject, QTimer
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from tapo_desktop.core.imou import regions
from tapo_desktop.services.imou_account import DEFAULT_DAILY_MINUTES, DEFAULT_MAX_LIVE, MAX_DAILY_MINUTES, MAX_LIVE_CAP

NOTE = ("Uses your own iMou Open Platform app (App ID and App secret from open.imoulife.com, made with the same "
        "iMou account that holds the cameras). The free quota is small (2 GB of video and 30,000 requests per month): "
        "every live view uses it. Watch \"My Resources\" in the Open Platform console. Nothing is changed on your cameras.")
TEST_TEXT = "Test"
SECTION_TEXT = "iMou (Open Platform)"


class ImouConnectDialog(QDialog):
    def __init__(self, manager, parent: QWidget | None = None, *, last_data_center: str | None = None,
                 clock: Callable[[], float] = time.time):
        super().__init__(parent)
        self.setObjectName("imouConnectDialog")
        self.setWindowTitle("iMou (Open Platform) - Tapo Desktop")
        self.setModal(True)
        self.setMinimumWidth(440)
        self._manager = manager
        self._clock = clock
        self._busy = False

        root = QVBoxLayout(self)
        title = QLabel("iMou connection (Open Platform)", self)
        title.setObjectName("imouTitle")
        font = title.font()
        font.setPointSizeF(font.pointSizeF() * 1.3)
        font.setBold(True)
        title.setFont(font)
        root.addWidget(title)
        note = QLabel(NOTE, self)
        note.setObjectName("imouNoteLabel")
        note.setWordWrap(True)
        root.addWidget(note)

        form = QFormLayout()
        self._app_id = QLineEdit(self)
        self._app_id.setObjectName("imouAppIdEdit")
        self._secret = QLineEdit(self)
        self._secret.setObjectName("imouSecretEdit")
        self._secret.setEchoMode(QLineEdit.EchoMode.Password)
        self._combo = QComboBox(self)
        self._combo.setObjectName("imouDataCenterCombo")
        self._combo.setEditable(False)
        for dc, label in regions.labels():
            self._combo.addItem(label, dc)
        index = self._combo.findData(last_data_center) if last_data_center else -1
        self._combo.setCurrentIndex(max(0, index))
        self._minutes = QSpinBox(self)
        self._minutes.setObjectName("imouDailyMinutesSpin")
        self._minutes.setRange(1, MAX_DAILY_MINUTES)
        self._minutes.setValue(self._current(manager, "daily_minutes", DEFAULT_DAILY_MINUTES))
        self._minutes.setSuffix(" min")
        self._live = QSpinBox(self)
        self._live.setObjectName("imouMaxLiveSpin")
        self._live.setRange(1, MAX_LIVE_CAP)
        self._live.setValue(self._current(manager, "max_live", DEFAULT_MAX_LIVE))
        form.addRow("App ID", self._app_id)
        form.addRow("App secret", self._secret)
        form.addRow("Data center", self._combo)
        form.addRow("Live minutes per camera and day", self._minutes)
        form.addRow("Live streams at once", self._live)
        root.addLayout(form)

        self._error = QLabel(self)
        self._error.setObjectName("imouErrorLabel")
        self._error.setWordWrap(True)
        self._error.setStyleSheet("color: #ff6b6b;")
        self._error.hide()
        root.addWidget(self._error)
        self._info = QLabel(self)
        self._info.setObjectName("imouInfoLabel")
        self._info.setWordWrap(True)
        self._info.hide()
        root.addWidget(self._info)

        buttons = QHBoxLayout()
        self._close = QPushButton("Close", self)
        self._close.setObjectName("imouCloseButton")
        self._close.setAutoDefault(False)
        self._close.clicked.connect(lambda _c=False: self.reject())
        self._test = QPushButton(TEST_TEXT, self)
        self._test.setObjectName("imouTestButton")
        self._test.setDefault(True)
        self._test.clicked.connect(lambda _c=False: self._on_test())
        buttons.addWidget(self._close)
        buttons.addStretch(1)
        buttons.addWidget(self._test)
        root.addLayout(buttons)

        self._secret.returnPressed.connect(self._on_test)
        manager.connected.connect(self._on_connected)
        manager.connect_failed.connect(self._on_failed)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.refresh_block_state)
        self.refresh_block_state()
        self._app_id.setFocus()

    @staticmethod
    def _current(manager, name: str, default: int) -> int:
        try:
            value = getattr(manager, name)()
            return value if isinstance(value, int) and not isinstance(value, bool) else default
        except Exception:
            return default

    # ------------------------------------------------------------------ public
    def refresh_block_state(self) -> None:
        """Re-read ``manager.blocked_until()`` and set the button text and state. Called once a second."""
        until = self._manager.blocked_until()
        left = math.ceil(until - self._clock()) if until is not None else 0
        if left > 0:
            wait = f"{left} s" if left < 120 else f"{math.ceil(left / 60)} min" if left < 7200 else f"{math.ceil(left / 3600)} h"
            self._test.setText(f"{TEST_TEXT} (wait {wait})")
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()
            if self._test.text() != TEST_TEXT:
                self._test.setText(TEST_TEXT)
        self._test.setEnabled(left <= 0 and not self._busy)

    # ------------------------------------------------------------------ helpers
    def _show_error(self, text: str) -> None:
        self._info.hide()
        self._error.setText(text)
        self._error.show()

    def _show_info(self, text: str) -> None:
        self._error.hide()
        self._info.setText(text)
        self._info.show()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for w in (self._app_id, self._secret, self._combo, self._minutes, self._live):
            w.setEnabled(not busy)
        self.refresh_block_state()

    # ------------------------------------------------------------------ user action
    def _on_test(self) -> None:
        if self._busy or not self._test.isEnabled():
            return
        app_id, secret = self._app_id.text().strip(), self._secret.text()
        data_center = self._combo.currentData()
        if not app_id:
            self._show_error("Enter the App ID of your iMou app.")
            self._app_id.setFocus()
            return
        if not secret:
            self._show_error("Enter the App secret of your iMou app.")
            self._secret.setFocus()
            return
        self._show_info("Testing the connection (one token call and one device-list call)\u2026")
        self._set_busy(True)
        self._manager.set_limits(self._minutes.value(), self._live.value())
        self._manager.test(app_id, secret, data_center)

    # ------------------------------------------------------------------ manager signals
    def _on_connected(self, _label: str) -> None:
        self._secret.clear()
        self._set_busy(False)
        self.accept()

    def _on_failed(self, message: str) -> None:
        self._secret.clear()
        self._set_busy(False)
        self._show_error(message or "iMou: the test failed.")


class ImouMenuSection(QObject):
    """Adds the iMou entries to the account menu and keeps them in step with the manager."""

    def __init__(self, window, menu: QMenu, manager, store):
        super().__init__(window)
        self._window, self._manager, self._store = window, manager, store
        self._dialog: ImouConnectDialog | None = None
        menu.addSection(SECTION_TEXT).setObjectName("accountImouHeader")
        self.connect_action = self._add(menu, "iMou connection\u2026", "imouConnectAction", self.show_dialog)
        self.refresh_action = self._add(menu, "Refresh iMou cameras", "imouRefreshAction", manager.refresh)
        self.refresh_action.setToolTip("Read the iMou camera list and the online state (one call, no polling)")
        self.disconnect_action = self._add(menu, "Disconnect iMou", "imouDisconnectAction", manager.disconnect)
        manager.connected.connect(lambda _label: self.sync())
        manager.disconnected.connect(self._on_disconnected)
        manager.connect_failed.connect(self._on_failed)
        self.sync()

    def _add(self, menu: QMenu, text: str, name: str, slot: Callable[[], object]) -> QAction:
        action = menu.addAction(text)
        action.setObjectName(name)
        action.triggered.connect(lambda _checked=False: slot())
        return action

    def sync(self) -> None:
        on = self._manager.is_connected()
        self.connect_action.setEnabled(True)
        self.refresh_action.setEnabled(on)
        self.disconnect_action.setEnabled(on)
        self.connect_action.setToolTip(f"Connected to iMou as {self._manager.account_label()}" if on
                                       else "Connect to the iMou Open Platform (App ID and App secret)")

    def show_dialog(self) -> bool:
        """Run the connection dialog modally; True when the test succeeded. The data center is remembered."""
        if self._dialog is not None:
            return False
        dialog = ImouConnectDialog(self._manager, self._window,
                                   last_data_center=getattr(self._manager, "data_center", lambda: None)())
        self._dialog = dialog
        try:
            return dialog.exec() == QDialog.DialogCode.Accepted
        finally:
            self._dialog = None
            dialog.deleteLater()

    def _on_disconnected(self) -> None:
        self.sync()
        self._window.statusBar().showMessage("Disconnected from iMou", 5000)

    def _on_failed(self, message: str) -> None:
        self.sync()
        if self._dialog is None:  # the dialog shows its own errors inline
            self._window.statusBar().showMessage(message, 10000)
