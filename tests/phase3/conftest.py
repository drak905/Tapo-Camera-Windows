"""Phase 3 test configuration (remote live view through the TP-Link cloud relay).

Imports the Phase 2 fixtures the same way ``tests/phase2/conftest.py`` does (``pytest_plugins`` is
not allowed in a non-root conftest) and adds:

* ``mock_relay``: started :class:`tests.fixtures.mock_relay.MockRelay` (TLS relay node);
* ``relay_cloud``: started mock cloud (default account + devices) whose ``/v2/relay/request`` hands
  out slots of ``mock_relay``;
* ``signed_in_cloud``: a real :class:`TapoCloudClient` signed in to ``relay_cloud``;
* ``relay_client``: a :class:`RelayClient` over ``signed_in_cloud`` whose TLS context trusts only the
  mock relay certificate.

Nothing here talks to the real TP-Link cloud or to real cameras.
"""
from __future__ import annotations

import pytest

from tests.fixtures.mock_relay import MockRelay
from tests.fixtures.phase2_fixtures import (  # noqa: F401 (pytest fixtures are used by name)
    DEFAULT_CLOUD_DEVICES,
    DEFAULT_EMAIL,
    DEFAULT_PASSWORD,
    mock_cloud,
    mock_cloud_factory,
    mock_tdp,
    mock_tdp_factory,
)

TERMINAL = "0123456789ABCDEF0123456789ABCDEF"
DEVICE = DEFAULT_CLOUD_DEVICES[0]  # "Front Door", C210, online


@pytest.fixture
def mock_relay():
    relay = MockRelay().start()
    try:
        yield relay
    finally:
        relay.stop()


@pytest.fixture
def relay_cloud(mock_cloud_factory, mock_relay):
    cloud = mock_cloud_factory()
    cloud.relay = mock_relay
    return cloud


@pytest.fixture
def signed_in_cloud(relay_cloud):
    from tapo_desktop.core.cloud.client import TapoCloudClient

    client = TapoCloudClient(terminal_uuid=TERMINAL, base_url=relay_cloud.base_url, timeout=5)
    client.login(DEFAULT_EMAIL, DEFAULT_PASSWORD)
    return client


@pytest.fixture
def relay_client(signed_in_cloud, mock_relay):
    from tapo_desktop.core.cloud.relay import RelayClient

    return RelayClient(signed_in_cloud, terminal_uuid=TERMINAL, timeout=5, ssl_context=mock_relay.client_context())
