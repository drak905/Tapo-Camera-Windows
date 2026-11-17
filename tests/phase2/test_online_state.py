"""Online state like the phone app: the v2 device list's ``status`` reads 0 even for streaming
cameras (seen on a real account), so the IoT thing list ``status`` (1 = online) and LAN presence win."""
from __future__ import annotations

import pytest

from tapo_desktop.core.cloud.client import TapoCloudClient
from tapo_desktop.models import CloudDevice, LanDevice
from tapo_desktop.services.account import AccountManager
from tests.fixtures.mock_cloud import make_cloud_device
from tests.fixtures.phase2_fixtures import DEFAULT_EMAIL, DEFAULT_PASSWORD

pytestmark = pytest.mark.integration


def test_thing_list_status_is_exposed_as_online(mock_cloud_factory):
    devices = [make_cloud_device(1, status=0), make_cloud_device(2, status=0)]
    cloud = mock_cloud_factory(devices=devices)
    real = cloud._thing

    def thing(d):
        item = real(d)
        item["status"] = 1 if d is devices[0] else 0
        return item

    cloud._thing = thing
    c = TapoCloudClient(terminal_uuid="A" * 32, base_url=cloud.base_url)
    c.login(DEFAULT_EMAIL, DEFAULT_PASSWORD)
    c.local_access_tokens()
    assert c.thing_online == {devices[0]["deviceId"]: True, devices[1]["deviceId"]: False}


class _Ctrl:
    """Minimal controller stand-in for AccountManager._apply_cameras."""

    def __init__(self):
        self.cams = []

    def cameras(self):
        return list(self.cams)

    def camera(self, cid):
        return next((c for c in self.cams if c.id == cid), None)

    def remove_cameras(self, ids):
        self.cams = [c for c in self.cams if c.id not in ids]

    def upsert_cameras(self, configs, cloud=None, local_tokens=None):
        self.cams = list(configs)


def test_cameras_on_lan_or_online_in_thing_list_are_not_shown_offline(qtbot):
    import hashlib

    am = AccountManager.__new__(AccountManager)  # exercise the pure merge logic only
    am._gen = 0
    am._ctrl = _Ctrl()
    am._thing_online = {"dev-b": True}
    devs = [CloudDevice(device_id=i, mac=f"AA-BB-CC-DD-EE-0{n}", model="C200", alias=i, online=False)
            for n, i in enumerate(("dev-a", "dev-b", "dev-c"))]
    lan = [LanDevice(ip="192.168.1.5", mac="AA-BB-CC-DD-EE-00",
                     device_id_md5=hashlib.md5(b"dev-a").hexdigest())]
    am._apply_cameras(0, devs, lan, "pw", notify=False)
    state = {c.id: (c.online, c.lan_found) for c in am._ctrl.cams}
    assert state == {"dev-a": (True, True), "dev-b": (True, False), "dev-c": (False, False)}
