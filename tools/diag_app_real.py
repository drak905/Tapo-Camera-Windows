"""Real-device check of the full app path, exactly as the app starts: restore the stored TP-Link ID
session (cloud login + device list + local access tokens + TDP), then live view in the grid for the
cameras on this LAN. Offscreen; the screenshot goes to %TEMP% (it shows your own cameras) and is
not part of the repo. Usage: python tools/diag_app_real.py"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src")]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from tapo_desktop.core.auth.vault import CredentialVault  # noqa: E402
from tapo_desktop.services.account import AccountManager  # noqa: E402
from tapo_desktop.services.config_store import ConfigStore, default_data_dir  # noqa: E402
from tapo_desktop.services.controller import AppController  # noqa: E402
from tapo_desktop.ui import _theme  # noqa: E402
from tapo_desktop.ui.main_window import MainWindow  # noqa: E402


def pump(app, cond, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


def main() -> int:
    app = QApplication(["tapo-real-check"])
    _theme.apply_theme(app)
    data = default_data_dir()
    ctl = AppController(ConfigStore(data / "config.json"), CredentialVault(data / "vault.json"))
    acct = AccountManager(ctl)
    frames: dict[str, int] = {}
    states: dict[str, list[str]] = {}
    ctl.frame_ready.connect(lambda cid, img: frames.__setitem__(cid, frames.get(cid, 0) + 1))
    ctl.stream_state.connect(lambda cid, s, r: states.setdefault(cid, []).append(s + (f": {r}" if r else "")))
    refreshed: list = []
    acct.cameras_refreshed.connect(lambda total, lan: refreshed.append((total, lan)))
    win = MainWindow(ctl, tray_enabled=False, accounts=acct)
    win.resize(1400, 860)
    win.show()
    restored = acct.restore_session()
    pump(app, lambda: bool(refreshed), 40)
    local = [c for c in ctl.cameras() if c.lan_found and c.host]
    pump(app, lambda: all(frames.get(c.id, 0) > 30 for c in local), 30)
    shot = Path(tempfile.gettempdir()) / "tapo_real_grid.png"
    win.grab().save(str(shot))
    out = {"restored": restored, "refreshed": refreshed,
           "local_cameras": [{"name": c.name, "ip": c.host, "frames": frames.get(c.id, 0),
                              "states": states.get(c.id, [])[:4]} for c in local],
           "screenshot": str(shot)}
    ctl.stop_all()
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0 if local and all(frames.get(c.id, 0) > 30 for c in local) else 1


if __name__ == "__main__":
    sys.exit(main())
