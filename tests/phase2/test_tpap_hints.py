"""TDP TPAP hints flow from discovery to the control client [spec:tpap.selection]."""
from __future__ import annotations

import pytest

from tapo_desktop.core.control_client.transport import PinStore
from tapo_desktop.models import CameraConfig, CloudDevice, CredentialKind, Credentials, LanDevice
from tapo_desktop.services.account import AccountManager, _control_protocol
from tapo_desktop.services.camera_service import TapoCameraService
from tests.e2e.fakes import FakeVault
from tests.fixtures.mock_tapo_server import MockTapoCamera

C200_RAW = {"encrypt_type": ["4"], "tpap": {"pake": [2], "tls": 1, "noc": 1, "port": 443}}


def _lan(raw: dict) -> LanDevice:
    return LanDevice(ip="192.168.1.20", mac="AA-BB-CC-DD-EE-FF", raw=raw)


@pytest.mark.unit
@pytest.mark.parametrize("raw,expected", [
    (C200_RAW, "tpap"), ({"tpap": {"pake": [2]}}, "tpap"), ({"encrypt_type": "4"}, "tpap"),
    ({"encrypt_type": ["3"]}, ""), ({}, ""), ({"tpap": "garbage", "encrypt_type": None}, ""),
])
def test_control_protocol_from_tdp(raw, expected):
    assert _control_protocol(_lan(raw)) == expected


@pytest.mark.unit
def test_cloud_config_records_the_hint_and_keeps_it_when_not_found():
    dev = CloudDevice(device_id="dev1", mac="AA-BB-CC-DD-EE-FF", model="C200", alias="Hall")
    cfg = AccountManager._cloud_config(None, dev, _lan(C200_RAW))
    assert cfg.control_protocol == "tpap" and cfg.host == "192.168.1.20"
    later = AccountManager._cloud_config(cfg, dev, None)            # not on the LAN this time
    assert later.control_protocol == "tpap" and later.lan_found is False


def _service(config: CameraConfig, factory=None, pin_store=None) -> TapoCameraService:
    vault = FakeVault()
    vault.set(config.id, CredentialKind.CLOUD, Credentials("admin", "cloudpass"))
    return TapoCameraService(config, vault, control_factory=factory, pin_store=pin_store)


class _Capture:
    def __init__(self):
        self.kwargs: dict = {}

    def __call__(self, **kw):
        self.kwargs = kw
        return self

    def set_led(self, on):  # the only call the test makes
        self.led = on

    def close(self):
        pass


@pytest.mark.unit
@pytest.mark.parametrize("protocol,expected", [("tpap", ["4"]), ("", None)])
def test_camera_service_passes_the_hint_only_when_set(protocol, expected):
    cap = _Capture()
    svc = _service(CameraConfig(name="Hall", host="192.168.1.20", id="dev1", control_protocol=protocol), cap)
    try:
        svc.set_led(True)
        assert cap.kwargs.get("encrypt_type") == expected
        assert ("encrypt_type" in cap.kwargs) is (expected is not None)
    finally:
        svc.close()


@pytest.mark.integration
def test_service_with_hint_skips_the_v3_probe_end_to_end():
    cam = MockTapoCamera(protocol="tpap", model="C200", tpap_credential="sha256_extra").start()
    try:
        cfg = CameraConfig(name="Hall", host=cam.host, id="dev1", https_port=cam.https_port, control_protocol="tpap")
        svc = _service(cfg, pin_store=PinStore())
        try:
            svc.set_privacy(True)
            assert cam.state.privacy is True
            assert cam.v3_probe_count == 0 and cam.tpap_share_attempts == 1
        finally:
            svc.close()
    finally:
        cam.stop()
