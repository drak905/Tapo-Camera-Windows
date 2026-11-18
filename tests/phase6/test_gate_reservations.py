"""RemoteStreamGate reservations: cameras in the view always get a relay slot (view-driven streaming);
the fixed ``capacity`` only limits streams outside the view. Without reservations the Phase 3 behaviour is
unchanged (tests/phase3/test_relay_service.py)."""
from __future__ import annotations

import threading

import pytest

from tapo_desktop.services.remote import RemoteStreamGate

pytestmark = pytest.mark.unit


def _noop() -> None:
    return None


def test_no_reservations_by_default():
    gate = RemoteStreamGate(2)
    assert gate.reserved() == frozenset()


def test_reserved_keys_acquire_beyond_capacity_without_evicting():
    gate = RemoteStreamGate(4)
    evicted: list[str] = []
    keys = [f"k{i}" for i in range(9)]
    gate.reserve(keys)
    for k in keys:
        assert gate.acquire(k, lambda k=k: evicted.append(k))
    assert gate.holders() == keys
    assert evicted == []
    assert gate.capacity == 4  # the setting itself is unchanged


def test_unreserved_keys_still_respect_capacity():
    gate = RemoteStreamGate(2)
    gate.reserve(["a", "b", "c"])
    for k in ("a", "b", "c"):
        assert gate.acquire(k, _noop)
    assert not gate.acquire("x", _noop)
    assert "x" not in gate.holders()


def test_preempt_never_evicts_a_reserved_holder():
    gate = RemoteStreamGate(2)
    evicted: list[str] = []
    gate.reserve(["a"])
    assert gate.acquire("a", lambda: evicted.append("a"))
    assert gate.acquire("u", lambda: evicted.append("u"))  # unreserved, fills capacity
    assert gate.acquire("v", lambda: evicted.append("v"), preempt=True)  # evicts the oldest unreserved
    assert evicted == ["u"] and gate.holders() == ["a", "v"]
    gate.reserve(["a", "v"])
    assert not gate.acquire("w", lambda: evicted.append("w"), preempt=True)  # only reserved holders left
    assert evicted == ["u"] and gate.holders() == ["a", "v"]


def test_reserve_replaces_the_previous_set_and_copies_it():
    gate = RemoteStreamGate(1)
    keys = ["a", "b"]
    gate.reserve(keys)
    keys.append("c")
    assert gate.reserved() == frozenset({"a", "b"})
    gate.reserve(["c"])
    assert gate.reserved() == frozenset({"c"})
    gate.reserve([])
    assert gate.reserved() == frozenset()


def test_unreserving_keeps_running_holders():
    gate = RemoteStreamGate(1)
    gate.reserve(["a", "b"])
    assert gate.acquire("a", _noop) and gate.acquire("b", _noop)
    gate.reserve([])
    assert gate.holders() == ["a", "b"]  # running streams keep their slot until released
    gate.release("a")
    gate.release("b")
    assert gate.holders() == []


def test_reserve_rejects_non_string_keys():
    gate = RemoteStreamGate(1)
    with pytest.raises(TypeError):
        gate.reserve([1])  # type: ignore[list-item]
    assert gate.reserved() == frozenset()


def test_repr_shows_reservations_without_keys():
    gate = RemoteStreamGate(4)
    gate.reserve(["secret-device-id"])
    assert "secret-device-id" not in repr(gate)
    assert "reserved=1" in repr(gate)


def test_reserved_acquire_is_thread_safe():
    gate = RemoteStreamGate(3)
    keys = [f"k{i}" for i in range(25)]
    gate.reserve(keys[:20])
    wins: list[str] = []
    lock = threading.Lock()

    def worker(k: str) -> None:
        if gate.acquire(k, _noop):
            with lock:
                wins.append(k)

    threads = [threading.Thread(target=worker, args=(k,)) for k in keys]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert set(keys[:20]) <= set(wins)
    assert len(gate.holders()) == len(wins)
    assert len([k for k in wins if k not in keys[:20]]) <= 3  # unreserved ones are bounded by the capacity
