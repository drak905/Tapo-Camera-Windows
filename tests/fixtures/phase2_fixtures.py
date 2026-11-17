"""Phase 2 pytest fixtures (registered by ``tests/phase2/conftest.py`` via ``pytest_plugins``).

* ``mock_cloud``: started :class:`MockTapoCloud` with account ``DEFAULT_EMAIL`` / ``DEFAULT_PASSWORD``
  and ``DEFAULT_CLOUD_DEVICES`` (two cameras + one smart plug that clients must filter out).
  Attributes (``mfa``, ``region_redirect``, ``devices``, ``accounts`` ...) may be changed per test.
* ``mock_tdp``: started :class:`MockTdpResponder` on ``127.0.0.1:<port>`` answering for the two
  cameras of ``DEFAULT_CLOUD_DEVICES`` (ip ``127.0.0.1``, encrypted replies). Use
  ``discover(targets=[("127.0.0.1", mock_tdp.port)])``.
* ``mock_cloud_factory`` / ``mock_tdp_factory``: build custom started instances (stopped at teardown).
"""
from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest

from tests.fixtures.mock_cloud import MockTapoCloud, make_cloud_device
from tests.fixtures.mock_tdp import MockTdpResponder

DEFAULT_EMAIL = "user@example.com"
DEFAULT_PASSWORD = "s3cret-Pass!"
DEFAULT_CLOUD_DEVICES: list[dict] = [
    make_cloud_device(1, alias="Front Door", deviceModel="C210"),
    make_cloud_device(2, alias="Garden", deviceModel="C320WS", status=0),
    make_cloud_device(3, alias="Desk Lamp Plug", deviceModel="P110", deviceType="SMART.TAPOPLUG"),
]


def tdp_device_for(cloud_device: dict, ip: str = "127.0.0.1", **overrides) -> dict:
    """MockTdpResponder device dict for a DeviceInfoResult dict (same deviceId / MAC)."""
    mac = cloud_device["deviceMac"]
    dev = {"ip": ip, "mac": ":".join(mac[i:i + 2] for i in range(0, 12, 2)).lower(),
           "device_id": cloud_device["deviceId"], "device_model": cloud_device["deviceModel"],
           "device_type": cloud_device["deviceType"], "http_port": 443}
    dev.update(overrides)
    return dev


DEFAULT_TDP_DEVICES: list[dict] = [tdp_device_for(d) for d in DEFAULT_CLOUD_DEVICES[:2]]


@pytest.fixture
def mock_cloud_factory() -> Iterator[Callable[..., MockTapoCloud]]:
    """``mock_cloud_factory(accounts=..., devices=..., mfa=..., region_redirect=...)`` -> started mock."""
    started: list[MockTapoCloud] = []

    def make(accounts: dict[str, str] | None = None, devices: list[dict] | None = None,
             mfa: bool = False, region_redirect: bool = False, **kwargs) -> MockTapoCloud:
        cloud = MockTapoCloud(accounts={DEFAULT_EMAIL: DEFAULT_PASSWORD} if accounts is None else accounts,
                              devices=list(DEFAULT_CLOUD_DEVICES) if devices is None else devices,
                              mfa=mfa, region_redirect=region_redirect, **kwargs)
        cloud.start()
        started.append(cloud)
        return cloud

    try:
        yield make
    finally:
        for c in started:
            c.stop()


@pytest.fixture
def mock_cloud(mock_cloud_factory) -> MockTapoCloud:
    """Started default mock cloud (see module docstring)."""
    return mock_cloud_factory()


@pytest.fixture
def mock_tdp_factory() -> Iterator[Callable[..., MockTdpResponder]]:
    """``mock_tdp_factory(devices=[...], concat=False, garbage=False)`` -> started responder."""
    started: list[MockTdpResponder] = []

    def make(devices: list[dict] | None = None, **kwargs) -> MockTdpResponder:
        responder = MockTdpResponder(devices=list(DEFAULT_TDP_DEVICES) if devices is None else devices, **kwargs)
        responder.start()
        started.append(responder)
        return responder

    try:
        yield make
    finally:
        for r in started:
            r.stop()


@pytest.fixture
def mock_tdp(mock_tdp_factory) -> MockTdpResponder:
    """Started default TDP responder (see module docstring)."""
    return mock_tdp_factory()
