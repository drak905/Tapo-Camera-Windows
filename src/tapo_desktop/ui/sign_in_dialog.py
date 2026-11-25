"""Sign in with a TP-Link ID, like the Tapo phone app (contract: docs/CONTRACT_PHASE2.md).

objectNames: ``emailEdit``, ``passwordEdit``, ``rememberCheck`` (default checked), ``signInButton``,
``skipButton`` ("Use without account" -> reject), ``errorLabel`` (inline errors; no message boxes),
``infoLabel``, and the two-step verification section ``mfaSection`` with ``mfaTypeCombo``,
``mfaCodeEdit``, ``mfaSendButton`` (enabled for email / SMS codes) and ``mfaVerifyButton``.

The dialog only talks to an AccountManager-like object: ``sign_in``, ``request_mfa_code``,
``submit_mfa`` and the signals ``signed_in`` (-> accept), ``sign_in_failed`` (-> inline error) and
``mfa_required`` (-> show the MFA section).
"""
from __future__ import annotations

import re

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

#: MFA type code -> label (1 push, 2 email, 3 authenticator/TOTP, 4 SMS) [spec:cloud.mfa].
MFA_TYPES: dict[int, str] = {2: "Email code", 4: "SMS code", 3: "Authenticator app", 1: "Tapo app notification"}
#: Types for which the cloud must be asked to send a code first.
_SENDABLE = (2, 4)
_MFA_HINTS: dict[int, str] = {
    1: "Approve the sign-in in the Tapo app, then enter the code shown there.",
    2: "Press \u201cSend code\u201d and enter the code from the email.",
    3: "Enter the code from your authenticator app.",
    4: "Press \u201cSend code\u201d and enter the code from the text message.",
}
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_CODE_RE = re.compile(r"\d{4,8}")


class SignInDialog(QDialog):
    def __init__(self, accounts, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("signInDialog")
        self.setWindowTitle("Sign in - Tepo Desktop")
        self.setModal(True)
        self.setMinimumWidth(380)
        self._accounts = accounts
        self._waiting = False

        root = QVBoxLayout(self)
        title = QLabel("Sign in with your TP-Link ID", self)
        title.setObjectName("signInTitle")
        font = title.font()
        font.setPointSizeF(font.pointSizeF() * 1.3)
        font.setBold(True)
        title.setFont(font)
        root.addWidget(title)
        hint = QLabel("Use the account of the Tapo app. Your cameras on this network appear "
                      "automatically; no camera account is needed.", self)
        hint.setWordWrap(True)
        root.addWidget(hint)

        form = QFormLayout()
        self._email = QLineEdit(self)
        self._email.setObjectName("emailEdit")
        self._email.setPlaceholderText("name@example.com")
        self._password = QLineEdit(self)
        self._password.setObjectName("passwordEdit")
        self._password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Email", self._email)
        form.addRow("Password", self._password)
        root.addLayout(form)
        self._remember = QCheckBox("Keep me signed in", self)
        self._remember.setObjectName("rememberCheck")
        self._remember.setChecked(True)
        root.addWidget(self._remember)

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

        root.addWidget(self._build_mfa())

        buttons = QHBoxLayout()
        self._skip = QPushButton("Use without account", self)
        self._skip.setObjectName("skipButton")
        self._skip.setToolTip("Add cameras manually by IP address instead")
        self._skip.setAutoDefault(False)
        self._skip.clicked.connect(lambda _c=False: self.reject())
        self._sign_in = QPushButton("Sign in", self)
        self._sign_in.setObjectName("signInButton")
        self._sign_in.setDefault(True)
        self._sign_in.clicked.connect(lambda _c=False: self._on_sign_in())
        buttons.addWidget(self._skip)
        buttons.addStretch(1)
        buttons.addWidget(self._sign_in)
        root.addLayout(buttons)

        accounts.signed_in.connect(self._on_signed_in)
        accounts.sign_in_failed.connect(self._on_failed)
        accounts.mfa_required.connect(self._on_mfa_required)
        self._email.setFocus()

    # ------------------------------------------------------------------ construction
    def _build_mfa(self) -> QWidget:
        box = QWidget(self)
        box.setObjectName("mfaSection")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 6, 0, 0)
        label = QLabel("Two-step verification", box)
        font = label.font()
        font.setBold(True)
        label.setFont(font)
        layout.addWidget(label)
        self._mfa_hint = QLabel(box)
        self._mfa_hint.setWordWrap(True)
        layout.addWidget(self._mfa_hint)
        self._mfa_type = QComboBox(box)
        self._mfa_type.setObjectName("mfaTypeCombo")
        self._mfa_type.currentIndexChanged.connect(lambda _i: self._sync_mfa_type())
        layout.addWidget(self._mfa_type)
        row = QHBoxLayout()
        self._mfa_code = QLineEdit(box)
        self._mfa_code.setObjectName("mfaCodeEdit")
        self._mfa_code.setPlaceholderText("Verification code")
        self._mfa_code.setMaxLength(8)
        self._mfa_send = QPushButton("Send code", box)
        self._mfa_send.setObjectName("mfaSendButton")
        self._mfa_send.setAutoDefault(False)
        self._mfa_send.clicked.connect(lambda _c=False: self._on_send_code())
        self._mfa_verify = QPushButton("Verify", box)
        self._mfa_verify.setObjectName("mfaVerifyButton")
        self._mfa_verify.setAutoDefault(False)
        self._mfa_verify.clicked.connect(lambda _c=False: self._on_verify())
        row.addWidget(self._mfa_code, 1)
        row.addWidget(self._mfa_send)
        row.addWidget(self._mfa_verify)
        layout.addLayout(row)
        box.hide()
        self._mfa_box = box
        return box

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
        self._waiting = busy
        for w in (self._email, self._password, self._remember, self._sign_in, self._mfa_verify, self._mfa_code):
            w.setEnabled(not busy)
        self._sync_mfa_type()

    def _mfa_type_value(self) -> int | None:
        value = self._mfa_type.currentData()
        return int(value) if value is not None else None

    def _sync_mfa_type(self) -> None:
        kind = self._mfa_type_value()
        self._mfa_send.setEnabled(not self._waiting and kind in _SENDABLE)
        self._mfa_hint.setText(_MFA_HINTS.get(kind, "Enter the verification code."))

    # ------------------------------------------------------------------ user actions
    def _on_sign_in(self) -> None:
        email, password = self._email.text().strip(), self._password.text()
        if not _EMAIL_RE.fullmatch(email):
            self._show_error("Enter the email address of your TP-Link ID.")
            self._email.setFocus()
            return
        if not password:
            self._show_error("Enter your password.")
            self._password.setFocus()
            return
        self._mfa_box.hide()
        self._show_info("Signing in\u2026")
        self._set_busy(True)
        self._accounts.sign_in(email, password, self._remember.isChecked())

    def _on_send_code(self) -> None:
        kind = self._mfa_type_value()
        if kind is None:
            return
        self._accounts.request_mfa_code(kind)
        self._show_info("Code requested. It can take a minute to arrive.")
        self._mfa_code.setFocus()

    def _on_verify(self) -> None:
        kind = self._mfa_type_value()
        code = self._mfa_code.text().strip()
        if kind is None:
            return
        if not _CODE_RE.fullmatch(code):
            self._show_error("Enter the verification code (digits only).")
            self._mfa_code.setFocus()
            return
        self._show_info("Verifying\u2026")
        self._set_busy(True)
        self._accounts.submit_mfa(code, kind)

    # ------------------------------------------------------------------ manager signals
    def show_mfa(self, types: list[int]) -> None:
        """Open directly on the two-step verification section (pending login in the manager)."""
        self._on_mfa_required(types)

    def _on_signed_in(self, _account) -> None:
        self._set_busy(False)
        self.accept()

    def _on_failed(self, message: str) -> None:
        self._set_busy(False)
        self._show_error(message or "Sign-in failed.")

    def _on_mfa_required(self, types) -> None:
        self._set_busy(False)
        offered = [int(t) for t in (types or [])]
        self._mfa_type.blockSignals(True)
        self._mfa_type.clear()
        for kind in sorted(offered, key=lambda k: list(MFA_TYPES).index(k) if k in MFA_TYPES else 99):
            self._mfa_type.addItem(MFA_TYPES.get(kind, f"Method {kind}"), kind)
        self._mfa_type.setCurrentIndex(0)
        self._mfa_type.blockSignals(False)
        self._mfa_type.setVisible(self._mfa_type.count() > 1)
        self._sync_mfa_type()
        self._mfa_code.clear()
        self._mfa_box.show()
        self._show_info("This account uses two-step verification.")
        self._mfa_code.setFocus(Qt.FocusReason.OtherFocusReason)
