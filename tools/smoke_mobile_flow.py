"""Mobile-parity smoke: the real app stack (AccountManager, controller, MainWindow, TapoCameraService,
TPAP control client, :8800 LocalStreamSupervisor) against a mock TP-Link cloud, a mock TDP responder
and two mock cameras speaking TPAP on 127.0.0.2/127.0.0.3 (ports 443 + 8800, like real cameras).

Flow checked: sign in with TP-Link ID -> cameras appear with cloud names -> found on LAN ->
live video in the grid -> privacy toggle reaches the camera -> snapshot. The first camera is in LAT
mode (like the real C200 fw 1.5.7 that rejected the password with -40401): it accepts only the cloud
local access token, which the app fetches from /v2/things and offers first [spec:lat.*].
Run: .venv\\Scripts\\python.exe tools\\smoke_mobile_flow.py   (writes artifacts/screenshots/mobile_flow.png)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
os.environ["TAPO_DESKTOP_HOME"] = tempfile.mkdtemp(prefix="tapo_mobile_")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from tapo_desktop.core.auth.vault import CredentialVault  # noqa: E402
from tapo_desktop.core.cloud.client import TapoCloudClient  # noqa: E402
from tapo_desktop.core.discovery.tdp import discover  # noqa: E402
from tapo_desktop.services.account import AccountManager  # noqa: E402
from tapo_desktop.services.config_store import ConfigStore, default_data_dir  # noqa: E402
from tapo_desktop.services.controller import AppController  # noqa: E402
from tapo_desktop.ui import _theme  # noqa: E402
from tapo_desktop.ui.main_window import MainWindow  # noqa: E402
from tests.fixtures.mock_cloud import MockTapoCloud, make_cloud_device  # noqa: E402
from tests.fixtures.mock_tapo_server import MockTapoCamera  # noqa: E402
from tests.fixtures.mock_tdp import MockTdpResponder  # noqa: E402
from tests.fixtures.phase2_fixtures import tdp_device_for  # noqa: E402

EMAIL, PASSWORD = "owner@example.com", "Tapo-Pass-123"
LAT = "f3a1c0de-local-access-token-7b2e"  # mock cloud LAT of the first camera


def pump(app, cond, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


def main() -> int:
    app = QApplication(["tapo-mobile-smoke"])
    _theme.apply_theme(app)
    devices = [make_cloud_device(1, alias="Phòng khách", deviceModel="C200"),
               make_cloud_device(2, alias="Cổng trước", deviceModel="C200")]
    cams = [MockTapoCamera(cloud_password=PASSWORD, camera_user="u", camera_password="p", model="C200",
                           host=ip, https_port=443, media_port=8800, protocol="tpap", **cred)
            for ip, cred in (("127.0.0.2", {"tpap_credential": "lat", "local_token": LAT}),
                             ("127.0.0.3", {"tpap_credential": "sha256_extra"}))]
    cloud = MockTapoCloud(accounts={EMAIL: PASSWORD}, devices=devices, local_tokens={devices[0]["deviceId"]: LAT})
    tdp = MockTdpResponder(devices=[tdp_device_for(d, ip=c.host, encrypt_type=["4"],
                                                   tpap={"pake": [2], "tls": 1, "noc": 1, "port": 443})
                                    for d, c in zip(devices, cams)])
    results: dict[str, object] = {}
    for s in (*cams, cloud, tdp):
        s.start()
    try:
        data = default_data_dir()
        data.mkdir(parents=True, exist_ok=True)
        ctl = AppController(ConfigStore(data / "config.json"), CredentialVault(data / "vault.json"))
        acct = AccountManager(ctl, cloud_factory=lambda uuid: TapoCloudClient(terminal_uuid=uuid, base_url=cloud.base_url),
                              discover=lambda: discover(timeout=1.5, targets=[("127.0.0.1", tdp.port)]))
        win = MainWindow(ctl, tray_enabled=False, accounts=acct)
        win.resize(1400, 860)
        win.show()
        events: list[str] = []
        acct.signed_in.connect(lambda a: events.append("signed_in"))
        acct.sign_in_failed.connect(lambda m: events.append("failed:" + m))
        frames: dict[str, int] = {}
        ctl.frame_ready.connect(lambda cid, img: frames.__setitem__(cid, frames.get(cid, 0) + 1))

        acct.sign_in("owner@example.com", "wrong-password")
        results["wrong_password_rejected"] = pump(app, lambda: any(e.startswith("failed:") for e in events), 15)
        results["nothing_stored_after_failure"] = len(ctl.cameras()) == 0

        acct.sign_in(EMAIL, PASSWORD)
        results["signed_in"] = pump(app, lambda: "signed_in" in events, 15)
        results["cameras_listed"] = pump(app, lambda: len(ctl.cameras()) == 2, 15)
        cfgs = ctl.cameras()
        results["names_from_cloud"] = sorted(c.name for c in cfgs) == sorted(d["alias"] for d in devices) \
            or sorted(c.name for c in cfgs)
        results["found_on_lan"] = all(c.lan_found and c.host in ("127.0.0.2", "127.0.0.3") for c in cfgs)
        win.set_grid_size(2)
        ids = [c.id for c in cfgs]
        for c in cfgs:  # the grid starts views itself; make sure in case it is not visible yet
            if not frames.get(c.id):
                pass
        results["live_video_over_8800"] = pump(app, lambda: all(frames.get(i, 0) > 10 for i in ids), 30)
        results["tpap_logins"] = [c.login_count for c in cams]
        results["tpap_share_attempts"] = [c.tpap_share_attempts for c in cams]
        results["lat_stored_for_lat_camera"] = ctl.uses_local_token(devices[0]["deviceId"]) is True
        results["no_lat_for_password_camera"] = ctl.uses_local_token(devices[1]["deviceId"]) is False
        results["lat_camera_one_share_per_login"] = cams[0].tpap_share_attempts == cams[0].login_count >= 1

        ctl.set_privacy(ids[0], True)
        target = next(c for c in cams if c.host == cfgs[0].host)
        results["privacy_reached_camera"] = pump(app, lambda: target.state.privacy is True, 10)

        pump(app, lambda: False, 1.5)
        shot = ROOT / "artifacts" / "screenshots"
        shot.mkdir(parents=True, exist_ok=True)
        win.grab().save(str(shot / "mobile_flow.png"))
        results["screenshot"] = str(shot / "mobile_flow.png")
        results["frames"] = {next(c.name for c in cfgs if c.id == i): n for i, n in frames.items()}
        ctl.stop_all()
    finally:
        for s in (*cams, cloud, tdp):
            s.stop()
    ok = all(v is True for k, v in results.items() if isinstance(v, bool)) and results.get("names_from_cloud") is True
    results["ok"] = ok
    print(json.dumps(results, indent=2, ensure_ascii=False, default=str))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
