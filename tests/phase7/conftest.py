"""Phase 7 test configuration (camera list sorting + camera rename).

Re-uses the e2e UI fixtures (``make_env``, ``no_app_quit``), the lane P2-app ``app_env`` (AccountManager
over a FakeCloud) and the Phase 4 remote-control fixtures (``remote_camera``, ``passthrough``,
``remote_client`` on the mock cloud), imported the way ``tests/phase5/conftest.py`` does.

``FakeCameraService`` gets a ``rename(name)`` method for every Phase 7 test (recorded as a ``"rename"``
call, so ``factory.fail(id, "rename", exc)`` works); the e2e fakes stay untouched.
Nothing here talks to real cameras or to the real TP-Link cloud.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from tests.e2e.conftest import make_env, no_app_quit  # noqa: E402,F401 (pytest fixtures are used by name)
from tests.e2e.fakes import FakeCameraService  # noqa: E402
from tests.phase2._app_helpers import app_env  # noqa: E402,F401
from tests.phase4.conftest import (  # noqa: E402,F401 (pytest fixtures are used by name)
    DEFAULT_CLOUD_DEVICES,
    DEFAULT_EMAIL,
    DEFAULT_PASSWORD,
    DEVICE,
    DEVICE_ID,
    TERMINAL,
    mock_cloud,
    mock_cloud_factory,
    mock_relay,
    mock_tdp,
    mock_tdp_factory,
    passthrough,
    relay_client,
    relay_cloud,
    remote_camera,
    remote_client,
    signed_in_cloud,
)


def _fake_rename(self: FakeCameraService, name: str) -> None:
    self._enter("rename", name)


@pytest.fixture(autouse=True)
def fake_service_rename(monkeypatch):
    """Give the e2e FakeCameraService the Phase 7 ``rename`` operation."""
    monkeypatch.setattr(FakeCameraService, "rename", _fake_rename, raising=False)
