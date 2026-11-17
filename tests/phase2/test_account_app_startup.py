"""app.main startup flow: restore the session or show SignInDialog; --selftest unaffected."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtWidgets import QApplication, QDialog  # noqa: E402

pytestmark = pytest.mark.ui


@pytest.fixture
def patched_app(qapp, tmp_path, monkeypatch):
    """Run app.main() in-process without entering the event loop or touching the network."""
    from tapo_desktop import app
    from tapo_desktop.services import account
    from tapo_desktop.ui import _theme, sign_in_dialog

    home = tmp_path / "home"
    monkeypatch.setenv("TAPO_DESKTOP_HOME", str(home))
    monkeypatch.setattr(QApplication, "exec", lambda self=None: 0)
    monkeypatch.setattr(_theme, "apply_theme", lambda _app: None)
    monkeypatch.setattr(app, "_configure_logging", lambda *_a: None)

    dialogs: list = []

    class RecordingDialog:
        def __init__(self, accounts, parent=None):
            self.accounts, self.parent = accounts, parent
            dialogs.append(self)

        def exec(self):
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr(sign_in_dialog, "SignInDialog", RecordingDialog)

    def no_cloud(*_a, **_k):
        raise AssertionError("no real cloud client may be created in this test")

    monkeypatch.setattr(account, "_default_cloud_factory", no_cloud)
    monkeypatch.setattr(account, "_default_discover", lambda: [])
    quit_flag = qapp.quitOnLastWindowClosed()
    before = set(QApplication.topLevelWidgets())
    yield app, dialogs, home
    for w in set(QApplication.topLevelWidgets()) - before:
        w.hide()
        w.deleteLater()
    qapp.setQuitOnLastWindowClosed(quit_flag)
    qapp.processEvents()


def test_main_without_account_shows_sign_in_dialog(patched_app):
    app, dialogs, _home = patched_app
    assert app.main([]) == 0
    assert len(dialogs) == 1
    assert dialogs[0].accounts is not None and hasattr(dialogs[0].accounts, "sign_in")


def test_main_with_stored_account_restores_silently(patched_app, monkeypatch):
    from tapo_desktop.services import account

    app, dialogs, _home = patched_app
    calls = []
    monkeypatch.setattr(account.AccountManager, "restore_session", lambda self: calls.append(1) or True)
    assert app.main([]) == 0
    assert calls == [1]
    assert dialogs == []


def test_selftest_never_signs_in(patched_app, monkeypatch):
    from tapo_desktop.services import account

    app, dialogs, _home = patched_app
    calls = []
    monkeypatch.setattr(account.AccountManager, "restore_session", lambda self: calls.append(1) or True)
    assert app.main(["--selftest"]) == 0
    assert calls == [] and dialogs == []
