"""Tier 2 chaos: LocalStreamSupervisor reconnect / stall recovery over :8800.

Contract: "reconnect with backoff on any failure/stall; first frame after recovery < 3 s".
The ChaosProxy sits in front of the mock :8800 port (its port is passed as ``port``).
Tolerance: 3.0 s + 0.5 s scheduling slack, as in the Phase 1 media chaos tests.
"""
from __future__ import annotations

import time

import pytest

from tapo_desktop.core.media_engine.local_stream import LocalStreamSupervisor
from tapo_desktop.core.media_engine.supervisor import StreamState
from tests.integration._media_helpers import Recorder, ffmpeg_children, wait_until

pytestmark = [pytest.mark.integration, pytest.mark.chaos, pytest.mark.timeout(90)]

RECOVERY_S = 3.0 + 0.5


@pytest.fixture
def make_local(ffmpeg_path):
    created: list[LocalStreamSupervisor] = []

    def factory(cam, port=None, **kw):
        rec = Recorder()
        kw.setdefault("ffmpeg_path", ffmpeg_path)
        kw.setdefault("stall_timeout", 2.0)
        sup = LocalStreamSupervisor(cam.host, cam.cloud_password, 640, 360, resolution="VGA",
                                    port=cam.media_port if port is None else port, **rec.kwargs(), **kw)
        created.append(sup)
        return sup, rec

    yield factory
    for s in created:
        try:
            s.stop()
        except Exception:
            pass


def _play(sup, rec, frames=10):
    sup.start()
    assert wait_until(lambda: sup.state is StreamState.PLAYING, 15), (rec.state_names(), rec.reasons())
    assert wait_until(lambda: rec.frame_count() >= frames, 10)


def _assert_recovered(sup, rec, t_fault: float, t_reachable: float):
    t_rec = rec.first_state_time(StreamState.RECONNECTING, after=t_fault)
    assert t_rec is not None, rec.state_names()
    assert wait_until(lambda: rec.first_state_time(StreamState.PLAYING, after=t_rec) is not None, 15), \
        (rec.state_names(), rec.reasons())
    new_frames = rec.frames_after(t_rec)
    assert new_frames, "no frame after reconnect"
    first = max(new_frames[0].timestamp, rec.first_state_time(StreamState.PLAYING, after=t_rec))
    assert first - t_reachable < RECOVERY_S, f"first frame {first - t_reachable:.2f}s after camera reachable"
    assert sup.stats()["reconnects"] >= 1
    assert wait_until(lambda: len(ffmpeg_children()) == 1, 3), "old ffmpeg not reaped after reconnect"
    assert wait_until(lambda: len(rec.frames_after(time.monotonic() - 1.0)) >= 8, 5), "frames not flowing again"


def test_mock_drop_media_connections_reconnects(make_local, mock_camera):
    sup, rec = make_local(mock_camera)
    _play(sup, rec)
    t = time.monotonic()
    mock_camera.drop_media_connections()
    assert wait_until(lambda: rec.first_state_time(StreamState.RECONNECTING, after=t) is not None, 3.0), \
        rec.state_names()
    _assert_recovered(sup, rec, t, t)
    assert wait_until(lambda: mock_camera.media_active_sessions == 1, 3)
    assert mock_camera.media_preview_requests == 2


def test_proxy_reset_all_reconnects(make_local, mock_camera, chaos_proxy):
    proxy = chaos_proxy(mock_camera.host, mock_camera.media_port)
    sup, rec = make_local(mock_camera, port=proxy.port)
    _play(sup, rec)
    assert proxy.connection_count >= 1
    t = time.monotonic()
    proxy.reset_all()
    assert wait_until(lambda: rec.first_state_time(StreamState.RECONNECTING, after=t) is not None, 3.0), \
        rec.state_names()
    _assert_recovered(sup, rec, t, t)


def test_repeated_resets_keep_recovering(make_local, mock_camera, chaos_proxy):
    proxy = chaos_proxy(mock_camera.host, mock_camera.media_port)
    sup, rec = make_local(mock_camera, port=proxy.port)
    _play(sup, rec)
    for _ in range(3):
        t = time.monotonic()
        proxy.reset_all()
        assert wait_until(lambda: rec.first_state_time(StreamState.RECONNECTING, after=t) is not None, 3.0)
        _assert_recovered(sup, rec, t, t)
    assert sup.stats()["reconnects"] >= 3
    assert sup.state is StreamState.PLAYING


def test_blackhole_stall_detected_and_recovered(make_local, mock_camera, chaos_proxy):
    proxy = chaos_proxy(mock_camera.host, mock_camera.media_port)
    sup, rec = make_local(mock_camera, port=proxy.port, stall_timeout=2.0)
    _play(sup, rec)
    t_start = time.monotonic()
    proxy.blackhole(5)
    t_end = t_start + 5.0
    # stall detection: no frame for stall_timeout -> RECONNECTING (1.5 s slack for buffered frames / polling)
    assert wait_until(lambda: rec.first_state_time(StreamState.RECONNECTING, after=t_start) is not None, 2.0 + 1.5), \
        rec.state_names()
    assert any("stall" in r.lower() for r in rec.reasons()), rec.reasons()
    assert sup.state is not StreamState.FAILED
    assert wait_until(lambda: rec.frames_after(t_end), 10), (rec.state_names(), rec.reasons())
    first_after = rec.frames_after(t_end)[0].timestamp
    assert first_after - t_end < RECOVERY_S, f"recovered {first_after - t_end:.2f}s after blackhole ended"
    assert wait_until(lambda: sup.state is StreamState.PLAYING, 3)
    assert wait_until(lambda: len(ffmpeg_children()) == 1, 3)


def test_refused_then_recovery(make_local, mock_camera, chaos_proxy):
    """Wi-Fi down for 4 s: the proxy refuses new connections and resets the open one."""
    proxy = chaos_proxy(mock_camera.host, mock_camera.media_port)
    sup, rec = make_local(mock_camera, port=proxy.port)
    _play(sup, rec)
    t = time.monotonic()
    proxy.set_refuse(True)
    proxy.reset_all()
    time.sleep(4.0)
    assert sup.state in (StreamState.RECONNECTING, StreamState.CONNECTING), rec.state_names()
    assert ffmpeg_children() == [], "ffmpeg must not run while the camera is unreachable"
    t_up = time.monotonic()
    proxy.set_refuse(False)
    assert wait_until(lambda: rec.frames_after(t_up), 10), (rec.state_names(), rec.reasons())
    assert rec.frames_after(t_up)[0].timestamp - t_up < RECOVERY_S
    assert rec.first_state_time(StreamState.RECONNECTING, after=t) is not None
