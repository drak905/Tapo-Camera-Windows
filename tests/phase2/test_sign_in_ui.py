"""SignInDialog: fields, inline errors (no message boxes), MFA section, skip."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtWidgets import QAbstractButton, QCheckBox, QDialog, QLabel, QLineEdit, QMessageBox  # noqa: E402

from tapo_desktop.models import AccountInfo  # noqa: E402
from tests.phase2.app_fakes import EMAIL, PASSWORD, FakeAccounts  # noqa: E402

pytestmark = pytest.mark.ui


@pytest.fixture(autouse=True)
def no_message_boxes(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("SignInDialog must report errors inline, not with a message box")

    for name in ("warning", "critical", "information", "question"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(boom))


def _dialog(qtbot, accounts=None):
    from tapo_desktop.ui.sign_in_dialog import SignInDialog

    accounts = accounts or FakeAccounts()
    dlg = SignInDialog(accounts)
    qtbot.addWidget(dlg)
    dlg.show()
    qtbot.waitExposed(dlg)
    return dlg, accounts


def _w(dlg, cls, name):
    w = dlg.findChild(cls, name)
    assert w is not None, name
    return w


def test_fields_exist_password_hidden_remember_default(qtbot):
    dlg, _ = _dialog(qtbot)
    _w(dlg, QLineEdit, "emailEdit")
    assert _w(dlg, QLineEdit, "passwordEdit").echoMode() == QLineEdit.EchoMode.Password
    assert _w(dlg, QCheckBox, "rememberCheck").isChecked()
    _w(dlg, QAbstractButton, "signInButton")
    _w(dlg, QAbstractButton, "skipButton")
    assert not _w(dlg, QLabel, "errorLabel").isVisible()
    for name in ("mfaCodeEdit",):
        assert not _w(dlg, QLineEdit, name).isVisible()
    for name in ("mfaSendButton", "mfaVerifyButton"):
        assert not _w(dlg, QAbstractButton, name).isVisible()


@pytest.mark.parametrize("email,password", [("", PASSWORD), ("not-an-email", PASSWORD), (EMAIL, "")])
def test_invalid_input_shows_inline_error_without_calling(qtbot, email, password):
    dlg, accounts = _dialog(qtbot)
    _w(dlg, QLineEdit, "emailEdit").setText(email)
    _w(dlg, QLineEdit, "passwordEdit").setText(password)
    _w(dlg, QAbstractButton, "signInButton").click()
    label = _w(dlg, QLabel, "errorLabel")
    assert label.isVisible() and label.text()
    assert accounts.calls == []


def test_sign_in_calls_manager_and_shows_failure_inline(qtbot):
    dlg, accounts = _dialog(qtbot)
    _w(dlg, QLineEdit, "emailEdit").setText("  " + EMAIL + " ")
    _w(dlg, QLineEdit, "passwordEdit").setText(PASSWORD)
    _w(dlg, QCheckBox, "rememberCheck").setChecked(False)
    button = _w(dlg, QAbstractButton, "signInButton")
    button.click()
    assert accounts.calls == [("sign_in", EMAIL, PASSWORD, False)]
    assert not button.isEnabled()  # busy while the request runs
    accounts.sign_in_failed.emit("Incorrect email or password.")
    label = _w(dlg, QLabel, "errorLabel")
    assert label.isVisible() and label.text() == "Incorrect email or password."
    assert button.isEnabled()
    assert dlg.isVisible()
    assert PASSWORD not in label.text()


def test_signed_in_accepts_dialog(qtbot):
    dlg, accounts = _dialog(qtbot)
    _w(dlg, QLineEdit, "emailEdit").setText(EMAIL)
    _w(dlg, QLineEdit, "passwordEdit").setText(PASSWORD)
    _w(dlg, QAbstractButton, "signInButton").click()
    accounts.signed_in.emit(AccountInfo(email=EMAIL))
    assert dlg.result() == QDialog.DialogCode.Accepted
    assert not dlg.isVisible()


def test_mfa_section_send_and_verify(qtbot):
    dlg, accounts = _dialog(qtbot)
    _w(dlg, QLineEdit, "emailEdit").setText(EMAIL)
    _w(dlg, QLineEdit, "passwordEdit").setText(PASSWORD)
    _w(dlg, QAbstractButton, "signInButton").click()
    accounts.mfa_required.emit([4, 2])
    code = _w(dlg, QLineEdit, "mfaCodeEdit")
    send = _w(dlg, QAbstractButton, "mfaSendButton")
    verify = _w(dlg, QAbstractButton, "mfaVerifyButton")
    assert code.isVisible() and verify.isVisible() and send.isVisible() and send.isEnabled()
    send.click()
    assert accounts.calls[-1] == ("request_mfa_code", 2)  # email preferred when offered
    code.setText("12a")
    verify.click()
    assert _w(dlg, QLabel, "errorLabel").isVisible()
    assert accounts.calls[-1] == ("request_mfa_code", 2)
    code.setText("123456")
    verify.click()
    assert accounts.calls[-1] == ("submit_mfa", "123456", 2)
    accounts.sign_in_failed.emit("The verification code is wrong or has expired.")
    assert "verification code" in _w(dlg, QLabel, "errorLabel").text()
    assert verify.isEnabled() and code.isVisible()


def test_mfa_authenticator_needs_no_send(qtbot):
    dlg, accounts = _dialog(qtbot)
    _w(dlg, QLineEdit, "emailEdit").setText(EMAIL)
    _w(dlg, QLineEdit, "passwordEdit").setText(PASSWORD)
    _w(dlg, QAbstractButton, "signInButton").click()
    accounts.mfa_required.emit([3])
    assert not _w(dlg, QAbstractButton, "mfaSendButton").isEnabled()
    _w(dlg, QLineEdit, "mfaCodeEdit").setText("654321")
    _w(dlg, QAbstractButton, "mfaVerifyButton").click()
    assert accounts.calls[-1] == ("submit_mfa", "654321", 3)


def test_use_without_account_rejects(qtbot):
    dlg, accounts = _dialog(qtbot)
    _w(dlg, QAbstractButton, "skipButton").click()
    assert dlg.result() == QDialog.DialogCode.Rejected
    assert accounts.calls == []
