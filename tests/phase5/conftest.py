"""Phase 5 test configuration (SD-card playback streaming).

Re-uses the Phase 3 / Phase 4 relay fixtures the way ``tests/phase4/conftest.py`` does and adds
``playback_camera``: a started secure :class:`MockTapoCamera` in continuous-playback mode
(``media_playback_mode = "continuous"``, unpaced like a real camera) with two recordings
:data:`RECS` (20 s, gap 40 s, 20 s) starting 2 h ago. Nothing here talks to real cameras or the cloud.
"""
from __future__ import annotations

import datetime as dt
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from tapo_desktop.models import RecordingClip  # noqa: E402
from tests.fixtures.mock_tapo_server import MockTapoCamera  # noqa: E402
from tests.phase3.conftest import (  # noqa: E402,F401 (pytest fixtures are used by name)
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
BASE = int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).timestamp()) // 60 * 60
RECS: list[tuple[int, int]] = [(BASE, BASE + 20), (BASE + 60, BASE + 80)]


def clips_of(recs) -> list[RecordingClip]:
    return [RecordingClip(start=dt.datetime.fromtimestamp(s, dt.timezone.utc),
                          end=dt.datetime.fromtimestamp(e, dt.timezone.utc), event_type=2) for s, e in recs]


@pytest.fixture
def playback_camera():
    cam = MockTapoCamera(secure=True)
    cam.media_playback_mode = "continuous"
    cam.media_playback_speed = None  # unpaced: faster than real time while acked, like real cameras
    cam.state.recordings = clips_of(RECS)
    cam.start()
    try:
        yield cam
    finally:
        cam.stop()
