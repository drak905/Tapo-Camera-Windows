"""READ-ONLY real-device check through the app classes (AccountManager + AppController +
TapoCameraService): for one remote owned camera and one shared camera, read status and presets
and list today's recordings over the TP-Link cloud. Nothing is changed. Usage: python tools/diag_app_remote.py"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src")]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from tapo_desktop.core.auth.vault import CredentialVault  # noqa: E402
from tapo_desktop.services.account import AccountManager  # noqa: E402
from tapo_desktop.services.config_store import ConfigStore, default_data_dir  # noqa: E402
from tapo_desktop.services.controller import AppController  # noqa: E402


def pump(app, cond, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


def main() -> int:
    app = QApplication(["tapo-remote-check"])
    data = default_data_dir()
    ctl = AppController(ConfigStore(data / "config.json"), CredentialVault(data / "vault.json"))
    acct = AccountManager(ctl)
    refreshed: list = []
    acct.cameras_refreshed.connect(lambda *a: refreshed.append(a))
    acct.restore_session()
    pump(app, lambda: bool(refreshed), 40)
    cams = [c for c in ctl.cameras() if c.source == "cloud" and c.online and not c.lan_found]
    picks = [next((c for c in cams if getattr(c, "role", 0) == 0), None),
             next((c for c in cams if getattr(c, "role", 0) != 0), None)]
    status: dict = {}
    presets: dict = {}
    recs: dict = {}
    fails: list = []
    ctl.status_changed.connect(lambda cid, s: status.__setitem__(cid, s))
    ctl.command_failed.connect(lambda cid, action, msg: fails.append((cid, action, msg)))
    if hasattr(ctl, "presets_ready"):
        ctl.presets_ready.connect(lambda cid, lst: presets.__setitem__(cid, lst))
    ctl.recordings_ready.connect(lambda cid, day, lst: recs.__setitem__(cid, lst))
    out = []
    for cam in [p for p in picks if p]:
        t0 = time.monotonic()
        ctl.refresh_status(cam.id)
        if hasattr(ctl, "load_presets"):
            ctl.load_presets(cam.id)
        ctl.load_recordings(cam.id, date.today())
        pump(app, lambda: cam.id in status and cam.id in recs and (cam.id in presets or not hasattr(ctl, "presets_ready")), 45)
        out.append({"camera": cam.name, "role": getattr(cam, "role", 0), "seconds": round(time.monotonic() - t0, 1),
                    "status": {k: str(v) for k, v in status.get(cam.id, {}).items()},
                    "presets": len(presets.get(cam.id, []) or []), "recordings_today": len(recs.get(cam.id, []) or []),
                    "errors": [f for f in fails if f[0] == cam.id][:3]})
    ctl.stop_all()
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    return 0 if out and all(o["status"] and not o["errors"] for o in out) else 1


if __name__ == "__main__":
    sys.exit(main())
