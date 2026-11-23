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
* Without ``--selftest``: shows the window, then (Phase 2) restores the TP-Link ID session silently
  (``AccountManager.restore_session``) or, when no account is stored, shows ``SignInDialog``
  ("Use without account" is remembered in setting ``"sign_in_skipped"``), then runs ``app.exec()``
  and returns its exit code. ``--selftest`` never signs in and never shows the dialog.
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
    guard = None
    try:
        data_dir = default_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        if not selftest:
            _configure_logging("--debug" in flags, data_dir)
            from tapo_desktop.ui.single_instance import SingleInstance, default_instance_name

            guard = SingleInstance(default_instance_name(data_dir), parent=app)
            if not guard.acquire():
                return 0  # the running instance shows its window instead
        window, controller, accounts = _build(app, ConfigStore(data_dir / "config.json"), data_dir,
                                              interactive=not selftest)
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 1

    if selftest:
        return _run_selftest(app, window, controller)
    previous_hook = sys.excepthook
    sys.excepthook = _log_unhandled
    if guard is not None:
        guard.activated.connect(lambda: _bring_to_front(window))
    try:
        window.show()
        _start_account(window, accounts)
        return int(app.exec())
    finally:
        sys.excepthook = previous_hook
        if guard is not None:
            guard.release()


def _bring_to_front(window) -> None:
    """Another start of the app asked this instance to show itself (also from the tray)."""
    window.showNormal()
    window.raise_()
    window.activateWindow()


def _build(app, store, data_dir, *, interactive: bool):
    """Create controller + main window; ``interactive`` adds the dark theme and the tray icon."""
    from tapo_desktop.core.auth.vault import CredentialVault
    from tapo_desktop.services.account import AccountManager
    from tapo_desktop.services.controller import AppController
    from tapo_desktop.services.imou_account import ImouAccountManager
    from tapo_desktop.services.kami_account import KamiAccountManager
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
    kami_accounts = KamiAccountManager(controller, controller.vault, store, parent=controller)
    imou_accounts = ImouAccountManager(controller, controller.vault, store, parent=controller)
    if interactive:  # the iMou tiles start with the window, so the connection (from the vault, no network call) comes first
        try:
            imou_accounts.restore()
        except Exception:
            log.exception("restoring the iMou connection failed")
    window = MainWindow(controller, tray_enabled=interactive, accounts=accounts, kami_accounts=kami_accounts,
                        imou_accounts=imou_accounts)
    return window, controller, accounts


def _start_account(window, accounts) -> None:
    """Phase 2 startup, like the Tapo app: silent sign-in with the stored TP-Link ID, otherwise the
    sign-in dialog. "Use without account" keeps the manual cameras only and is remembered (setting
    ``"sign_in_skipped"``), so the dialog is not forced on every start; the account button still
    offers "Sign in...". Never used by --selftest.

    A stored Kami session is restored first, with its token and never a password. The Kami manager itself
    refuses after a failed attempt (setting ``kami_last_sign_in_failed``), so nothing is sent then."""
    kami = getattr(window, "kami_accounts", None)
    if kami is not None:
        try:
            kami.restore_session()
        except Exception:  # a broken vault/config must not keep the app from starting
            log.exception("restoring the Kami session failed")
    imou = getattr(window, "imou_accounts", None)
    if imou is not None:  # rebuilds the iMou connection from the vault in memory: no network call at start-up
        try:
            imou.restore()
        except Exception:
            log.exception("restoring the iMou connection failed")
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
