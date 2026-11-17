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


def main(argv: list[str] | None = None) -> int:
    raise NotImplementedError
