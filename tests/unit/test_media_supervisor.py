"""Tier 1: StreamSupervisor behaviour that needs no camera (states, limiter refusal, stop semantics)."""
from __future__ import annotations

import dataclasses
import threading
import time

import psutil
import pytest

from tapo_desktop.core.media_engine.limiter import StreamLimiter
from tapo_desktop.core.media_engine.supervisor import Frame, StreamState, StreamSupervisor

pytestmark = pytest.mark.unit

URL = "rtsp://camuser:Pa55w0rdXYZ@127.0.0.1:1/stream2"  # port 1: nothing listens


def _ffmpeg_children() -> list[psutil.Process]:
    out = []
    for c in psutil.Process().children(recursive=True):
        try:
            if "ffmpeg" in c.name().lower():
                out.append(c)
        except psutil.NoSuchProcess:
            pass
    return out


def test_initial_state_and_stats():
    assert [s.name for s in StreamState] == ["IDLE", "CONNECTING", "PLAYING", "RECONNECTING", "STOPPED", "FAILED"]
    f = Frame(width=2, height=1, data=b"\x00" * 6, index=0, timestamp=1.0)
    assert dataclasses.is_dataclass(f)
    with pytest.raises(dataclasses.FrozenInstanceError):
        f.index = 1  # type: ignore[misc]
    sup = StreamSupervisor(URL, 640, 360, on_frame=lambda f: None)
    assert sup.state is StreamState.IDLE
    assert sup.pid is None
    st = sup.stats()
    assert {"frames", "reconnects", "fps", "last_frame_age", "decoder"} <= set(st)
    assert st["frames"] == 0
    assert st["reconnects"] == 0


def test_stop_before_start_is_safe_and_idempotent():
    sup = StreamSupervisor(URL, 640, 360, on_frame=lambda f: None)
    sup.stop()
    sup.stop()
    assert sup.state in (StreamState.IDLE, StreamState.STOPPED)
    assert sup.pid is None


def _wait_state(sup, wanted, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sup.state is wanted:
            return True
        time.sleep(0.01)
    return sup.state is wanted


def test_limiter_full_means_failed_without_spawning_ffmpeg():
    lim = StreamLimiter(max_per_host=2)
    lim.acquire("cam-A")
    lim.acquire("cam-A")
    states: list[tuple[StreamState, str | None]] = []
    got_failed = threading.Event()

    def on_state(state, reason):
        states.append((state, reason))
        if state is StreamState.FAILED:
            got_failed.set()

    before = {p.pid for p in _ffmpeg_children()}
    sup = StreamSupervisor(
        URL, 640, 360, on_frame=lambda f: None, on_state=on_state, limiter=lim, limiter_key="cam-A",
    )
    t0 = time.monotonic()
    sup.start()  # must not block
    assert time.monotonic() - t0 < 0.5
    try:
        assert got_failed.wait(2.0), states
        assert sup.state is StreamState.FAILED
        reason = [r for s, r in states if s is StreamState.FAILED][-1]
        assert reason and "limit" in reason.lower()
        assert "Pa55w0rdXYZ" not in reason
        assert sup.pid is None
        assert {p.pid for p in _ffmpeg_children()} == before
        assert lim.active("cam-A") == 2  # did not steal or leak a slot
    finally:
        sup.stop()
    assert lim.active("cam-A") == 2


def test_limiter_key_defaults_to_url_host():
    lim = StreamLimiter(max_per_host=1)
    lim.acquire("127.0.0.1")
    failed = threading.Event()
    sup = StreamSupervisor(
        URL, 640, 360, on_frame=lambda f: None,
        on_state=lambda s, r: failed.set() if s is StreamState.FAILED else None,
        limiter=lim,
    )
    sup.start()
    try:
        assert failed.wait(2.0)
    finally:
        sup.stop()
    assert lim.active("127.0.0.1") == 1


def test_lease_released_after_stop():
    lim = StreamLimiter(max_per_host=2)
    sup = StreamSupervisor(
        URL, 640, 360, on_frame=lambda f: None, limiter=lim, limiter_key="k",
        backoff_initial=0.05, backoff_max=0.1,
    )
    sup.start()
    deadline = time.monotonic() + 2
    while lim.active("k") == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert lim.active("k") == 1  # held while running, even while (re)connecting
    t0 = time.monotonic()
    sup.stop()
    assert time.monotonic() - t0 < 3.0
    assert lim.active("k") == 0
    assert sup.state is StreamState.STOPPED
    sup.stop()
    assert lim.active("k") == 0
