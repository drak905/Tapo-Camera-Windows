"""Tier 2 chaos: reconnect / stall recovery of StreamSupervisor through ChaosProxy and mock faults.

Contract: "first frame after network recovery within 3 s of the camera being reachable again".
Tolerance used here: 3.0 s + 0.5 s scheduling slack.
"""
from __future__ import annotations

import time

import pytest

from tapo_desktop.core.media_engine.supervisor import StreamState, StreamSupervisor
from tests.integration._media_helpers import Recorder, ffmpeg_children, wait_until, with_port

pytestmark = [pytest.mark.integration, pytest.mark.chaos, pytest.mark.timeout(60)]

RECOVERY_S = 3.0 + 0.5


@pytest.fixture
def sup_factory(ffmpeg_path):
    created: list[StreamSupervisor] = []

    def factory(url, audio=False, **kw):
        rec = Recorder()
        kw.setdefault("ffmpeg_path", ffmpeg_path)
        kw.setdefault("stall_timeout", 2.0)
        sup = StreamSupervisor(url, 640, 360, **rec.kwargs(audio=audio), **kw)
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
    assert wait_until(lambda: sup.state is StreamState.PLAYING, 15), rec.state_names()
    assert wait_until(lambda: rec.frame_count() >= frames, 10)


def _assert_recovered(sup, rec, t_fault: float, t_reachable: float):
    t_rec = rec.first_state_time(StreamState.RECONNECTING, after=t_fault)
    assert t_rec is not None, rec.state_names()
    assert wait_until(lambda: rec.first_state_time(StreamState.PLAYING, after=t_rec) is not None, 15), rec.state_names()
    new_frames = rec.frames_after(t_rec)
    assert new_frames, "no frame after reconnect"
    first = new_frames[0].timestamp
    assert first - t_reachable < RECOVERY_S, f"first frame {first - t_reachable:.2f}s after camera reachable"
    assert sup.stats()["reconnects"] >= 1
    assert len(ffmpeg_children()) == 1, "old ffmpeg not reaped after reconnect"


def test_mock_drop_connections_reconnects(sup_factory, mock_camera):
    sup, rec = sup_factory(mock_camera.rtsp_url("stream2"))
    _play(sup, rec)
    t = time.monotonic()
    mock_camera.drop_rtsp_connections()
    assert wait_until(lambda: rec.first_state_time(StreamState.RECONNECTING, after=t) is not None, 4.0)
    _assert_recovered(sup, rec, t, t)
    assert mock_camera.rtsp_active_clients == 1


def test_proxy_reset_all_reconnects(sup_factory, mock_camera, chaos_proxy):
    proxy = chaos_proxy(mock_camera.host, mock_camera.rtsp_port)
    sup, rec = sup_factory(with_port(mock_camera.rtsp_url("stream2"), proxy.port))
    _play(sup, rec)
    assert proxy.connection_count >= 1
    t = time.monotonic()
    proxy.reset_all()
    assert wait_until(lambda: rec.first_state_time(StreamState.RECONNECTING, after=t) is not None, 4.0)
    _assert_recovered(sup, rec, t, t)


def test_repeated_resets_backoff_resets_after_success(sup_factory, mock_camera, chaos_proxy):
    proxy = chaos_proxy(mock_camera.host, mock_camera.rtsp_port)
    sup, rec = sup_factory(with_port(mock_camera.rtsp_url("stream2"), proxy.port), backoff_initial=0.5,
                           backoff_max=10.0)
    _play(sup, rec)
    for _ in range(4):
        t = time.monotonic()
        proxy.reset_all()
        assert wait_until(lambda: rec.first_state_time(StreamState.RECONNECTING, after=t) is not None, 4.0)
        _assert_recovered(sup, rec, t, t)
        assert wait_until(lambda: len(rec.frames_after(time.monotonic() - 0.5)) > 3, 5)
    assert sup.stats()["reconnects"] >= 4


def test_blackhole_stall_detected_and_recovered(sup_factory, mock_camera, chaos_proxy):
    proxy = chaos_proxy(mock_camera.host, mock_camera.rtsp_port)
    sup, rec = sup_factory(with_port(mock_camera.rtsp_url("stream2"), proxy.port), stall_timeout=2.0)
    _play(sup, rec)
    t_start = time.monotonic()
    proxy.blackhole(5)
    t_end = t_start + 5.0
    # stall detection: no frames for stall_timeout -> RECONNECTING (allow 1.5 s for buffered frames/polling)
    assert wait_until(lambda: rec.first_state_time(StreamState.RECONNECTING, after=t_start) is not None, 2.0 + 1.5), \
        rec.state_names()
    assert sup.state is not StreamState.FAILED
    assert wait_until(lambda: rec.frames_after(t_end), 10), rec.state_names()
    first_after = rec.frames_after(t_end)[0].timestamp
    assert first_after - t_end < RECOVERY_S, f"recovered {first_after - t_end:.2f}s after blackhole ended"
    assert sup.state is StreamState.PLAYING
    assert wait_until(lambda: len(ffmpeg_children()) == 1, 3)


def test_refused_connections_then_recovery(sup_factory, mock_camera, chaos_proxy):
    """Wi-Fi down for 4 s: proxy refuses new connections and existing ones are reset."""
    proxy = chaos_proxy(mock_camera.host, mock_camera.rtsp_port)
    sup, rec = sup_factory(with_port(mock_camera.rtsp_url("stream2"), proxy.port))
    _play(sup, rec)
    t = time.monotonic()
    proxy.set_refuse(True)
    proxy.reset_all()
    time.sleep(4.0)
    assert sup.state in (StreamState.RECONNECTING, StreamState.CONNECTING)
    t_up = time.monotonic()
    proxy.set_refuse(False)
    assert wait_until(lambda: rec.frames_after(t_up), 10), rec.state_names()
    assert rec.frames_after(t_up)[0].timestamp - t_up < RECOVERY_S
    assert rec.first_state_time(StreamState.RECONNECTING, after=t) is not None


def test_corrupt_audio_keeps_video_and_audio_flowing(sup_factory, mock_camera):
    sup, rec = sup_factory(mock_camera.rtsp_url("stream2"), audio=True)
    _play(sup, rec)
    assert wait_until(lambda: rec.audio_bytes_after(0) > 4000, 5), "no audio before corruption"
    t = time.monotonic()
    mock_camera.set_rtsp_corrupt_audio(True)
    time.sleep(4.0)
    states_after = [s for ts, s, _ in rec.states if ts > t]
    assert states_after == [], f"state changed during corrupt audio: {states_after}"
    assert sup.state is StreamState.PLAYING
    assert len(rec.frames_after(t)) >= 30  # ~15 fps * 4 s = 60 nominal
    # garbled packets carry only 1..37 random bytes, so the audio rate drops but must not stop
    assert rec.audio_bytes_after(t + 1.0) > 0, "audio pipeline died after corruption"
    mock_camera.set_rtsp_corrupt_audio(False)
    t_clean = time.monotonic()
    time.sleep(3.0)
    # back to clean PCMA: 8000 Hz * 2 bytes = 16000 B/s nominal; measure the last 2 s
    assert rec.audio_bytes_after(t_clean + 1.0) >= 16000, "audio did not recover after corruption ended"
    assert sup.state is StreamState.PLAYING
    assert [s for ts, s, _ in rec.states if ts > t] == []
    assert not rec.callback_errors
