"""Tier 1: per-camera RTSP concurrency limiter  [spec:stream.rtsp.limits max_simultaneous_rtsp_streams=2]."""
from __future__ import annotations

import threading
import time

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tapo_desktop.core.errors import StreamLimitError, TapoError
from tapo_desktop.core.media_engine.limiter import StreamLease, StreamLimiter

pytestmark = pytest.mark.unit


def test_default_cap_is_two_and_third_raises():
    lim = StreamLimiter()
    a = lim.acquire("cam1")
    b = lim.acquire("cam1")
    assert isinstance(a, StreamLease) and isinstance(b, StreamLease)
    assert lim.active("cam1") == 2
    with pytest.raises(StreamLimitError):
        lim.acquire("cam1")
    assert lim.active("cam1") == 2


def test_stream_limit_error_is_a_tapo_error():
    lim = StreamLimiter(max_per_host=1)
    lim.acquire("h")
    with pytest.raises(TapoError):
        lim.acquire("h")


def test_release_frees_a_slot():
    lim = StreamLimiter()
    a = lim.acquire("cam1")
    lim.acquire("cam1")
    a.release()
    assert lim.active("cam1") == 1
    lim.acquire("cam1")
    assert lim.active("cam1") == 2


def test_hosts_are_independent():
    lim = StreamLimiter()
    for _ in range(2):
        lim.acquire("10.0.0.1")
        lim.acquire("10.0.0.2")
    assert lim.active("10.0.0.1") == 2
    assert lim.active("10.0.0.2") == 2
    assert lim.active("10.0.0.3") == 0
    lim.acquire("10.0.0.3")
    with pytest.raises(StreamLimitError):
        lim.acquire("10.0.0.2")


def test_release_is_idempotent():
    lim = StreamLimiter()
    a = lim.acquire("cam")
    b = lim.acquire("cam")
    a.release()
    a.release()
    a.release()
    assert lim.active("cam") == 1  # b still counted
    b.release()
    assert lim.active("cam") == 0


def test_lease_is_a_context_manager():
    lim = StreamLimiter()
    with lim.acquire("cam") as lease:
        assert isinstance(lease, StreamLease)
        assert lim.active("cam") == 1
    assert lim.active("cam") == 0


def test_context_manager_releases_on_exception_and_explicit_release_after_is_noop():
    lim = StreamLimiter()
    with pytest.raises(RuntimeError):
        with lim.acquire("cam") as lease:
            raise RuntimeError("boom")
    assert lim.active("cam") == 0
    lease.release()
    assert lim.active("cam") == 0


def test_custom_cap():
    lim = StreamLimiter(max_per_host=3)
    for _ in range(3):
        lim.acquire("x")
    with pytest.raises(StreamLimitError):
        lim.acquire("x")


def test_acquire_with_timeout_waits_for_release():
    lim = StreamLimiter(max_per_host=1)
    first = lim.acquire("cam")
    threading.Timer(0.15, first.release).start()
    t0 = time.monotonic()
    lease = lim.acquire("cam", timeout=2.0)
    waited = time.monotonic() - t0
    assert 0.1 <= waited < 1.0
    assert lim.active("cam") == 1
    lease.release()


def test_acquire_with_timeout_gives_up_after_timeout():
    lim = StreamLimiter(max_per_host=1)
    lim.acquire("cam")
    t0 = time.monotonic()
    with pytest.raises(StreamLimitError):
        lim.acquire("cam", timeout=0.3)
    waited = time.monotonic() - t0
    assert 0.25 <= waited < 0.9


def test_zero_timeout_does_not_block():
    lim = StreamLimiter(max_per_host=1)
    lim.acquire("cam")
    t0 = time.monotonic()
    with pytest.raises(StreamLimitError):
        lim.acquire("cam")
    assert time.monotonic() - t0 < 0.05


def test_threads_never_exceed_cap():
    lim = StreamLimiter(max_per_host=2)
    inside = 0
    peak = 0
    guard = threading.Lock()
    acquired = 0
    errors: list[BaseException] = []

    def worker():
        nonlocal inside, peak, acquired
        try:
            for _ in range(200):
                try:
                    lease = lim.acquire("cam")
                except StreamLimitError:
                    continue
                with guard:
                    inside += 1
                    acquired += 1
                    peak = max(peak, inside)
                    assert lim.active("cam") <= 2
                with guard:
                    inside -= 1
                lease.release()
                lease.release()  # idempotent under contention too
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not errors
    assert peak <= 2
    assert acquired > 0
    assert lim.active("cam") == 0


def test_blocking_waiters_all_eventually_served():
    lim = StreamLimiter(max_per_host=2)
    served = []
    lock = threading.Lock()

    def worker(i):
        lease = lim.acquire("cam", timeout=5.0)
        try:
            with lock:
                served.append(i)
                assert lim.active("cam") <= 2
            time.sleep(0.01)
        finally:
            lease.release()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert sorted(served) == list(range(10))
    assert lim.active("cam") == 0


_ops = st.lists(
    st.tuples(st.sampled_from(["acq", "rel", "rel_again"]), st.sampled_from(["a", "b", "c"])),
    max_size=60,
)


@settings(max_examples=150, deadline=None)
@given(cap=st.integers(min_value=1, max_value=4), ops=_ops)
def test_matches_reference_model(cap, ops):
    lim = StreamLimiter(max_per_host=cap)
    held: dict[str, list[StreamLease]] = {"a": [], "b": [], "c": []}
    released: dict[str, list[StreamLease]] = {"a": [], "b": [], "c": []}
    for op, host in ops:
        if op == "acq":
            if len(held[host]) < cap:
                held[host].append(lim.acquire(host))
            else:
                with pytest.raises(StreamLimitError):
                    lim.acquire(host)
        elif op == "rel" and held[host]:
            lease = held[host].pop(0)
            lease.release()
            released[host].append(lease)
        elif op == "rel_again" and released[host]:
            released[host][-1].release()  # must not free someone else's slot
        for h in held:
            assert lim.active(h) == len(held[h])
