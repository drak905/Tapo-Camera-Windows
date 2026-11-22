"""Phase 9 (iMou Open Platform, route A) fixtures: a mock iMou cloud, a loopback-only network guard, a clock."""
from __future__ import annotations

import os
import socket

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from tests.fixtures.mock_imou_cloud import MOCK_APP_ID, MOCK_APP_SECRET, MockImouCloud  # noqa: E402

LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture(autouse=True)
def _loopback_only(monkeypatch):
    """No test in this folder may open a socket to anything but loopback (the iMou cloud is never contacted)."""
    real = socket.create_connection

    def guard(address, *args, **kwargs):
        host = address[0]
        if host not in LOOPBACK:
            raise AssertionError(f"test tried to contact a non-loopback host: {host!r}")
        return real(address, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", guard)


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("TAPO_DESKTOP_HOME", str(tmp_path / "home"))


@pytest.fixture(autouse=True)
def _reset_runtime():
    """The iMou runtime is process-wide state: every test starts and ends without one."""
    try:
        from tapo_desktop.services import imou_camera_service
    except ImportError:  # the service module is built in a later part
        yield
        return
    imou_camera_service.set_runtime(None)
    yield
    imou_camera_service.set_runtime(None)


class Clock:
    """Settable epoch clock."""

    def __init__(self, now: float = 1_700_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def cloud(clock):
    return MockImouCloud(clock=clock)


@pytest.fixture
def app_keys():
    return MOCK_APP_ID, MOCK_APP_SECRET
