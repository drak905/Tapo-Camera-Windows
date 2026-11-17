"""Tier 2: LocalStreamSupervisor (live view over :8800, like the phone app) against the mock camera.

Contract: docs/CONTRACT_PHASE2.md, lane P2-live. Fixtures from tests/conftest.py: mock_camera,
ffmpeg_path. The mock preview loops a 6 s clip (90 frames, 15 fps) in real time.
"""
from __future__ import annotations

import logging
import time

import pytest

from tapo_desktop.core.media_engine.limiter import StreamLimiter
from tapo_desktop.core.media_engine.local_stream import LocalStreamSupervisor
from tapo_desktop.core.media_engine.supervisor import StreamState
from tests.integration._media_helpers import Recorder, ffmpeg_children, wait_until

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


@pytest.fixture
def make_local(ffmpeg_path):
    created: list[LocalStreamSupervisor] = []

    def factory(cam, width=640, height=360, *, password=None, port=None, rec=None, **kw):
        rec = rec or Recorder()
        kw.setdefault("ffmpeg_path", ffmpeg_path)
        kw.setdefault("stall_timeout", 2.0)
        sup = LocalStreamSupervisor(cam.host, cam.cloud_password if password is None else password, width, height,
                                    port=cam.media_port if port is None else port, **rec.kwargs(), **kw)
        created.append(sup)
        return sup, rec

    yield factory
    for s in created:
        try:
            s.stop()
        except Exception:
            pass


def _previews(cam) -> list[dict]:
    out = []
    for entry in cam.media_request_log:
        body = entry["body"]
        if isinstance(body, dict) and isinstance(body.get("params"), dict) and "preview" in body["params"]:
            out.append(body["params"]["preview"])
    return out


def _play(sup, rec, frames=10):
    sup.start()
    assert wait_until(lambda: sup.state is StreamState.PLAYING, 15), (rec.state_names(), rec.reasons())
    assert wait_until(lambda: rec.frame_count() >= frames, 10)


@pytest.mark.parametrize("resolution,size", [("VGA", (640, 360)), ("HD", (1280, 720)), ("VGA", (480, 270))])
def test_plays_with_exact_frame_size(make_local, mock_camera, resolution, size):
    sup, rec = make_local(mock_camera, *size, resolution=resolution)
    t0 = time.monotonic()
    sup.start()
    assert time.monotonic() - t0 < 0.5, "start() must not block"
    assert wait_until(lambda: sup.state is StreamState.PLAYING, 15), (rec.state_names(), rec.reasons())
    assert wait_until(lambda: rec.frame_count() >= 15, 10)
    assert rec.state_names()[:2] == ["CONNECTING", "PLAYING"]
    assert all((f.width, f.height, f.size) == (*size, size[0] * size[1] * 3) for f in rec.frames)
    assert rec.last_frame is not None and len(rec.last_frame.data) == size[0] * size[1] * 3
    preview = _previews(mock_camera)[0]
    assert preview["resolutions"] == [resolution] and preview["channels"] == [0]
    assert sup.pid is not None and sup.pid in {p.pid for p in ffmpeg_children()}


@pytest.mark.parametrize("resolution,size", [("VGA", (640, 360)), ("HD", (1280, 720))])
def test_frame_rate_matches_camera(make_local, mock_camera, resolution, size):
    """One output frame per camera frame (15 fps clip): no duplicated or dropped frames."""
    sup, rec = make_local(mock_camera, *size, resolution=resolution)
    _play(sup, rec, frames=5)
    t0 = time.monotonic()
    time.sleep(4.0)
    n = len(rec.frames_after(t0))
    assert 4 * 15 * 0.8 <= n <= 4 * 15 * 1.2, f"{n} frames in 4 s (expected ~60)"


def test_first_frame_quickly(make_local, mock_camera):
    sup, rec = make_local(mock_camera)
    t0 = time.monotonic()
    sup.start()
    assert wait_until(lambda: rec.frame_count() >= 1, 10)
    assert rec.frames[0].timestamp - t0 < 3.0, f"first frame after {rec.frames[0].timestamp - t0:.2f} s"


def test_switch_vga_to_hd(make_local, mock_camera):
    sup, rec = make_local(mock_camera, resolution="VGA")
    _play(sup, rec)
    t = time.monotonic()
    sup.switch("HD", 1280, 720)
    assert wait_until(lambda: any(f.width == 1280 for f in rec.frames_after(t)), 10), rec.state_names()
    hd = [f for f in rec.frames_after(t) if f.width == 1280]
    assert hd[0].timestamp - t < 3.0, f"first HD frame {hd[0].timestamp - t:.2f} s after switch"
    assert all((f.height, f.size) == (720, 1280 * 720 * 3) for f in hd)
    assert wait_until(lambda: len([f for f in rec.frames_after(t) if f.width == 1280]) >= 10, 5)
    late = rec.frames_after(time.monotonic() - 0.3)
    assert late and all(f.width == 1280 for f in late), "VGA frames still arriving after the switch"
    assert [p["resolutions"] for p in _previews(mock_camera)] == [["VGA"], ["HD"]]
    assert sup.state is StreamState.PLAYING
    assert sup.stats()["reconnects"] == 0
    assert wait_until(lambda: mock_camera.media_active_sessions == 1, 3), "old :8800 session not closed"
    assert wait_until(lambda: len(ffmpeg_children()) == 1, 3), "old ffmpeg not reaped after switch"


def test_frames_flow_20s_without_reconnects(make_local, mock_camera):
    """Longer than 3 clip loops and far beyond the 50-part ack window: proves acks and looping work."""
    sup, rec = make_local(mock_camera)
    _play(sup, rec)
    t0 = time.monotonic()
    time.sleep(20.5)
    frames = rec.frames_after(t0)
    assert len(frames) >= 20 * 15 * 0.85, f"only {len(frames)} frames in 20 s"
    gaps = [b.timestamp - a.timestamp for a, b in zip(frames, frames[1:])]
    assert max(gaps) < 1.0, f"frame gap of {max(gaps):.2f} s"
    assert [s for ts, s, _ in rec.states if ts > t0] == [], rec.state_names()
    stats = sup.stats()
    assert stats["reconnects"] == 0 and 10 <= stats["fps"] <= 20, stats
    acks = mock_camera.media_acks
    assert len(acks) >= 10 and all(a % 25 == 0 for a in acks), acks[:10]
    assert len(_previews(mock_camera)) == 1


def test_wrong_password_fails_with_auth(make_local, mock_camera, caplog):
    caplog.set_level(logging.DEBUG)
    secret = "definitely-wrong-pw"
    sup, rec = make_local(mock_camera, password=secret, max_retries=5)
    sup.start()
    assert wait_until(lambda: sup.state is StreamState.FAILED, 10), rec.state_names()
    reason = rec.reasons()[-1]
    assert "auth" in reason.lower(), reason
    assert ffmpeg_children() == []
    assert secret not in caplog.text and all(secret not in r for r in rec.reasons())
    assert mock_camera.media_active_sessions == 0


def test_third_concurrent_session_fails_with_limit(make_local, mock_camera):
    first, rec1 = make_local(mock_camera)
    second, rec2 = make_local(mock_camera)
    _play(first, rec1)
    _play(second, rec2)
    third, rec3 = make_local(mock_camera)
    third.start()
    assert wait_until(lambda: third.state is StreamState.FAILED, 10), rec3.state_names()
    assert "limit" in rec3.reasons()[-1].lower(), rec3.reasons()
    assert first.state is StreamState.PLAYING and second.state is StreamState.PLAYING
    assert len(ffmpeg_children()) == 2


def test_limiter_lease_refused_without_connecting(make_local, mock_camera):
    limiter = StreamLimiter(max_per_host=1)
    first, rec1 = make_local(mock_camera, limiter=limiter, limiter_key="cam-1")
    _play(first, rec1)
    second, rec2 = make_local(mock_camera, limiter=limiter, limiter_key="cam-1")
    second.start()
    assert wait_until(lambda: second.state is StreamState.FAILED, 2)
    assert "limit" in rec2.reasons()[-1].lower()
    assert mock_camera.media_active_sessions == 1
    first.stop()
    assert limiter.active("cam-1") == 0


def test_stop_is_fast_and_leaves_nothing_behind(make_local, mock_camera):
    sup, rec = make_local(mock_camera)
    _play(sup, rec)
    t0 = time.monotonic()
    sup.stop()
    assert time.monotonic() - t0 < 3.0
    stopped_at = time.monotonic()
    assert sup.state is StreamState.STOPPED and sup.pid is None
    assert wait_until(lambda: ffmpeg_children() == [], 2), "ffmpeg child left running"
    assert wait_until(lambda: mock_camera.media_active_sessions == 0, 3), ":8800 session left open"
    time.sleep(0.3)
    assert rec.frames_after(stopped_at) == []
    assert rec.state_names()[-1] == "STOPPED"
    sup.stop()  # idempotent


def test_stop_during_connect_is_fast(make_local, mock_camera, chaos_proxy):
    proxy = chaos_proxy(mock_camera.host, mock_camera.media_port)
    proxy.blackhole(10)
    sup, rec = make_local(mock_camera, port=proxy.port)
    sup.start()
    time.sleep(0.5)
    t0 = time.monotonic()
    sup.stop()
    assert time.monotonic() - t0 < 3.0
    assert sup.state is StreamState.STOPPED
    assert ffmpeg_children() == []


def test_stats_shape(make_local, mock_camera):
    sup, rec = make_local(mock_camera)
    before = sup.stats()
    assert before["frames"] == 0 and before["reconnects"] == 0
    _play(sup, rec, frames=20)
    stats = sup.stats()
    assert set(stats) >= {"frames", "reconnects", "fps", "last_frame_age", "decoder"}
    assert stats["frames"] >= 20 and stats["reconnects"] == 0
    assert stats["fps"] > 5
    assert stats["decoder"] in ("d3d11va", "software")
    assert stats["last_frame_age"] is not None and stats["last_frame_age"] < 1.0


def test_audio_track_delivered_when_requested(mock_camera, ffmpeg_path):
    """The mock TS carries AAC (ffmpeg can decode it); s16le 8 kHz mono chunks reach on_audio."""
    rec = Recorder()
    sup = LocalStreamSupervisor(mock_camera.host, mock_camera.cloud_password, 640, 360, port=mock_camera.media_port,
                                ffmpeg_path=ffmpeg_path, stall_timeout=2.0, **rec.kwargs(audio=True))
    try:
        _play(sup, rec)
        assert wait_until(lambda: rec.audio_bytes_after(0) > 8000, 6), "no audio"
        assert rec.callback_errors == []
    finally:
        sup.stop()


def test_software_decoding(make_local, mock_camera):
    sup, rec = make_local(mock_camera, hwaccel="none")
    _play(sup, rec)
    assert sup.stats()["decoder"] == "software"


def test_unreachable_camera_reconnects_then_gives_up(make_local, mock_camera):
    port = mock_camera.media_port
    mock_camera.stop()
    sup, rec = make_local(mock_camera, port=port, max_retries=1, backoff_initial=0.2, backoff_max=0.4)
    sup.start()
    assert wait_until(lambda: sup.state is StreamState.FAILED, 20), rec.state_names()
    assert "RECONNECTING" in rec.state_names()
    assert ffmpeg_children() == []


def test_repr_and_reasons_hide_password(make_local, mock_camera):
    sup, _ = make_local(mock_camera)
    assert mock_camera.cloud_password not in repr(sup)
