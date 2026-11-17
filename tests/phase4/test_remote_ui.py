"""UI end to end for a remote camera (not on the LAN) through the real controller / account wiring:
the controls panel shows and changes the camera's real state through the cloud passthrough, the
playback panel lists the recordings and downloads one through the relay."""
from __future__ import annotations

import datetime as dt
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from tapo_desktop.models import RecordingClip  # noqa: E402
from tests.e2e.fakes import FakeVault  # noqa: E402
from tests.phase4.conftest import DEFAULT_EMAIL, DEFAULT_PASSWORD, DEVICE_ID  # noqa: E402

pytestmark = [pytest.mark.integration, pytest.mark.ui, pytest.mark.timeout(120)]


@pytest.fixture
def signed_in_app(tmp_path, qtbot, relay_cloud, mock_relay, remote_camera, ffmpeg_path, monkeypatch):
    from tapo_desktop.core.cloud.client import TapoCloudClient
    from tapo_desktop.core.cloud.relay import RelayClient
    from tapo_desktop.services.account import AccountManager
    from tapo_desktop.services.config_store import ConfigStore
    from tapo_desktop.services.controller import AppController

    monkeypatch.setattr("tapo_desktop.core.vod.downloader.find_ffmpeg", lambda *a, **k: ffmpeg_path)
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
    yield controller
    controller.stop_all()
    accounts.wait_idle(3000)


def test_controls_panel_works_for_a_remote_camera(signed_in_app, qtbot, remote_camera):
    from PyQt6.QtWidgets import QComboBox, QWidget

    from tapo_desktop.ui.controls_panel import ControlsPanel

    controller = signed_in_app
    remote_camera.state.privacy = True
    remote_camera.state.led = False
    failures = []
    controller.command_failed.connect(lambda cid, action, msg: failures.append((action, msg)))
    panel = ControlsPanel(controller)
    qtbot.addWidget(panel)
    panel.set_camera(DEVICE_ID)
    privacy = panel.findChild(QWidget, "privacyToggle")
    led = panel.findChild(QWidget, "ledToggle")
    presets = panel.findChild(QComboBox, "presetCombo")
    qtbot.waitUntil(lambda: privacy.isChecked() and presets.count() == 2, timeout=10000)
    assert privacy.isEnabled() and led.isEnabled() and not led.isChecked()
    qtbot.mouseClick(privacy, __import__("PyQt6.QtCore", fromlist=["Qt"]).Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: remote_camera.state.privacy is False, timeout=10000)
    assert failures == []


def test_playback_panel_lists_and_downloads_for_a_remote_camera(signed_in_app, qtbot, remote_camera, tmp_path):
    from PyQt6.QtWidgets import QListWidget, QPushButton

    from tapo_desktop.ui.playback_panel import PlaybackPanel

    controller = signed_in_app
    midnight = dt.datetime.now().astimezone().replace(hour=0, minute=1, second=0, microsecond=0)
    start = midnight.astimezone(dt.timezone.utc)
    remote_camera.state.recordings = [RecordingClip(start=start, end=start + dt.timedelta(seconds=6), event_type=2)]
    panel = PlaybackPanel(controller)
    qtbot.addWidget(panel)
    out = tmp_path / "remote_clip.mp4"
    panel.choose_save_path = lambda clip: out  # type: ignore[method-assign]
    panel.set_camera(DEVICE_ID)
    lst = panel.findChild(QListWidget, "recordingsList")
    qtbot.waitUntil(lambda: lst.count() == 1, timeout=10000)
    lst.setCurrentRow(0)
    button = panel.findChild(QPushButton, "downloadButton")
    assert button.isEnabled()
    finished = []
    controller.download_finished.connect(lambda cid, path, error: finished.append((cid, path, error)))
    button.click()
    qtbot.waitUntil(lambda: bool(finished), timeout=60000)
    assert finished[-1] == (DEVICE_ID, str(out), "")
    assert out.stat().st_size > 1000
