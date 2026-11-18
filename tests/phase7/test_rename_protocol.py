"""setDeviceAlias end to end against the mock camera: the dispatcher (additive fixture change), the LAN
TapoControlClient, the cloud passthrough RemoteControlClient and TapoCameraService routing."""
from __future__ import annotations

import pytest

from tapo_desktop.core.errors import TapoDeviceError
from tapo_desktop.models import CameraConfig, CredentialKind
from tests.e2e.fakes import CLOUD, FakeVault
from tests.fixtures.mock_tapo_server.dispatcher import HANDLERS, dispatch
from tests.fixtures.mock_tapo_server.shadow import DeviceShadow

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]

ALIAS_PARAMS = {"system": {"sys": {"dev_alias": "Cổng trước"}}}


# ------------------------------------------------------------------ mock dispatcher
def test_dispatcher_handles_set_device_alias_and_get_device_info_returns_it():
    shadow = DeviceShadow()
    assert "setDeviceAlias" in HANDLERS
    assert dispatch(shadow, "setDeviceAlias", ALIAS_PARAMS) == (0, {})
    code, result = dispatch(shadow, "getDeviceInfo", {"device_info": {"name": ["basic_info"]}})
    assert code == 0 and result["device_info"]["basic_info"]["device_alias"] == "Cổng trước"


@pytest.mark.parametrize("params, code", [
    ({}, -40211),
    ({"system": {"sys": {}}}, -40211),
    ({"system": {"sys": {"dev_alias": 5}}}, -40210),
    ({"system": {"sys": {"dev_alias": ""}}}, -40210),
    ({"system": {"sys": {"dev_alias": "ổ" * 11}}}, -40210),  # 33 bytes
])
def test_dispatcher_refuses_bad_aliases(params, code):
    shadow = DeviceShadow()
    before = shadow.alias
    assert dispatch(shadow, "setDeviceAlias", params)[0] == code
    assert shadow.alias == before


# ------------------------------------------------------------------ LAN client
def test_lan_client_renames_the_mock_camera(mock_camera):
    from tapo_desktop.core.control_client.client import TapoControlClient
    from tapo_desktop.core.control_client.transport import PinStore

    client = TapoControlClient(mock_camera.host, mock_camera.cloud_password, port=mock_camera.https_port,
                               pin_store=PinStore(), timeout=5)
    try:
        client.rename("Garage 2")
        assert mock_camera.state.alias == "Garage 2"
        assert client.get_device_info().alias == "Garage 2"
    finally:
        client.close()


def test_lan_client_reports_a_refused_alias(mock_camera):
    from tapo_desktop.core.control_client.client import TapoControlClient
    from tapo_desktop.core.control_client.transport import PinStore

    client = TapoControlClient(mock_camera.host, mock_camera.cloud_password, port=mock_camera.https_port,
                               pin_store=PinStore(), timeout=5)
    try:
        with pytest.raises(TapoDeviceError) as err:
            client.rename("x" * 40)
        assert err.value.code == -40210
    finally:
        client.close()


# ------------------------------------------------------------------ cloud passthrough
def test_remote_client_renames_through_the_passthrough(remote_client, remote_camera, relay_cloud):
    remote_client.rename("Hiên nhà")
    assert remote_camera.state.alias == "Hiên nhà"
    assert remote_client.get_device_info().alias == "Hiên nhà"
    sent = [r for entry in relay_cloud.passthrough_log for r in entry["requests"] if r.get("method") == "setDeviceAlias"]
    assert sent == [{"method": "setDeviceAlias", "params": {"system": {"sys": {"dev_alias": "Hiên nhà"}}}}]


# ------------------------------------------------------------------ TapoCameraService routing
class _Client:
    def __init__(self, error: Exception | None = None):
        self.renamed: list[str] = []
        self.error = error
        self.logged_in = True

    def rename(self, name: str) -> None:
        if self.error is not None:
            raise self.error
        self.renamed.append(name)

    def close(self) -> None:
        pass


def _service(cfg: CameraConfig, client: _Client, provider=None):
    from tapo_desktop.services.camera_service import TapoCameraService

    vault = FakeVault()
    vault.set(cfg.id, CredentialKind.CLOUD, CLOUD)
    return TapoCameraService(cfg, vault, control_factory=lambda **kw: client,
                             remote=(lambda: provider) if provider is not None else None)


def test_service_renames_an_owned_lan_camera_through_the_lan_client():
    client = _Client()
    svc = _service(CameraConfig(name="Old", host="192.0.2.5", id="c1", source="cloud", role=0), client)
    svc.rename("New")
    assert client.renamed == ["New"]


def test_service_renames_an_owned_remote_camera_through_the_cloud():
    lan, remote = _Client(), _Client()

    class Provider:
        available = True

        def control_client(self, cfg):
            return remote

    cfg = CameraConfig(name="Old", host="", id="c2", source="cloud", role=0, lan_found=False, online=True)
    _service(cfg, lan, Provider()).rename("New")
    assert remote.renamed == ["New"] and lan.renamed == []


def test_service_explains_a_refused_name():
    client = _Client(TapoDeviceError(-63103, "setDeviceAlias"))
    svc = _service(CameraConfig(name="Old", host="192.0.2.5", id="c1", source="cloud"), client)
    with pytest.raises(TapoDeviceError) as err:
        svc.rename("New")
    assert err.value.code == -63103
    assert "name" in str(err.value).lower()
