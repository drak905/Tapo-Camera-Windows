"""Application entry point (contract: docs/CONTRACT_UI.md).

``main(argv)``:

* Reuses ``QApplication.instance()`` if one exists, else creates one (argv[0] = "tapo-desktop").
* Data dir = ``default_data_dir()`` (honours ``TAPO_DESKTOP_HOME``); created if missing.
  ``ConfigStore(data_dir / "config.json")``, ``CredentialVault(data_dir / "vault.json")``
  (imported lazily), ``AppController(store, vault)``, ``MainWindow(controller, tray_enabled=...)``.
* ``--selftest``: builds everything above with ``tray_enabled=False``, shows the window, processes
  pending events, then ``controller.stop_all()`` and closes the window WITHOUT entering the event
  loop and WITHOUT any network I/O (no stream is started: the grid's ``start_view`` calls are
  allowed but must not block). Returns 0 on success, 1 if anything raised (error printed to stderr).
* Without ``--selftest``: runs ``app.exec()`` and returns its exit code.
* Unknown arguments -> usage on stderr, return 2.
"""
from __future__ import annotations

import logging
import sys
import traceback
from types import TracebackType

USAGE = "usage: tapo-desktop [--selftest] [--debug]\n" \
        "  --selftest  build the main window, process events once and exit (no network I/O)\n" \
        "  --debug     verbose logging\n"
_KNOWN = {"--selftest", "--debug"}

log = logging.getLogger(__name__)


def _parse(argv: list[str]) -> tuple[set[str], int | None]:
    """Return (flags, exit_code); exit_code is set when main should return immediately."""
    if any(a in ("-h", "--help") for a in argv):
        sys.stdout.write(USAGE)
        return set(), 0
    unknown = [a for a in argv if a not in _KNOWN]
    if unknown:
        sys.stderr.write(f"tapo-desktop: unknown argument(s): {' '.join(unknown)}\n{USAGE}")
        return set(), 2
    return set(argv), None


def _configure_logging(debug: bool, data_dir) -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        handlers.append(logging.FileHandler(data_dir / "tapo-desktop.log", encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _log_unhandled(exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
    """sys.excepthook for the GUI: PyQt6 would otherwise abort the process on a slot exception."""
    log.critical("unhandled exception", exc_info=(exc_type, exc, tb))


def main(argv: list[str] | None = None) -> int:
    """Run the desktop app; see the module docstring for the arguments and exit codes."""
    flags, code = _parse(list(sys.argv[1:] if argv is None else argv))
    if code is not None:
        return code
    selftest = "--selftest" in flags

    from PyQt6.QtWidgets import QApplication

    from tapo_desktop.services.config_store import ConfigStore, default_data_dir

    app = QApplication.instance() or QApplication(["tapo-desktop"])
    try:
        data_dir = default_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        if not selftest:
            _configure_logging("--debug" in flags, data_dir)
        window, controller, accounts = _build(app, ConfigStore(data_dir / "config.json"), data_dir,
                                              interactive=not selftest)
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 1

    if selftest:
        return _run_selftest(app, window, controller)
    previous_hook = sys.excepthook
    sys.excepthook = _log_unhandled
    try:
        window.show()
        _start_account(window, accounts)
        return int(app.exec())
    finally:
        sys.excepthook = previous_hook


def _build(app, store, data_dir, *, interactive: bool):
    """Create controller + main window; ``interactive`` adds the dark theme and the tray icon."""
    from tapo_desktop.core.auth.vault import CredentialVault
    from tapo_desktop.services.account import AccountManager
    from tapo_desktop.services.controller import AppController
    from tapo_desktop.ui import _theme
    from tapo_desktop.ui.main_window import MainWindow

    app.setApplicationName("Tapo Desktop")
    app.setOrganizationName("TapoDesktop")
    app.setWindowIcon(_theme.app_icon())
    if interactive:
        _theme.apply_theme(app)
        app.setQuitOnLastWindowClosed(False)  # the tray keeps the app alive
    controller = AppController(store, CredentialVault(data_dir / "vault.json"))
    accounts = AccountManager(controller, parent=controller)
    window = MainWindow(controller, tray_enabled=interactive, accounts=accounts)
    return window, controller, accounts


def _start_account(window, accounts) -> None:
    """Phase 2 startup, like the Tapo app: silent sign-in with the stored TP-Link ID, otherwise the
    sign-in dialog. "Use without account" keeps the manual cameras only and is remembered (setting
    ``"sign_in_skipped"``), so the dialog is not forced on every start; the account button still
    offers "Sign in...". Never used by --selftest."""
    try:
        if accounts.restore_session():
            return
    except Exception:  # a broken vault/config must not keep the app from starting
        log.exception("restoring the TP-Link ID session failed")
    from tapo_desktop.services.account import SETTING_SIGN_IN_SKIPPED

    store = accounts.config_store
    if store.get_setting(SETTING_SIGN_IN_SKIPPED, False):
        return
    if not window.show_sign_in() and accounts.account() is None:
        store.set_setting(SETTING_SIGN_IN_SKIPPED, True)


def _run_selftest(app, window, controller) -> int:
    try:
        window.show()
        app.processEvents()
        controller.stop_all()
        window.hide()
        app.processEvents()
        window.deleteLater()
        controller.deleteLater()
        return 0
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 1
