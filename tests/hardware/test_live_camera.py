"""Tier 4 live-hardware smoke tests (Gate 3). Skipped unless TAPO_HW_IP is set.

Environment:
  TAPO_HW_IP               camera LAN IP
  TAPO_HW_CLOUD_PASSWORD   TP-Link cloud account password (HTTPS :443 control, :8800 media)
  TAPO_HW_CAM_USER         camera account user (Tapo app > Advanced settings > Camera account)
  TAPO_HW_CAM_PASSWORD     camera account password (RTSP :554, ONVIF :2020)
  TAPO_HW_ALLOW_PTZ=1      also nudge the PTZ motor and return it (optional)
Every setting the tests change is restored afterwards.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.hardware

IP = os.environ.get("TAPO_HW_IP", "")
CLOUD = os.environ.get("TAPO_HW_CLOUD_PASSWORD", "")
CAM_USER = os.environ.get("TAPO_HW_CAM_USER", "")
CAM_PASS = os.environ.get("TAPO_HW_CAM_PASSWORD", "")


def _need(*values: str) -> None:
    if not IP or not all(values):
        pytest.skip("live camera credentials not configured")


@pytest.fixture(scope="module")
def client():
    _need(CLOUD)
    from tapo_desktop.core.control_client.client import TapoControlClient
    c = TapoControlClient(IP, CLOUD, timeout=10.0)
    yield c
    c.close()


def test_login_and_device_info(client):
    info = client.get_device_info()
    assert info.model
    assert info.firmware


def test_night_vision_toggle_and_restore(client):
    from tapo_desktop.models import DayNightMode
    original = client.get_day_night()
    target = DayNightMode.ON if original != DayNightMode.ON else DayNightMode.OFF
    try:
        client.set_day_night(target)
        assert client.get_day_night() == target
    finally:
        client.set_day_night(original)
    assert client.get_day_night() == original


def test_led_toggle_and_restore(client):
    original = client.get_led()
    try:
        client.set_led(not original)
        assert client.get_led() is (not original)
    finally:
        client.set_led(original)


@pytest.mark.skipif(os.environ.get("TAPO_HW_ALLOW_PTZ") != "1", reason="PTZ movement not allowed (TAPO_HW_ALLOW_PTZ=1)")
def test_ptz_nudge_and_return(client):
    client.move(10, 0)
    time.sleep(2)
    client.move(-10, 0)


def test_rtsp_stream1_at_least_15_fps():
    _need(CAM_USER, CAM_PASS)
    from tapo_desktop.core.media_engine.profiles import build_rtsp_url
    from tapo_desktop.core.media_engine.supervisor import StreamState, StreamSupervisor
    from tapo_desktop.models import StreamQuality

    frames: list[float] = []
    playing = threading.Event()

    def on_state(state, reason):
        if state is StreamState.PLAYING:
            playing.set()

    sup = StreamSupervisor(build_rtsp_url(IP, CAM_USER, CAM_PASS, StreamQuality.HD), 1280, 720,
                           on_frame=lambda f: frames.append(f.timestamp), on_state=on_state)
    sup.start()
    try:
        assert playing.wait(15), "stream1 never reached PLAYING"
        time.sleep(10)
        window = [t for t in frames if t >= frames[-1] - 8.0]
        fps = (len(window) - 1) / (window[-1] - window[0])
        print(f"stream1 fps={fps:.1f} decoder={sup.stats().get('decoder')}")
        assert fps >= 15 * 0.9  # 10 % jitter tolerance on a 15 fps camera
    finally:
        sup.stop()


def test_onvif_device_information_and_profiles():
    _need(CAM_USER, CAM_PASS)
    from tapo_desktop.core.onvif_service.soap import OnvifClient
    oc = OnvifClient(IP, CAM_USER, CAM_PASS)
    assert oc.get_device_information().get("Manufacturer")
    assert oc.get_profiles()


def test_recording_search_last_7_days(client):
    today = datetime.now(timezone.utc).astimezone().date()
    days = client.days_with_recordings(today - timedelta(days=7), today)
    if not days:
        pytest.skip("no SD-card recordings in the last 7 days")
    clips = client.recordings_for_day(days[-1])
    assert all(c.end > c.start for c in clips)
