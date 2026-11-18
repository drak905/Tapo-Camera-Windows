"""Real wiring: an owned camera that is not on the LAN is renamed through AppController -> TapoCameraService
-> cloud passthrough -> mock camera, and the pending name survives the next cloud refresh."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from tests.e2e.fakes import FakeVault  # noqa: E402
from tests.phase4.conftest import DEFAULT_EMAIL, DEFAULT_PASSWORD, DEVICE_ID  # noqa: E402

pytestmark = [pytest.mark.integration, pytest.mark.ui, pytest.mark.timeout(120)]


@pytest.fixture
def signed_in_app(tmp_path, qtbot, relay_cloud, mock_relay, remote_camera):
    from tapo_desktop.core.cloud.client import TapoCloudClient
    from tapo_desktop.core.cloud.relay import RelayClient
    from tapo_desktop.services.account import AccountManager
    from tapo_desktop.services.config_store import ConfigStore
    from tapo_desktop.services.controller import AppController

    controller = AppController(ConfigStore(tmp_path / "config.json"), FakeVault())
    ctx = mock_relay.client_context()
    accounts = AccountManager(
        controller, discover=lambda: [],
        cloud_factory=lambda t: TapoCloudClient(terminal_uuid=t, base_url=relay_cloud.base_url, timeout=5),
        relay_factory=lambda c: RelayClient(c, terminal_uuid=c.terminal_uuid, timeout=5, ssl_context=ctx))
    with qtbot.waitSignal(accounts.signed_in, timeout=5000):
        accounts.sign_in(DEFAULT_EMAIL, DEFAULT_PASSWORD)
    assert accounts.wait_idle(5000)
    assert controller.remote_available(DEVICE_ID)
    yield controller, accounts
    controller.stop_all()
    accounts.wait_idle(3000)


def test_owned_remote_camera_is_renamed_through_the_cloud(signed_in_app, qtbot, remote_camera, relay_cloud):
    controller, accounts = signed_in_app
    old = controller.camera(DEVICE_ID).name
    with qtbot.waitSignal(controller.rename_finished, timeout=15000) as blocker:
        assert controller.rename_camera(DEVICE_ID, "Hiên nhà") is True
    assert blocker.args == [DEVICE_ID, ""]
    assert remote_camera.state.alias == "Hiên nhà"
    sent = [r for e in relay_cloud.passthrough_log for r in e["requests"] if r.get("method") == "setDeviceAlias"]
    assert sent == [{"method": "setDeviceAlias", "params": {"system": {"sys": {"dev_alias": "Hiên nhà"}}}}]
    cfg = controller.camera(DEVICE_ID)
    assert cfg.name == "Hiên nhà" and cfg.pending_name == "Hiên nhà" and cfg.cloud_alias == old
    with qtbot.waitSignal(accounts.cameras_refreshed, timeout=10000):
        accounts.refresh_cameras()  # the mock cloud still lists the old alias: the pending name stays
    assert accounts.wait_idle(5000)
    assert controller.camera(DEVICE_ID).name == "Hiên nhà"
