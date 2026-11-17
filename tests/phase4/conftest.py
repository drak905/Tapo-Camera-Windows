"""Phase 4 test configuration (remote control, remote recordings and shared cameras through the cloud).

Re-uses the Phase 2 / Phase 3 fixtures the way ``tests/phase3/conftest.py`` does (``pytest_plugins`` is
not allowed in a non-root conftest) and adds:

* ``remote_camera``: a started :class:`MockTapoCamera` registered in ``relay_cloud`` for the default
  "Front Door" device, so ``services-sync`` passthrough runs on its real shadow state;
* ``passthrough``: a :class:`CloudPassthrough` over ``signed_in_cloud`` (thing list already loaded);
* ``remote_client``: a :class:`RemoteControlClient` for the "Front Door" thing.

Nothing here talks to the real TP-Link cloud or to real cameras.
"""
from __future__ import annotations

import pytest

from tests.fixtures.mock_tapo_server import MockTapoCamera
from tests.phase3.conftest import (  # noqa: F401 (pytest fixtures are used by name)
    DEFAULT_CLOUD_DEVICES,
    DEFAULT_EMAIL,
    DEFAULT_PASSWORD,
    DEVICE,
    TERMINAL,
    mock_cloud,
    mock_cloud_factory,
    mock_relay,
    mock_tdp,
    mock_tdp_factory,
    relay_client,
    relay_cloud,
    signed_in_cloud,
)

DEVICE_ID = DEVICE["deviceId"]
OFFLINE_ID = DEFAULT_CLOUD_DEVICES[1]["deviceId"]  # "Garden", status 0


@pytest.fixture
def remote_camera(relay_cloud):
    cam = MockTapoCamera(model="C210").start()
    relay_cloud.register_camera(DEVICE_ID, cam)
    try:
        yield cam
    finally:
        cam.stop()


@pytest.fixture
def passthrough(signed_in_cloud):
    from tapo_desktop.core.cloud.passthrough import CloudPassthrough

    signed_in_cloud.local_access_tokens()  # loads the IoT thing list (thing URLs, roles)
    return CloudPassthrough(signed_in_cloud, timeout=10)


@pytest.fixture
def remote_client(passthrough, signed_in_cloud):
    from tapo_desktop.core.cloud.passthrough import RemoteControlClient

    thing = signed_in_cloud.things[DEVICE_ID]
    return RemoteControlClient(passthrough, thing.thing_name, thing.app_server_url, player_id=TERMINAL.lower())
