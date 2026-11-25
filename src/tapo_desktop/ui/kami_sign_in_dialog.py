"""Sign in to a Kami Home account (unofficial). Looks like the Tapo sign-in dialog.

objectNames: ``emailEdit``, ``passwordEdit`` (masked), ``countryCombo`` (editable, type to search),
``signInButton``, ``cancelButton``, ``errorLabel`` (inline errors; no message boxes), ``infoLabel``,
and the three notes ``kamiNoteLabel``, ``phoneNoteLabel``, ``socialNoteLabel``.

The dialog only talks to a KamiAccountManager-like object: ``sign_in(email, password, country_code)``,
``blocked_until()`` and the signals ``signed_in`` (-> accept) and ``sign_in_failed`` (-> inline error).

Safety rules (plan review finding 10):

* One click sends exactly one ``sign_in``. The button stays off while the call runs, so a double click,
  Enter key or repeated click cannot send a second one.
* While ``manager.blocked_until()`` is in the future the button is off and shows the seconds left.
* No retry, no automatic second attempt. After a failure the password field is cleared.
* The password is never logged, never shown in a label and never stored here.

``countries`` is a list of ``(code, name)``; the integration step passes ``core.kami.regions.countries()``.
``last_country`` (a code) is pre-selected; ``country_chosen(code)`` fires on each sign-in click so the
caller can remember the choice.
"""
from __future__ import annotations

import math
import re
import time
from collections.abc import Callable, Sequence

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QCompleter,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

KAMI_NOTE = "Unofficial. Kami cameras show in the list; live view is not supported yet."
PHONE_NOTE = "Signing in here may sign you out of the Kami app on your phone."
SOCIAL_NOTE = ("If you sign in to Kami with Google or Apple, first set a password for your account "
               "in the Kami app. Then use that password here.")
SIGN_IN_TEXT = "Sign in"

_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


class KamiSignInDialog(QDialog):
    country_chosen = pyqtSignal(str)

    def __init__(self, manager, countries: Sequence[tuple[str, str]] | None = None,
                 parent: QWidget | None = None, *, last_country: str | None = None,
                 clock: Callable[[], float] = time.time):
        super().__init__(parent)
        self.setObjectName("kamiSignInDialog")
        self.setWindowTitle("Sign in to Kami Home - Tepo Desktop")
        self.setModal(True)
        self.setMinimumWidth(400)
        self._manager = manager
        self._clock = clock
        self._busy = False

        root = QVBoxLayout(self)
        title = QLabel("Sign in to Kami Home", self)
        title.setObjectName("kamiSignInTitle")
        font = title.font()
        font.setPointSizeF(font.pointSizeF() * 1.3)
        font.setBold(True)
        title.setFont(font)
        root.addWidget(title)
        for name, text in (("kamiNoteLabel", KAMI_NOTE), ("phoneNoteLabel", PHONE_NOTE),
                           ("socialNoteLabel", SOCIAL_NOTE)):
            note = QLabel(text, self)
            note.setObjectName(name)
            note.setWordWrap(True)
            root.addWidget(note)

        form = QFormLayout()
        self._email = QLineEdit(self)
        self._email.setObjectName("emailEdit")
        self._email.setPlaceholderText("name@example.com")
        self._password = QLineEdit(self)
        self._password.setObjectName("passwordEdit")
        self._password.setEchoMode(QLineEdit.EchoMode.Password)
        self._country = QComboBox(self)
        self._country.setObjectName("countryCombo")
        self._country.setEditable(True)
        self._country.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        for code, name in countries or ():
            self._country.addItem(name, code)
        completer = QCompleter(self._country.model(), self._country)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._country.setCompleter(completer)
        self._country.setCurrentIndex(-1)
        if last_country:
            index = self._country.findData(last_country)
            if index >= 0:
                self._country.setCurrentIndex(index)
        form.addRow("Email", self._email)
        form.addRow("Password", self._password)
        form.addRow("Country", self._country)
        root.addLayout(form)

        self._error = QLabel(self)
        self._error.setObjectName("errorLabel")
        self._error.setWordWrap(True)
        self._error.setStyleSheet("color: #ff6b6b;")
        self._error.hide()
        root.addWidget(self._error)
        self._info = QLabel(self)
        self._info.setObjectName("infoLabel")
        self._info.setWordWrap(True)
        self._info.hide()
        root.addWidget(self._info)

        buttons = QHBoxLayout()
        self._cancel = QPushButton("Cancel", self)
        self._cancel.setObjectName("cancelButton")
        self._cancel.setAutoDefault(False)
        self._cancel.clicked.connect(lambda _c=False: self.reject())
        self._sign_in = QPushButton(SIGN_IN_TEXT, self)
        self._sign_in.setObjectName("signInButton")
        self._sign_in.setDefault(True)
        self._sign_in.clicked.connect(lambda _c=False: self._on_sign_in())
        buttons.addWidget(self._cancel)
        buttons.addStretch(1)
        buttons.addWidget(self._sign_in)
        root.addLayout(buttons)

        manager.signed_in.connect(self._on_signed_in)
        manager.sign_in_failed.connect(self._on_failed)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.refresh_block_state)
        self.refresh_block_state()
        self._email.setFocus()

    # ------------------------------------------------------------------ public
    def selected_country(self) -> str | None:
        """Code of the country named in the box (matched by name, ignoring case), or None."""
        text = self._country.currentText().strip().casefold()
        for i in range(self._country.count()):
            if self._country.itemText(i).casefold() == text:
                return self._country.itemData(i)
        return None

    def refresh_block_state(self) -> None:
        """Re-read ``manager.blocked_until()`` and set the button text and state. Called once a second."""
        until = self._manager.blocked_until()
        left = math.ceil(until - self._clock()) if until is not None else 0
        if left > 0:
            self._sign_in.setText(f"{SIGN_IN_TEXT} (wait {left} s)")
            if not self._timer.isActive():
                self._timer.start()
                if not self._error.isVisibleTo(self):
                    self._show_info("Too many wrong tries. Please wait before you try again.")
        else:
            self._timer.stop()
            if self._sign_in.text() != SIGN_IN_TEXT:
                self._sign_in.setText(SIGN_IN_TEXT)
                self._info.hide()
        self._sign_in.setEnabled(left <= 0 and not self._busy)

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
        for w in (self._email, self._password, self._country):
            w.setEnabled(not busy)
        self.refresh_block_state()

    # ------------------------------------------------------------------ user action
    def _on_sign_in(self) -> None:
        if self._busy or not self._sign_in.isEnabled():
            return
        email, password = self._email.text().strip(), self._password.text()
        code = self.selected_country()
        if not _EMAIL_RE.fullmatch(email):
            self._show_error("Enter the email address of your Kami account.")
            self._email.setFocus()
            return
        if not password:
            self._show_error("Enter your password.")
            self._password.setFocus()
            return
        if code is None:
            self._show_error("Pick your country from the list.")
            self._country.setFocus()
            return
        self._show_info("Signing in\u2026")
        self._set_busy(True)
        self.country_chosen.emit(code)
        self._manager.sign_in(email, password, code)

    # ------------------------------------------------------------------ manager signals
    def _on_signed_in(self, _masked_email: str) -> None:
        self._password.clear()
        self._set_busy(False)
        self.accept()

    def _on_failed(self, message: str) -> None:
        self._password.clear()
        self._set_busy(False)
        self._show_error(message or "Sign-in failed.")
