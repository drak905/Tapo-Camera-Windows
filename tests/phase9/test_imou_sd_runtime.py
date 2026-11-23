"""iMou runtime slots keyed by camera AND kind (live / playback): one cap counts both, nothing frees another's slot."""
from __future__ import annotations

import threading


def runtime(clock=None):
    from tapo_desktop.core.imou.runtime import ImouRuntime

    return ImouRuntime(client=object(), guard=object(), supervisor_factory=lambda **kw: None)


def test_the_old_calls_keep_working():
    rt = runtime()
    assert rt.try_acquire("a", 2) and rt.try_acquire("a", 2)  # idempotent for the same camera and kind
    assert rt.live_ids() == {"a"}
    rt.release("a")
    assert rt.live_ids() == set()


def test_slot_owner_kinds():
    rt = runtime()
    assert rt.try_acquire("a", 2, "live") and rt.owner("a") == "live"
    assert rt.try_acquire("b", 2, "playback") and rt.owner("b") == "playback"
    assert rt.owner("zzz") is None and rt.live_ids() == {"a", "b"}


def test_one_camera_has_one_slot_so_a_second_kind_is_refused():
    rt = runtime()
    assert rt.try_acquire("a", 2, "live")
    assert not rt.try_acquire("a", 2, "playback")  # a transfer is the only way to change the owner
    assert rt.owner("a") == "live"


def test_release_of_the_wrong_kind_frees_nothing():
    rt = runtime()
    rt.try_acquire("a", 2, "playback")
    rt.release("a", "live")
    assert rt.owner("a") == "playback"
    rt.release("a", "playback")
    assert rt.owner("a") is None
    rt.release("a", "playback")  # idempotent


def test_transfer_live_to_playback_is_atomic_and_needs_the_expected_owner():
    rt = runtime()
    rt.try_acquire("a", 1, "live")
    assert rt.transfer("a", "live", "playback") is True
    assert rt.owner("a") == "playback" and rt.live_ids() == {"a"}
    assert rt.transfer("a", "live", "playback") is False  # not the owner any more
    assert rt.transfer("nobody", "live", "playback") is False


def test_cap_counts_live_and_playback_together():
    rt = runtime()
    assert rt.try_acquire("a", 2, "live") and rt.try_acquire("b", 2, "playback")
    assert not rt.try_acquire("c", 2, "live") and not rt.try_acquire("c", 2, "playback")
    rt.release("a", "live")
    assert rt.try_acquire("c", 2, "playback")


def test_a_transfer_never_passes_the_cap():
    rt = runtime()
    rt.try_acquire("a", 1, "live")
    assert rt.transfer("a", "live", "playback")
    assert not rt.try_acquire("b", 1, "live")


def test_slots_are_thread_safe_under_a_race_for_the_last_slot():
    rt = runtime()
    wins: list[str] = []

    def take(cid, kind):
        if rt.try_acquire(cid, 3, kind):
            wins.append(cid)

    threads = [threading.Thread(target=take, args=(f"c{i}", "live" if i % 2 else "playback")) for i in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(wins) == 3 and len(rt.live_ids()) == 3
