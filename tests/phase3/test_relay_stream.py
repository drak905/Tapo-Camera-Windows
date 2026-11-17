"""RelayStreamSupervisor: live view through the cloud relay (mock cloud + mock relay + real ffmpeg).
The mock relay loops the 6 s 15 fps mock clip in real time as plaintext TS parts."""
from __future__ import annotations

import time

import pytest

from tapo_desktop.core.media_engine.limiter import StreamLimiter
from tapo_desktop.core.media_engine.relay_stream import RelayStreamSupervisor
from tapo_desktop.core.media_engine.supervisor import StreamState
from tests.integration._media_helpers import Recorder, ffmpeg_children, wait_until
from tests.phase3.conftest import DEVICE

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]

DEV = DEVICE["deviceId"]


@pytest.fixture
def make_relay(ffmpeg_path, relay_client):
    created: list[RelayStreamSupervisor] = []

    def factory(width=640, height=360, *, rec=None, **kw):
        rec = rec or Recorder()
        kw.setdefault("ffmpeg_path", ffmpeg_path)
        kw.setdefault("stall_timeout", 2.0)
        kw.setdefault("model", "C210")
        sup = RelayStreamSupervisor(relay_client, DEV, width, height, **rec.kwargs(), **kw)
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
    assert wait_until(lambda: sup.state is StreamState.PLAYING, 20), (rec.state_names(), rec.reasons())
    assert wait_until(lambda: rec.frame_count() >= frames, 10)


@pytest.mark.parametrize("resolution,size", [("VGA", (640, 360)), ("HD", (1280, 720)), ("VGA", (480, 270))])
def test_plays_with_exact_frame_size(make_relay, mock_relay, relay_cloud, resolution, size):
    sup, rec = make_relay(*size, resolution=resolution)
    t0 = time.monotonic()
    sup.start()
    assert time.monotonic() - t0 < 0.5, "start() must not block"
    assert wait_until(lambda: sup.state is StreamState.PLAYING, 20), (rec.state_names(), rec.reasons())
    assert wait_until(lambda: rec.frame_count() >= 15, 10)
    assert rec.state_names()[:2] == ["CONNECTING", "PLAYING"]
    assert all((f.width, f.height, f.size) == (*size, size[0] * size[1] * 3) for f in rec.frames)
    assert relay_cloud.calls("/v2/relay/request")[-1]["body"]["resolution"] == resolution
    assert mock_relay.previews[-1]["resolution"] == resolution
    assert sup.pid is not None and sup.pid in {p.pid for p in ffmpeg_children()}
    assert DEV in repr(sup) and not any(tok in repr(sup) for tok in mock_relay.slots)


def test_heartbeat_while_playing(make_relay, mock_relay):
    sup, rec = make_relay(heartbeat_interval=0.5)
    _play(sup, rec, frames=3)
    assert wait_until(lambda: len(mock_relay.heartbeats) >= 2, 5)


def test_reconnects_after_relay_drop(make_relay, mock_relay, relay_cloud):
    sup, rec = make_relay()
    _play(sup, rec)
    slots_before = len(relay_cloud.calls("/v2/relay/request"))
    t = time.monotonic()
    mock_relay.drop_all()
    assert wait_until(lambda: rec.first_state_time(StreamState.RECONNECTING, t) is not None, 10), rec.state_names()
    assert wait_until(lambda: len(rec.frames_after(time.monotonic())) == 0 or
                      rec.first_state_time(StreamState.PLAYING, t) is not None, 15)
    assert wait_until(lambda: rec.first_state_time(StreamState.PLAYING, t) is not None, 15), rec.state_names()
    assert len(relay_cloud.calls("/v2/relay/request")) > slots_before, "a fresh relay slot is requested"
    assert wait_until(lambda: len(rec.frames_after(t + 0.5)) >= 5, 10)
    assert sup.stats()["reconnects"] >= 1


def test_stop_is_fast_and_clean(make_relay, mock_relay):
    sup, rec = make_relay()
    _play(sup, rec)
    t0 = time.monotonic()
    sup.stop()
    assert time.monotonic() - t0 < 3.0
    assert sup.state is StreamState.STOPPED
    assert wait_until(lambda: not ffmpeg_children(), 3), "ffmpeg left behind"
    assert wait_until(lambda: mock_relay.stops >= 1, 3), "no stop request before close"
    assert wait_until(lambda: mock_relay.open_tunnels == 0, 3)


def test_camera_offline_fails_without_retry(make_relay, mock_relay):
    mock_relay.mode = "offline"
    sup, rec = make_relay()
    sup.start()
    assert wait_until(lambda: sup.state is StreamState.FAILED, 15), rec.state_names()
    assert "camera is offline" in (rec.reasons()[-1] or "").lower()
    time.sleep(1.0)
    assert len(mock_relay.tunnels) == 1, "-1007 must not be retried"
    assert not ffmpeg_children()


def test_switch_vga_to_hd_requests_hd_slot(make_relay, mock_relay, relay_cloud):
    sup, rec = make_relay(resolution="VGA")
    _play(sup, rec)
    t = time.monotonic()
    sup.switch("HD", 1280, 720)
    assert wait_until(lambda: any(f.width == 1280 for f in rec.frames_after(t)), 15), rec.state_names()
    assert relay_cloud.calls("/v2/relay/request")[-1]["body"]["resolution"] == "HD"
    assert [p["resolution"] for p in mock_relay.previews] == ["VGA", "HD"]
    assert mock_relay.previews[-1]["slot_resolution"] == "HD"
    assert wait_until(lambda: mock_relay.open_tunnels == 1, 3), "old tunnel not closed"


def test_limiter_refusal(make_relay):
    limiter = StreamLimiter(max_per_host=1)
    lease = limiter.acquire(DEV)
    try:
        sup, rec = make_relay(limiter=limiter)
        sup.start()
        assert sup.state is StreamState.FAILED and "limit" in rec.reasons()[-1]
    finally:
        lease.release()
