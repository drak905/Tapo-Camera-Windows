"""PlaybackStreamSupervisor (LAN :8800) and RelayPlaybackStreamSupervisor (cloud relay, streamType 1)
against the continuous-playback mocks: exact frame size, ~1x pacing although the camera sends faster,
speed, pause / resume, seek, continuous playback across recordings, end of recordings, reconnect
from the last position, fast stop without leftover ffmpeg."""
from __future__ import annotations

import json
import threading
import time

import pytest

from tapo_desktop.core.media_engine.supervisor import StreamState
from tests.integration._media_helpers import Recorder, ffmpeg_children, wait_until
from tests.phase5.conftest import BASE, DEVICE_ID, RECS

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]


class Positions:
    def __init__(self):
        self.items: list[tuple[float, float]] = []  # (monotonic, utc)
        self._lock = threading.Lock()

    def __call__(self, utc: float) -> None:
        with self._lock:
            self.items.append((time.monotonic(), float(utc)))

    @property
    def last(self) -> float | None:
        with self._lock:
            return self.items[-1][1] if self.items else None

    def after(self, t: float) -> list[float]:
        with self._lock:
            return [u for ts, u in self.items if ts > t]

    def all(self) -> list[float]:
        with self._lock:
            return [u for _, u in self.items]


def _get_playbacks(cam) -> list[dict]:
    out = []
    for entry in cam.media_request_log:
        body = entry["body"]
        if isinstance(body, dict) and isinstance(body.get("params"), dict) and "playback" in body["params"]:
            out.append(body["params"]["playback"])
    return out


@pytest.fixture
def make_pb(ffmpeg_path):
    created = []

    def factory(cam, *, start=BASE, end=BASE + 3600, segments=RECS, width=640, height=360, **kw):
        from tapo_desktop.core.media_engine.playback_stream import PlaybackStreamSupervisor

        rec, pos = Recorder(), Positions()
        kw.setdefault("ffmpeg_path", ffmpeg_path)
        kw.setdefault("stall_timeout", 4.0)
        sup = PlaybackStreamSupervisor(cam.host, cam.cloud_password, width, height, port=cam.media_port,
                                       start_utc=start, end_utc=end, segments=segments, on_position=pos,
                                       **rec.kwargs(), **kw)
        created.append(sup)
        return sup, rec, pos

    yield factory
    for s in created:
        try:
            s.stop()
        except Exception:
            pass


@pytest.fixture
def make_relay_pb(ffmpeg_path, relay_client, mock_relay):
    created = []
    mock_relay.playback_continuous = True
    mock_relay.playback_recordings = RECS
    mock_relay.playback_speed = None

    def factory(*, start=BASE, end=BASE + 3600, segments=RECS, **kw):
        from tapo_desktop.core.media_engine.playback_stream import RelayPlaybackStreamSupervisor

        rec, pos = Recorder(), Positions()
        kw.setdefault("ffmpeg_path", ffmpeg_path)
        kw.setdefault("stall_timeout", 4.0)
        sup = RelayPlaybackStreamSupervisor(relay_client, DEVICE_ID, 640, 360, model="C210", start_utc=start,
                                            end_utc=end, segments=segments, on_position=pos, **rec.kwargs(), **kw)
        created.append(sup)
        return sup, rec, pos

    yield factory
    for s in created:
        try:
            s.stop()
        except Exception:
            pass


def _playing(sup, rec, frames=5, timeout=20):
    sup.start()
    assert wait_until(lambda: sup.state is StreamState.PLAYING, timeout), (rec.state_names(), rec.reasons())
    assert wait_until(lambda: rec.frame_count() >= frames, 10)


def _rate(pos: Positions, seconds: float) -> float:
    """Recording seconds played per wall second over ``seconds``."""
    assert wait_until(lambda: pos.last is not None, 10)
    p0, t0 = pos.last, time.monotonic()
    time.sleep(seconds)
    return (pos.last - p0) / (time.monotonic() - t0)


# ------------------------------------------------------------------------------------- builders
def test_request_builders():
    from tapo_desktop.core.media_engine.playback_stream import (
        PLAYBACK_SPEEDS,
        build_pause_request,
        build_range_playback_request,
        build_resume_request,
    )

    assert build_range_playback_request(1700000000, 1700086399) == {"type": "request", "params": {
        "method": "get", "playback": {"client_id": 1, "channels": [0], "scale": "1/1", "start_time": "1700000000",
                                      "end_time": "1700086399", "event_type": [1, 2]}}}
    assert build_pause_request() == {"type": "request", "params": {"method": "do", "pause": "null"}}
    assert build_resume_request() == {"type": "request", "params": {"method": "do", "play": "null"}}
    assert list(PLAYBACK_SPEEDS) == ["1/1", "2/1", "4/1", "8/1"]
    with pytest.raises(ValueError):
        build_range_playback_request(0, 10)


# ------------------------------------------------------------------------------------- LAN
def test_lan_frames_of_exact_size_and_request(make_pb, playback_camera):
    sup, rec, pos = make_pb(playback_camera, width=480, height=270)
    t0 = time.monotonic()
    sup.start()
    assert time.monotonic() - t0 < 0.5, "start() must not block"
    assert wait_until(lambda: sup.state is StreamState.PLAYING, 20), (rec.state_names(), rec.reasons())
    assert wait_until(lambda: rec.frame_count() >= 10, 10)
    assert all((f.width, f.height, f.size) == (480, 270, 480 * 270 * 3) for f in rec.frames)
    req = _get_playbacks(playback_camera)[0]
    assert req == {"client_id": 1, "channels": [0], "scale": "1/1", "start_time": str(BASE),
                   "end_time": str(BASE + 3600), "event_type": [1, 2]}
    assert wait_until(lambda: pos.last is not None and BASE <= pos.last <= BASE + 20, 5)
    assert sup.pid is not None and sup.pid in {p.pid for p in ffmpeg_children()}


def test_lan_paced_at_recording_speed_although_camera_is_faster(make_pb, playback_camera):
    sup, rec, pos = make_pb(playback_camera)
    _playing(sup, rec)
    t0 = time.monotonic()
    rate = _rate(pos, 3.0)
    assert 0.75 <= rate <= 1.25, f"played {rate:.2f} s of recording per second"
    n = len(rec.frames_after(t0))
    assert 3 * 15 * 0.6 <= n <= 3 * 15 * 1.3, f"{n} frames in 3 s (15 fps recording)"


def test_lan_speed_changes_pace(make_pb, playback_camera):
    sup, rec, pos = make_pb(playback_camera)
    _playing(sup, rec)
    sup.set_speed("2/1")
    time.sleep(0.3)
    rate = _rate(pos, 2.5)
    assert 1.5 <= rate <= 2.5, f"2x speed played {rate:.2f} s per second"
    with pytest.raises(ValueError):
        sup.set_speed("3/1")


def test_lan_pause_and_resume(make_pb, playback_camera):
    sup, rec, pos = make_pb(playback_camera)
    _playing(sup, rec)
    time.sleep(1.0)
    sup.pause()
    assert sup.paused
    time.sleep(0.5)
    frames, p = rec.frame_count(), pos.last
    time.sleep(2.0)
    assert rec.frame_count() - frames <= 1, "frames kept coming while paused"
    assert abs(pos.last - p) < 0.2
    assert sup.state is StreamState.PLAYING and "RECONNECTING" not in rec.state_names()
    assert wait_until(lambda: playback_camera.playback_feeds and playback_camera.playback_feeds[-1].pauses == 1, 3)
    t0 = time.monotonic()
    sup.resume()
    assert not sup.paused
    assert wait_until(lambda: len(rec.frames_after(t0)) >= 10, 5)
    assert wait_until(lambda: pos.last > p + 0.5, 5)
    assert playback_camera.playback_feeds[-1].resumes == 1


def test_lan_seek_moves_position(make_pb, playback_camera):
    sup, rec, pos = make_pb(playback_camera)
    _playing(sup, rec)
    target = BASE + 65
    t0 = time.monotonic()
    sup.seek(target)
    assert wait_until(lambda: any(target - 1 <= u <= target + 4 for u in pos.after(t0)), 8), pos.after(t0)[-5:]
    t1 = time.monotonic()
    assert wait_until(lambda: len(rec.frames_after(t1)) >= 5, 8)
    assert target - 1 <= pos.last <= target + 8
    assert any(int(r["start_time"]) in (target, target - 1) for r in _get_playbacks(playback_camera)[1:])


@pytest.mark.parametrize("pts_mode", ["reset", "continuous"])
def test_lan_continuous_across_clips_then_end_of_recordings(make_pb, playback_camera, pts_mode):
    from tapo_desktop.core.media_engine.playback_stream import END_OF_RECORDINGS

    recs = [(BASE, BASE + 3), (BASE + 30, BASE + 33)]
    playback_camera.state.recordings = __import__("tests.phase5.conftest", fromlist=["clips_of"]).clips_of(recs)
    playback_camera.media_playback_pts = pts_mode
    sup, rec, pos = make_pb(playback_camera, segments=recs)
    sup.start()
    assert wait_until(lambda: sup.state is StreamState.STOPPED, 25), (rec.state_names(), rec.reasons())
    assert rec.reasons()[-1] == END_OF_RECORDINGS
    seen = pos.all()
    assert any(BASE <= u <= BASE + 3.5 for u in seen), seen[:5]
    assert any(BASE + 30 <= u <= BASE + 33.5 for u in seen), seen[-5:]
    assert not any(BASE + 4.5 < u < BASE + 29.5 for u in seen), "position entered the gap between recordings"
    assert "FAILED" not in rec.state_names()
    assert wait_until(lambda: sup.pid is None, 3)


def test_lan_reconnect_resumes_near_last_position(make_pb, playback_camera):
    sup, rec, pos = make_pb(playback_camera, stall_timeout=3.0)
    _playing(sup, rec)
    time.sleep(2.0)
    before = pos.last
    playback_camera.drop_media_connections()
    assert wait_until(lambda: "RECONNECTING" in rec.state_names(), 8), rec.state_names()
    t0 = time.monotonic()
    assert wait_until(lambda: len(rec.frames_after(t0)) >= 5, 15), rec.reasons()
    requests = _get_playbacks(playback_camera)
    assert len(requests) >= 2
    assert before - 2 <= int(requests[-1]["start_time"]) <= before + 1
    assert before - 2.5 <= pos.last <= before + 6


def test_lan_stop_is_fast_and_leaves_no_ffmpeg(make_pb, playback_camera):
    sup, rec, pos = make_pb(playback_camera)
    _playing(sup, rec)
    pid = sup.pid
    t0 = time.monotonic()
    sup.stop()
    assert time.monotonic() - t0 < 3.0
    assert sup.state is StreamState.STOPPED
    assert wait_until(lambda: pid not in {p.pid for p in ffmpeg_children()}, 3)
    assert wait_until(lambda: playback_camera.media_active_sessions == 0, 5)


def test_lan_camera_refuses_another_session(make_pb, playback_camera):
    playback_camera.max_media_sessions = 0
    sup, rec, pos = make_pb(playback_camera)
    sup.start()
    assert wait_until(lambda: sup.state is StreamState.FAILED, 15), rec.state_names()
    assert "limit" in rec.reasons()[-1].lower()


def test_lan_live_view_and_playback_together(make_pb, playback_camera, ffmpeg_path):
    from tapo_desktop.core.media_engine.limiter import StreamLimiter
    from tapo_desktop.core.media_engine.local_stream import LocalStreamSupervisor

    limiter = StreamLimiter()
    live_rec = Recorder()
    live = LocalStreamSupervisor(playback_camera.host, playback_camera.cloud_password, 320, 180,
                                 port=playback_camera.media_port, ffmpeg_path=ffmpeg_path, limiter=limiter,
                                 limiter_key="cam", **live_rec.kwargs())
    try:
        live.start()
        sup, rec, pos = make_pb(playback_camera, limiter=limiter, limiter_key="cam")
        _playing(sup, rec)
        assert wait_until(lambda: live_rec.frame_count() >= 5, 15)
        assert playback_camera.media_active_sessions == 2
    finally:
        live.stop()


# ------------------------------------------------------------------------------------- relay
def test_relay_frames_pacing_and_slot(make_relay_pb, relay_cloud, mock_relay):
    sup, rec, pos = make_relay_pb()
    _playing(sup, rec)
    assert all((f.width, f.height) == (640, 360) and f.size == 640 * 360 * 3 for f in rec.frames)
    rate = _rate(pos, 2.5)
    assert 0.7 <= rate <= 1.3, f"relay playback played {rate:.2f} s per second"
    body = [r["body"] for r in relay_cloud.calls("/v2/relay/request")][-1]
    assert body["streamType"] == 1 and body["channelId"] == "0" and json.loads(body["customParams"]) == {}
    assert "resolution" not in body and "preConnection" not in body
    req = mock_relay.vod_requests[-1]
    assert req["verb"] == "playback" and req["params"]["start_time"] == str(BASE)
    assert req["params"]["end_time"] == str(BASE + 3600)
    assert mock_relay.acks


def test_relay_pause_resume_and_stop(make_relay_pb, mock_relay):
    sup, rec, pos = make_relay_pb()
    _playing(sup, rec)
    sup.pause()
    time.sleep(0.5)
    frames, p = rec.frame_count(), pos.last
    time.sleep(1.5)
    assert rec.frame_count() - frames <= 1 and abs(pos.last - p) < 0.2
    assert wait_until(lambda: mock_relay.playback_feeds[-1].pauses == 1, 3)
    t0 = time.monotonic()
    sup.resume()
    assert wait_until(lambda: len(rec.frames_after(t0)) >= 5, 8)
    t1 = time.monotonic()
    sup.stop()
    assert time.monotonic() - t1 < 3.0
    assert wait_until(lambda: mock_relay.open_tunnels == 0, 5)


def test_relay_end_of_recordings(make_relay_pb, mock_relay):
    from tapo_desktop.core.media_engine.playback_stream import END_OF_RECORDINGS

    recs = [(BASE, BASE + 3), (BASE + 30, BASE + 33)]
    mock_relay.playback_recordings = recs
    sup, rec, pos = make_relay_pb(segments=recs)
    sup.set_speed("4/1")
    sup.start()
    assert wait_until(lambda: sup.state is StreamState.STOPPED, 25), (rec.state_names(), rec.reasons())
    assert rec.reasons()[-1] == END_OF_RECORDINGS
    assert any(BASE + 30 <= u <= BASE + 33.5 for u in pos.all())
