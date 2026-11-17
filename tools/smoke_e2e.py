"""Integrated smoke test: real app stack (TapoCameraService + core) against two mock cameras.

Run:  .venv\\Scripts\\python.exe tools\\smoke_e2e.py [--show]
Writes artifacts/screenshots/smoke_grid.png and prints a JSON result. Exit 0 = all checks passed.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
if "--show" not in sys.argv:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["TAPO_DESKTOP_HOME"] = tempfile.mkdtemp(prefix="tapo_smoke_")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from tapo_desktop.core.auth.vault import CredentialVault  # noqa: E402
from tapo_desktop.models import CameraConfig, Credentials, DayNightMode  # noqa: E402
from tapo_desktop.services.config_store import ConfigStore, default_data_dir  # noqa: E402
from tapo_desktop.services.controller import AppController  # noqa: E402
from tapo_desktop.ui import _theme  # noqa: E402
from tapo_desktop.ui.main_window import MainWindow  # noqa: E402
from tests.fixtures.mock_tapo_server import MockTapoCamera  # noqa: E402


def pump(app: QApplication, cond, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


def main() -> int:
    app = QApplication(["tapo-smoke"])
    _theme.apply_theme(app)
    cams = [MockTapoCamera(cloud_password="cloudpass", camera_user="camuser", camera_password="campass",
                           model=m) for m in ("C210", "C200")]
    for c in cams:
        c.start()
    results: dict[str, object] = {}
    try:
        data = default_data_dir()
        data.mkdir(parents=True, exist_ok=True)
        ctl = AppController(ConfigStore(data / "config.json"), CredentialVault(data / "vault.json"))
        frames: dict[str, int] = {}
        motion: list = []
        ctl.frame_ready.connect(lambda cid, img: frames.__setitem__(cid, frames.get(cid, 0) + 1))
        ctl.motion_event.connect(lambda cid, ev: motion.append(cid))
        ids = []
        for i, c in enumerate(cams):
            cfg = CameraConfig(name=f"Mock {c.state.__class__.__name__} {i + 1}", host=c.host,
                               https_port=c.https_port, rtsp_port=c.rtsp_port,
                               onvif_port=c.onvif_port, media_port=c.media_port)
            ctl.add_camera(cfg, Credentials("admin", "cloudpass"), Credentials("camuser", "campass"))
            ids.append(cfg.id)
        win = MainWindow(ctl, tray_enabled=False)
        win.resize(1400, 860)
        win.show()
        win.set_grid_size(2)
        results["frames_all_tiles"] = pump(app, lambda: all(frames.get(i, 0) > 10 for i in ids), 25)

        ctl.set_privacy(ids[0], True)
        ctl.set_day_night(ids[0], DayNightMode.ON)
        ctl.set_led(ids[1], False)
        ctl.ptz_move(ids[0], 1, 0)
        results["privacy_applied"] = pump(app, lambda: cams[0].state.privacy is True, 10)
        results["day_night_applied"] = pump(app, lambda: cams[0].state.inf_type == "on", 10)
        results["led_applied"] = pump(app, lambda: cams[1].state.led is False, 10)
        results["ptz_moved"] = pump(app, lambda: cams[0].state.ptz_x != 0, 10)

        clips: list = []
        ctl.recordings_ready.connect(lambda cid, day, lst: clips.extend(lst))
        ctl.load_recordings(ids[0], datetime.now(timezone.utc).astimezone().date())
        results["recordings_listed"] = pump(app, lambda: len(clips) > 0, 10)

        def fire_motion() -> bool:
            cams[1].emit_motion_event(True)
            return pump(app, lambda: ids[1] in motion, 4)
        results["onvif_motion_event"] = any(fire_motion() for _ in range(4))

        pump(app, lambda: False, 1.0)
        shot = ROOT / "artifacts" / "screenshots"
        shot.mkdir(parents=True, exist_ok=True)
        win.grab().save(str(shot / "smoke_grid.png"))
        results["screenshot"] = str(shot / "smoke_grid.png")
        results["frames"] = {i[:6]: n for i, n in frames.items()}

        t0 = time.monotonic()
        ctl.stop_all()
        results["stop_all_seconds"] = round(time.monotonic() - t0, 2)
        win.hide()
    finally:
        for c in cams:
            c.stop()
    ok = all(v for k, v in results.items() if isinstance(v, bool))
    results["ok"] = ok
    print(json.dumps(results, indent=2, default=str))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
