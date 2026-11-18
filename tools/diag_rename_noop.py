"""Real-device check of the rename command path (Phase 7) WITHOUT changing any name: for one owned LAN camera
(default 192.168.1.10) and one owned camera reached through the TP-Link cloud, read the camera's current
alias, send setDeviceAlias with EXACTLY that alias through AppController.rename_camera (LAN client / cloud
passthrough) and read the alias again. Runs on a temporary copy of the app's config / vault / pins, so the
user's files are not modified. Prints no secrets. Usage: python tools/diag_rename_noop.py [lan_ip]"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src")]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from tapo_desktop.core.auth.vault import CredentialVault  # noqa: E402
from tapo_desktop.core.cloud.passthrough import RemoteControlClient  # noqa: E402
from tapo_desktop.core.control_client.client import TapoControlClient  # noqa: E402
from tapo_desktop.services.account import AccountManager  # noqa: E402
from tapo_desktop.services.camera_names import InvalidCameraName, validate_camera_name  # noqa: E402
from tapo_desktop.services.config_store import ConfigStore, default_data_dir  # noqa: E402
from tapo_desktop.services.controller import AppController  # noqa: E402

REPLIES: list[dict] = []


def _spy(cls) -> None:
    """Record the camera's reply item of every setDeviceAlias request (method, error_code, result)."""
    original = cls.multiple_request

    def multiple_request(self, reqs):
        items = original(self, reqs)
        for req, item in zip(reqs, items):
            if req.get("method") == "setDeviceAlias":
                REPLIES.append({"client": cls.__name__, "request": req, "reply": item})
        return items

    cls.multiple_request = multiple_request


def pump(app, cond, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


def main() -> int:
    lan_ip = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.10"
    _spy(TapoControlClient)
    _spy(RemoteControlClient)
    app = QApplication(["tapo-rename-check"])
    src = default_data_dir()
    tmp = Path(tempfile.mkdtemp(prefix="tapo-rename-check-"))
    for name in ("config.json", "vault.json", "pins.json"):
        if (src / name).exists():
            shutil.copy2(src / name, tmp / name)
    ctl = AppController(ConfigStore(tmp / "config.json"), CredentialVault(tmp / "vault.json"))
    acct = AccountManager(ctl)
    refreshed: list = []
    acct.cameras_refreshed.connect(lambda *a: refreshed.append(a))
    finished: dict[str, str] = {}
    ctl.rename_finished.connect(lambda cid, err: finished.__setitem__(cid, err))
    try:
        if not acct.restore_session():
            print("no stored account")
            return 2
        pump(app, lambda: bool(refreshed), 60)
        owned = [c for c in ctl.cameras() if c.source == "cloud" and c.role == 0 and c.online]
        picks = [("lan", next((c for c in owned if c.lan_found and c.host == lan_ip), None)),
                 ("remote", next((c for c in owned if not c.lan_found), None))]
        out = []
        for route, cfg in picks:
            if cfg is None:
                out.append({"route": route, "error": "no matching owned camera"})
                continue
            cam = ctl._cams[cfg.id]  # diagnostic: read the camera's own alias through the same service
            svc = ctl._service(cam)
            before = svc._control().get_device_info().alias
            entry = {"route": route, "camera": cfg.name, "model": cfg.model, "alias_before": before}
            try:
                if validate_camera_name(before) != before:
                    raise InvalidCameraName("alias would be normalised")
            except InvalidCameraName as exc:
                entry["skipped"] = f"not a safe no-op: {exc}"
                out.append(entry)
                continue
            t0 = time.monotonic()
            entry["on_camera"] = ctl.rename_camera(cfg.id, before)
            pump(app, lambda: cfg.id in finished, 60)
            entry["seconds"] = round(time.monotonic() - t0, 1)
            entry["rename_finished_error"] = finished.get(cfg.id, "<timeout>")
            entry["alias_after"] = svc._control().get_device_info().alias
            entry["unchanged"] = entry["alias_after"] == before
            out.append(entry)
        ctl.stop_all()
        print(json.dumps({"results": out, "replies": REPLIES}, indent=2, ensure_ascii=False, default=str))
        ok = all(o.get("unchanged") and o.get("rename_finished_error") == "" for o in out)
        return 0 if ok and len(out) == 2 else 1
    finally:
        acct.wait_idle(3000)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
