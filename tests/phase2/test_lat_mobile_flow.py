"""End to end: the mobile flow with LAT-mode cameras (the real C200 fw 1.5.7 situation).

Real AccountManager + AppController + TapoCameraService + TapoCloudClient + TDP discovery + DPAPI
vault against MockTapoCloud (issues localAccessToken via /v2/things), MockTdpResponder and two TPAP
mock cameras: one in LAT mode (accepts only SHA256_HEX_UPPER(LAT), :8800 digest with the LAT) and
one still on the TP-Link ID password. Both must log in and stream. Never contacts the real cloud.
"""
from __future__ import annotations

import dataclasses
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from tapo_desktop.core.control_client import tpap  # noqa: E402
from tapo_desktop.models import CredentialKind, StreamQuality  # noqa: E402
from tests.fixtures.mock_cloud import make_cloud_device  # noqa: E402
from tests.fixtures.mock_tapo_server import MockTapoCamera  # noqa: E402
from tests.fixtures.phase2_fixtures import tdp_device_for  # noqa: E402

pytestmark = [pytest.mark.integration, pytest.mark.ui, pytest.mark.timeout(120)]

EMAIL, PASSWORD = "owner@example.com", "Tapo-Pass-123"
LAT = "f3a1c0de-local-access-token-7b2e"
TPAP_HINT = {"pake": [2], "tls": 1, "noc": 1, "port": 443}


@pytest.fixture
def rig(tmp_path, qtbot, mock_cloud_factory, mock_tdp_factory, ffmpeg_path, monkeypatch):
    from tapo_desktop.core.auth.vault import CredentialVault
    from tapo_desktop.core.cloud.client import TapoCloudClient
    from tapo_desktop.core.discovery.tdp import discover
    from tapo_desktop.services.account import AccountManager
    from tapo_desktop.services.camera_service import TapoCameraService
    from tapo_desktop.services.config_store import ConfigStore
    from tapo_desktop.services.controller import AppController

    monkeypatch.setattr(tpap, "RELOGIN_RETRY_DELAY_S", 0.05)
    monkeypatch.setenv("TAPO_FFMPEG", ffmpeg_path)
    devices = [make_cloud_device(1, alias="Phong khach", deviceModel="C200"),
               make_cloud_device(2, alias="Cong truoc", deviceModel="C200")]
    cams = [MockTapoCamera(cloud_password=PASSWORD, model="C200", protocol="tpap", tpap_credential="lat",
                           local_token=LAT).start(),
            MockTapoCamera(cloud_password=PASSWORD, model="C200", protocol="tpap",
                           tpap_credential="sha256_extra").start()]
    try:
        cloud = mock_cloud_factory(accounts={EMAIL: PASSWORD}, devices=devices,
                                   local_tokens={devices[0]["deviceId"]: LAT})
        tdp = mock_tdp_factory(devices=[tdp_device_for(d, ip=c.host, http_port=c.https_port, encrypt_type=["4"],
                                                       tpap=TPAP_HINT) for d, c in zip(devices, cams)])
        media_by_https = {c.https_port: c.media_port for c in cams}
        vault = CredentialVault(tmp_path / "vault.json")

        def service_factory(cfg):
            return TapoCameraService(dataclasses.replace(cfg, media_port=media_by_https.get(cfg.https_port, 8800)),
                                     vault)

        ctl = AppController(ConfigStore(tmp_path / "config.json"), vault, service_factory=service_factory)
        acct = AccountManager(ctl, cloud_factory=lambda u: TapoCloudClient(terminal_uuid=u, base_url=cloud.base_url,
                                                                           timeout=5.0),
                              discover=lambda: discover(timeout=1.0, targets=[("127.0.0.1", tdp.port)]))
        yield ctl, acct, vault, cams, devices, cloud
        acct.wait_idle(3000)
        ctl.stop_all()
    finally:
        for c in cams:
            c.stop()


def test_lat_cameras_sign_in_find_and_stream(rig, qtbot):
    ctl, acct, vault, cams, devices, cloud = rig
    frames: dict[str, int] = {}
    failures: list[tuple] = []
    ctl.frame_ready.connect(lambda cid, img: frames.__setitem__(cid, frames.get(cid, 0) + 1))
    ctl.command_failed.connect(lambda *a: failures.append(a))
    with qtbot.waitSignal(acct.signed_in, timeout=15000):
        acct.sign_in(EMAIL, PASSWORD)
    assert acct.wait_idle(15000)
    qtbot.waitUntil(lambda: len(ctl.cameras()) == 2, timeout=10000)
    lat_id, pw_id = devices[0]["deviceId"], devices[1]["deviceId"]
    assert vault.get(lat_id, CredentialKind.LOCAL_TOKEN).password == LAT
    assert vault.get(pw_id, CredentialKind.LOCAL_TOKEN) is None
    assert all(c.lan_found and c.control_protocol == "tpap" for c in ctl.cameras())
    assert ctl.uses_local_token(lat_id) and not ctl.uses_local_token(pw_id)

    for cid in (lat_id, pw_id):
        ctl.start_view(cid, StreamQuality.SD, 320, 180)
    qtbot.waitUntil(lambda: frames.get(lat_id, 0) > 5 and frames.get(pw_id, 0) > 5, timeout=40000)

    ctl.set_privacy(lat_id, True)
    qtbot.waitUntil(lambda: cams[0].state.privacy is True, timeout=10000)
    assert cams[0].tpap_share_attempts == 1            # the LAT was the first and only passcode
    assert cams[1].tpap_share_attempts == 1            # password camera: LAT-free path unchanged
    assert not failures, failures
