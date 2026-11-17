"""Tier 2: StreamSupervisor + real ffmpeg against the mock camera's RTSP server.

Fixtures from tests/conftest.py (lane-fixture): mock_camera, dual_lens_camera, ffmpeg_path.
"""
from __future__ import annotations

import ctypes
import logging
import time
from ctypes import wintypes

import psutil
import pytest

from tapo_desktop.core.errors import StreamLimitError  # noqa: F401  (documented failure reason)
from tapo_desktop.core.media_engine.job_object import JobObject
from tapo_desktop.core.media_engine.limiter import StreamLimiter
from tapo_desktop.core.media_engine.profiles import build_rtsp_url
from tapo_desktop.core.media_engine.supervisor import StreamState, StreamSupervisor
from tapo_desktop.models import StreamQuality
from tests.integration._media_helpers import Recorder, ffmpeg_children, url_password, wait_until

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]

SD = (640, 360)
HD = (1280, 720)


@pytest.fixture
def make_sup(ffmpeg_path):
    created: list[StreamSupervisor] = []

    def factory(url, size=SD, rec: Recorder | None = None, audio=False, **kw):
        rec = rec or Recorder()
        kw.setdefault("ffmpeg_path", ffmpeg_path)
        sup = StreamSupervisor(url, size[0], size[1], **rec.kwargs(audio=audio), **kw)
        created.append(sup)
        return sup, rec

    yield factory
    for s in created:
        try:
            s.stop()
        except Exception:
            pass


def _playing(sup, timeout=15.0) -> bool:
    return wait_until(lambda: sup.state is StreamState.PLAYING, timeout)


def test_plays_stream2_with_exact_frame_size(make_sup, mock_camera, caplog):
    caplog.set_level(logging.DEBUG)
    url = mock_camera.rtsp_url("stream2")
    sup, rec = make_sup(url)
    t0 = time.monotonic()
    sup.start()
    assert time.monotonic() - t0 < 0.5, "start() must not block"
    assert _playing(sup), rec.state_names()
    assert wait_until(lambda: rec.frame_count() >= 15, 10)
    frames = list(rec.frames)
    for f in frames:
        assert (f.width, f.height) == SD
        assert f.size == 640 * 360 * 3
    idx = [f.index for f in frames]
    assert idx == sorted(idx) and len(set(idx)) == len(idx)
    ts = [f.timestamp for f in frames]
    assert ts == sorted(ts)
    data = rec.last_frame.data
    assert len(set(data[:: 997])) > 4, "frame looks blank"
    assert rec.state_names()[:2] == ["CONNECTING", "PLAYING"]
    assert mock_camera.rtsp_active_clients == 1
    pid = sup.pid
    assert pid and "ffmpeg" in psutil.Process(pid).name().lower()
    time.sleep(2.0)
    st = sup.stats()
    assert st["frames"] >= 30
    assert 8.0 <= st["fps"] <= 25.0  # mock paces ~15 fps
    assert st["last_frame_age"] is not None and st["last_frame_age"] < 1.0
    assert st["reconnects"] == 0
    assert st["decoder"] in ("d3d11va", "software")
    sup.stop()
    assert sup.state is StreamState.STOPPED
    assert rec.state_names()[-1] == "STOPPED"
    assert url_password(url) not in caplog.text
    assert all(url_password(url) not in r for r in rec.reasons())
    assert not rec.callback_errors


def test_switch_to_stream1_hd(make_sup, mock_camera):
    lim = StreamLimiter()
    sup, rec = make_sup(mock_camera.rtsp_url("stream2"), limiter=lim, limiter_key="cam")
    sup.start()
    assert _playing(sup)
    assert wait_until(lambda: rec.frame_count() >= 5, 10)
    t_switch = time.monotonic()
    sup.switch(mock_camera.rtsp_url("stream1"), *HD)
    assert wait_until(lambda: any((f.width, f.height) == HD for f in rec.frames_after(t_switch)), 10)
    first_hd = next(f for f in rec.frames if (f.width, f.height) == HD)
    assert first_hd.timestamp - t_switch < 5.0
    assert wait_until(lambda: len([f for f in rec.frames if f.timestamp > first_hd.timestamp]) >= 10, 10)
    after = [f for f in rec.frames if f.timestamp >= first_hd.timestamp]
    assert all((f.width, f.height, f.size) == (1280, 720, 1280 * 720 * 3) for f in after), "mixed sizes after switch"
    assert lim.active("cam") == 1, "switch must keep exactly one lease"
    assert wait_until(lambda: mock_camera.rtsp_active_clients == 1, 5), "old RTSP session not torn down"
    assert sup.state is StreamState.PLAYING
    assert wait_until(lambda: len(ffmpeg_children()) == 1, 5)


def test_third_stream_with_shared_limiter_fails_fast(make_sup, mock_camera):
    lim = StreamLimiter(max_per_host=2)
    a, _ = make_sup(mock_camera.rtsp_url("stream2"), limiter=lim)
    b, _ = make_sup(mock_camera.rtsp_url("stream1"), size=HD, limiter=lim)
    a.start()
    b.start()
    assert _playing(a) and _playing(b)
    c, rec_c = make_sup(mock_camera.rtsp_url("stream2"), limiter=lim)
    t0 = time.monotonic()
    c.start()
    assert wait_until(lambda: c.state is StreamState.FAILED, 3.0)
    assert time.monotonic() - t0 < 3.0
    assert any("limit" in r.lower() for r in rec_c.reasons())
    assert c.pid is None
    assert mock_camera.rtsp_active_clients == 2
    assert mock_camera.rtsp_reject_count == 0, "limiter must refuse before touching the camera"
    assert a.state is StreamState.PLAYING and b.state is StreamState.PLAYING
    assert lim.active(mock_camera.host) == 2


def test_camera_enforces_cap_when_limiter_bypassed_and_slot_is_reused(make_sup, mock_camera):
    a, _ = make_sup(mock_camera.rtsp_url("stream2"), limiter=StreamLimiter(10))
    b, _ = make_sup(mock_camera.rtsp_url("stream2"), limiter=StreamLimiter(10))
    a.start()
    b.start()
    assert _playing(a) and _playing(b)
    c, rec_c = make_sup(mock_camera.rtsp_url("stream2"), limiter=StreamLimiter(10),
                        backoff_initial=0.2, backoff_max=1.0)
    c.start()
    assert wait_until(lambda: mock_camera.rtsp_reject_count >= 1, 10)
    time.sleep(1.0)
    assert c.state in (StreamState.CONNECTING, StreamState.RECONNECTING)
    assert rec_c.frame_count() == 0
    assert a.state is StreamState.PLAYING and b.state is StreamState.PLAYING
    assert mock_camera.rtsp_active_clients == 2
    a.stop()
    assert _playing(c, 10), rec_c.state_names()
    assert b.state is StreamState.PLAYING


def test_rtsp_busy_453_is_retried(make_sup, mock_camera):
    mock_camera.inject_rtsp_busy(times=2)
    sup, rec = make_sup(mock_camera.rtsp_url("stream2"), backoff_initial=0.2, backoff_max=1.0)
    sup.start()
    assert _playing(sup, 15), rec.state_names()
    assert sup.stats()["reconnects"] >= 1 or "RECONNECTING" in rec.state_names()


def test_max_retries_then_failed_on_unreachable_camera(make_sup):
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens here
    url = build_rtsp_url("127.0.0.1", "camuser", "NoLeakPw77", StreamQuality.SD, port=port)
    sup, rec = make_sup(url, max_retries=2, backoff_initial=0.1, backoff_max=0.2)
    sup.start()
    assert wait_until(lambda: sup.state is StreamState.FAILED, 15), rec.state_names()
    names = rec.state_names()
    assert names[0] == "CONNECTING" and names[-1] == "FAILED"
    assert "PLAYING" not in names
    assert all("NoLeakPw77" not in r for r in rec.reasons())
    assert wait_until(lambda: not ffmpeg_children(), 3)


def test_stop_returns_fast_and_leaves_no_ffmpeg(make_sup, mock_camera):
    lim = StreamLimiter()
    sups = [make_sup(mock_camera.rtsp_url(q), size=sz, limiter=lim) for q, sz in (("stream2", SD), ("stream1", HD))]
    for s, _ in sups:
        s.start()
    for s, _ in sups:
        assert _playing(s)
    pids = [s.pid for s, _ in sups]
    assert all(pids)
    assert len(ffmpeg_children()) == 2
    for s, rec in sups:
        t0 = time.monotonic()
        s.stop()
        assert time.monotonic() - t0 < 3.0
        assert s.state is StreamState.STOPPED
        assert s.pid is None
        n = rec.frame_count()
        time.sleep(0.3)
        assert rec.frame_count() == n, "on_frame called after stop() returned"
        s.stop()  # idempotent
    assert wait_until(lambda: not ffmpeg_children(), 1.0)
    for pid in pids:
        assert not psutil.pid_exists(pid)
    assert lim.active(mock_camera.host) == 0
    assert wait_until(lambda: mock_camera.rtsp_active_clients == 0, 5)


def test_ffmpeg_child_is_assigned_to_given_job(make_sup, mock_camera):
    job = JobObject()
    try:
        sup, _ = make_sup(mock_camera.rtsp_url("stream2"), job=job)
        sup.start()
        assert _playing(sup)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        h = k32.OpenProcess(0x1000, False, sup.pid)
        res = wintypes.BOOL()
        assert k32.IsProcessInJob(h, wintypes.HANDLE(job.handle), ctypes.byref(res))
        k32.CloseHandle(h)
        assert res.value
        pid = sup.pid
        job.close()  # closing the job kills ffmpeg; supervisor must notice and not crash
        assert wait_until(lambda: not psutil.pid_exists(pid), 1.0)
    finally:
        job.close()


def test_hwaccel_bogus_falls_back_to_software(make_sup, mock_camera):
    sup, rec = make_sup(mock_camera.rtsp_url("stream2"), hwaccel="bogus_accel")
    sup.start()
    assert _playing(sup, 20), rec.state_names()
    assert sup.stats()["decoder"] == "software"
    assert wait_until(lambda: rec.frame_count() >= 5, 5)
    assert all(f.size == 640 * 360 * 3 for f in rec.frames)


def test_hwaccel_auto_reports_decoder(make_sup, mock_camera):
    sup, _ = make_sup(mock_camera.rtsp_url("stream2"), hwaccel="auto")
    sup.start()
    assert _playing(sup)
    assert sup.stats()["decoder"] in ("d3d11va", "software")


def test_audio_chunks_are_s16le_8khz(make_sup, mock_camera):
    sup, rec = make_sup(mock_camera.rtsp_url("stream2"), audio=True)
    sup.start()
    assert _playing(sup)
    t0 = time.monotonic()
    time.sleep(3.0)
    got = rec.audio_bytes_after(t0)
    # 8000 Hz * 2 bytes * 3 s = 48000 bytes nominal; allow jitter/buffering
    assert 24000 <= got <= 72000, got
    assert not rec.callback_errors


def test_special_character_password_end_to_end(make_sup, ffmpeg_path):
    from tests.fixtures.mock_tapo_server import MockTapoCamera

    pw = "p@ss:w/rd#1%"
    with MockTapoCamera(cloud_password="cloudpass", camera_user="camuser", camera_password=pw) as cam:
        url = build_rtsp_url(cam.host, "camuser", pw, StreamQuality.SD, port=cam.rtsp_port)
        sup, rec = make_sup(url)
        sup.start()
        assert _playing(sup), rec.state_names()
        sup.stop()


def test_dual_lens_second_sensor_stream(make_sup, dual_lens_camera):
    url = dual_lens_camera.rtsp_url("stream7")
    sup, rec = make_sup(url)
    sup.start()
    assert _playing(sup)
    assert wait_until(lambda: rec.frame_count() >= 3, 5)
    assert all((f.width, f.height) == SD for f in rec.frames)


def test_single_lens_camera_has_no_stream6(make_sup, mock_camera):
    url = mock_camera.rtsp_url("stream6")
    sup, rec = make_sup(url, max_retries=1, backoff_initial=0.1, backoff_max=0.2)
    sup.start()
    assert wait_until(lambda: sup.state is StreamState.FAILED, 15), rec.state_names()
    assert rec.frame_count() == 0
