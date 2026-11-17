"""Tier 2: ONVIF client, PTZ and PullPoint events against the mock camera (and through ChaosProxy)."""
from __future__ import annotations

import threading
import time

import pytest

from tapo_desktop.core.errors import TapoDeviceError
from tapo_desktop.core.onvif_service.events import PullPointSubscriber
from tapo_desktop.core.onvif_service.ptz import OnvifPtz
from tapo_desktop.core.onvif_service.soap import OnvifClient
from tapo_desktop.models import MotionEvent, Preset
from tests.integration._core_it_helpers import camera_password, camera_user, wait_until

pytestmark = [pytest.mark.integration, pytest.mark.timeout(40)]


def onvif(cam, *, port=None, password=None, host=None) -> OnvifClient:
    return OnvifClient(host or cam.host, camera_user(cam), password if password is not None else camera_password(cam),
                       port=port or cam.onvif_port, timeout=5.0)


class Collector:
    def __init__(self):
        self.events: list[MotionEvent] = []
        self.lock = threading.Lock()

    def __call__(self, ev: MotionEvent) -> None:
        with self.lock:
            self.events.append(ev)

    def count(self) -> int:
        with self.lock:
            return len(self.events)

    def snapshot(self) -> list[MotionEvent]:
        with self.lock:
            return list(self.events)


def emit_until(cam, collector: Collector, active: bool, timeout: float = 3.0) -> MotionEvent | None:
    """Emit a motion event (re-emitting every 0.5 s) until a new matching one is delivered."""
    start_count = collector.count()
    end = time.monotonic() + timeout
    next_emit = 0.0
    while time.monotonic() < end:
        if time.monotonic() >= next_emit:
            cam.emit_motion_event(active)
            next_emit = time.monotonic() + 0.5
        for ev in collector.snapshot()[start_count:]:
            if ev.active is active:
                return ev
        time.sleep(0.02)
    return None


# --------------------------------------------------------------------------- device service
def test_get_device_information(mock_camera):
    info = onvif(mock_camera).get_device_information()
    for key in ("Manufacturer", "Model", "FirmwareVersion", "SerialNumber", "HardwareId"):
        assert key in info
    assert info["Manufacturer"]
    assert info["Model"]


def test_get_capabilities(mock_camera):
    caps = onvif(mock_camera).get_capabilities()
    assert caps["ptz"].startswith("http://")
    assert caps["events"].startswith("http://")


def test_get_profiles(mock_camera):
    profiles = onvif(mock_camera).get_profiles()
    assert profiles and all(p["token"] for p in profiles)
    assert all(set(p) >= {"token", "name"} for p in profiles)


def test_wrong_password_is_soap_fault(mock_camera):
    client = onvif(mock_camera, password="not-the-password")
    with pytest.raises(TapoDeviceError) as ei:
        client.get_device_information()
    assert ei.value.code == -1
    assert "not-the-password" not in str(ei.value)


# --------------------------------------------------------------------------- PTZ
def test_continuous_move_and_stop(mock_camera):
    ptz = OnvifPtz(onvif(mock_camera))
    x0 = mock_camera.state.ptz_x
    ptz.continuous_move(0.5, 0.0)
    assert wait_until(lambda: mock_camera.state.ptz_x > x0, timeout=3.0), "ContinuousMove must update ptz_x"
    ptz.stop()
    time.sleep(0.3)
    x1 = mock_camera.state.ptz_x
    time.sleep(0.5)
    assert mock_camera.state.ptz_x == x1, "position keeps changing after Stop"


def test_continuous_move_tilt_down(mock_camera):
    ptz = OnvifPtz(onvif(mock_camera))
    y0 = mock_camera.state.ptz_y
    ptz.continuous_move(0.0, -5.0)          # clamped to -1
    assert wait_until(lambda: mock_camera.state.ptz_y < y0, timeout=3.0)
    ptz.stop()


def test_onvif_presets(mock_camera):
    ptz = OnvifPtz(onvif(mock_camera))
    presets = ptz.get_presets()
    assert isinstance(presets, list) and all(isinstance(p, Preset) for p in presets)
    if presets:
        ptz.goto_preset(presets[0].id)


# --------------------------------------------------------------------------- events
def test_pullpoint_receives_motion_events(mock_camera):
    col = Collector()
    sub = PullPointSubscriber(onvif(mock_camera), col, pull_timeout_s=2, retry_backoff_s=0.2)
    sub.start()
    try:
        assert sub.running
        ev = emit_until(mock_camera, col, True, timeout=3.0)
        assert ev is not None, "motion event not delivered within 3 s"
        assert ev.camera_host == mock_camera.host
        assert ev.topic.endswith("CellMotionDetector/Motion")
        assert ev.timestamp.tzinfo is not None
        ev2 = emit_until(mock_camera, col, False, timeout=3.0)
        assert ev2 is not None, "motion-cleared event not delivered within 3 s"
    finally:
        sub.stop()
    assert not sub.running


@pytest.mark.chaos
def test_pullpoint_survives_reset_all(mock_camera, chaos_proxy):
    proxy = chaos_proxy(mock_camera.host, mock_camera.onvif_port)
    col = Collector()
    sub = PullPointSubscriber(onvif(mock_camera, host="127.0.0.1", port=proxy.port), col,
                              pull_timeout_s=2, retry_backoff_s=0.2)
    sub.start()
    try:
        assert emit_until(mock_camera, col, True, timeout=3.0) is not None
        conns_before = proxy.connection_count
        proxy.reset_all()
        assert emit_until(mock_camera, col, False, timeout=10.0) is not None, "no delivery after reset_all"
        assert emit_until(mock_camera, col, True, timeout=5.0) is not None
        assert proxy.connection_count > conns_before, "subscriber must reconnect through the client's host:port"
        assert sub.running
    finally:
        sub.stop()


@pytest.mark.chaos
def test_pullpoint_survives_blackhole(mock_camera, chaos_proxy):
    proxy = chaos_proxy(mock_camera.host, mock_camera.onvif_port)
    col = Collector()
    client = OnvifClient("127.0.0.1", camera_user(mock_camera), camera_password(mock_camera),
                         port=proxy.port, timeout=1.0)
    sub = PullPointSubscriber(client, col, pull_timeout_s=1, retry_backoff_s=0.2)
    sub.start()
    try:
        assert emit_until(mock_camera, col, True, timeout=3.0) is not None
        proxy.blackhole(2.0)
        time.sleep(2.5)
        assert emit_until(mock_camera, col, True, timeout=10.0) is not None
    finally:
        sub.stop()


def test_pullpoint_stop_unsubscribes_quickly(mock_camera):
    sub = PullPointSubscriber(onvif(mock_camera), lambda e: None, pull_timeout_s=5)
    sub.start()
    time.sleep(0.5)
    t0 = time.monotonic()
    sub.stop(timeout=3.0)
    assert time.monotonic() - t0 < 3.5
    assert not sub.running
