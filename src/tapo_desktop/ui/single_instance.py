"""Single running instance per Windows user.

Several copies of the app would each open live streams, and a Tapo camera serves only about two
media sessions, so later copies (and the phone app) would be refused. A second start connects to
the first instance's local socket, asks it to show its window, and exits.
"""
from __future__ import annotations

import getpass
import hashlib
import logging

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtNetwork import QLocalServer, QLocalSocket

log = logging.getLogger(__name__)
_CONNECT_TIMEOUT_MS = 500


def default_instance_name(data_dir: object | None = None) -> str:
    """Server name per Windows user and data folder (two copies sharing one config/vault are the
    conflict; a separate ``TAPO_DESKTOP_HOME`` is a separate instance). Hashed: safe as a pipe name."""
    if data_dir is None:
        from tapo_desktop.services.config_store import default_data_dir

        data_dir = default_data_dir()
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - any failure: fall back to one shared name
        user = "user"
    key = f"{user}|{str(data_dir).lower()}"
    return "TapoDesktop-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


class SingleInstance(QObject):
    """``acquire()`` -> True for the first instance (it then emits ``activated`` whenever another
    start asks it to come to the front); False when another instance already runs."""

    activated = pyqtSignal()

    def __init__(self, name: str | None = None, parent: QObject | None = None):
        super().__init__(parent)
        self._name = name or default_instance_name()
        self._server: QLocalServer | None = None

    def acquire(self) -> bool:
        probe = QLocalSocket(self)
        probe.connectToServer(self._name)
        if probe.waitForConnected(_CONNECT_TIMEOUT_MS):
            probe.write(b"activate\n")
            probe.flush()
            probe.waitForBytesWritten(_CONNECT_TIMEOUT_MS)
            probe.disconnectFromServer()
            log.info("Tapo Desktop is already running; asked it to show its window")
            return False
        probe.abort()
        QLocalServer.removeServer(self._name)  # stale pipe from a crashed instance
        server = QLocalServer(self)
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if not server.listen(self._name):
            log.warning("single-instance guard unavailable: %s", server.errorString())
            return True  # never block the app because of the guard itself
        server.newConnection.connect(self._on_connection)
        self._server = server
        return True

    def release(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None

    def _on_connection(self) -> None:
        server = self._server
        if server is None:
            return
        while server.hasPendingConnections():
            sock = server.nextPendingConnection()
            if sock is not None:
                sock.disconnectFromServer()
        self.activated.emit()
