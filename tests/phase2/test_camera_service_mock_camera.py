"""TapoCameraService against the mock camera with ONLY the TP-Link ID password (cloud camera):
control works and motion arrives through getLastAlarmInfo polling."""
from __future__ import annotations

import threading

import pytest

from tapo_desktop.models import CameraConfig, CredentialKind, Credentials
from tapo_desktop.services.camera_service import TapoCameraService
from tests.e2e.fakes import FakeVault

pytestmark = pytest.mark.integration


def test_cloud_camera_control_and_alarm_polling(mock_camera):
    config = CameraConfig(name="Mock", host=mock_camera.host, id="dev-mock", source="cloud",
                          https_port=mock_camera.https_port, media_port=mock_camera.media_port)
    vault = FakeVault()
    vault.set(config.id, CredentialKind.CLOUD, Credentials("admin", mock_camera.cloud_password))
    svc = TapoCameraService(config, vault, alarm_poll_interval=0.1)
    events = []
    got = threading.Event()
    try:
        info = svc.connect()
        assert info.model
        assert svc.live_transport() == "local"
        svc.start_events(lambda ev: (events.append(ev), got.set()))
        threading.Event().wait(0.3)  # let the poller take its baseline reading
        mock_camera.emit_motion_event(True)
        assert got.wait(5), "motion from getLastAlarmInfo polling did not arrive"
        assert events[0].active and events[0].source == "last_alarm_info"
        assert events[0].camera_host == mock_camera.host
    finally:
        svc.close()
