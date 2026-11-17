"""services.view_layout.ViewLayout: the ordered grid slot list behind setting "grid_slots" (pure, no Qt)."""
from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tapo_desktop.services.view_layout import MAX_SLOTS, SETTING_KEY, ViewLayout, default_slots, parse_slots

pytestmark = pytest.mark.unit

IDS = ["a", "b", "c", "d", "e"]


def _pad(*items):
    return list(items) + [None] * (MAX_SLOTS - len(items))


def test_constants():
    assert MAX_SLOTS == 25
    assert SETTING_KEY == "grid_slots"


def test_default_slots_are_cameras_in_list_order():
    assert default_slots(IDS) == _pad(*IDS)
    many = [f"c{i}" for i in range(30)]
    assert default_slots(many) == many[:25]  # more cameras than slots: the rest are not in the view
    assert ViewLayout(IDS).slots() == _pad(*IDS)
    assert ViewLayout(IDS, None).slots() == _pad(*IDS)


@pytest.mark.parametrize("garbage", ["a,b", 42, {"0": "a"}, b"ab", 3.5, True, ("a", "b")])
def test_non_list_setting_falls_back_to_default(garbage):
    assert parse_slots(garbage, IDS) is None
    assert ViewLayout(IDS, garbage).slots() == _pad(*IDS)


def test_parse_drops_unknown_duplicate_and_non_string_entries():
    saved = ["c", "ghost", None, 7, "a", "c", {"x": 1}, "b"]
    assert parse_slots(saved, IDS) == _pad("c", None, None, None, "a", None, None, "b")


def test_parse_pads_and_truncates_to_max_slots():
    assert parse_slots([], IDS) == [None] * MAX_SLOTS
    long = [None] * 40 + ["a"]
    assert parse_slots(long, IDS) == [None] * MAX_SLOTS


def test_saved_layout_is_kept_and_unlisted_cameras_stay_hidden():
    lay = ViewLayout(IDS, ["b", None, "a"])
    assert lay.slots() == _pad("b", None, "a")
    assert lay.index_of("c") is None  # known at start: not "new", so not auto-added
    assert lay.visible(4) == ["b", None, "a", None]
    assert lay.visible(1) == ["b"]


def test_all_empty_saved_layout_is_respected():
    lay = ViewLayout(IDS, [None] * 25)
    assert lay.slots() == [None] * 25


def test_place_into_empty_slot_moves_camera():
    lay = ViewLayout(IDS)
    assert lay.place(7, "a") is True
    assert lay.slots()[:8] == [None, "b", "c", "d", "e", None, None, "a"]
    assert lay.place(7, "a") is False  # already there


def test_place_over_camera_swaps_the_two():
    lay = ViewLayout(IDS)
    assert lay.place(0, "c") is True
    assert lay.slots()[:5] == ["c", "b", "a", "d", "e"]


def test_place_hidden_camera_replaces_and_unknown_raises():
    lay = ViewLayout(["a", "b", "x"], ["a", "b"])
    assert lay.place(1, "x") is True
    assert lay.slots()[:2] == ["a", "x"]
    assert lay.index_of("b") is None
    with pytest.raises(KeyError):
        lay.place(0, "nope")
    with pytest.raises(IndexError):
        lay.place(25, "a")


def test_swap_clear_remove():
    lay = ViewLayout(IDS)
    assert lay.swap(0, 4) is True
    assert lay.slots()[:5] == ["e", "b", "c", "d", "a"]
    assert lay.swap(2, 2) is False
    assert lay.clear(1) is True and lay.clear(1) is False
    assert lay.remove("e") is True and lay.remove("e") is False
    assert lay.slots()[:5] == [None, None, "c", "d", "a"]


def test_add_uses_first_empty_visible_slot_then_fallback():
    lay = ViewLayout(IDS, ["a", None, "b", "c"])
    assert lay.add("d", 4) == 1
    assert lay.visible(4) == ["a", "d", "b", "c"]
    assert lay.add("d", 4) == 1  # already visible: unchanged
    assert lay.add("e", 4, fallback=2) == 2  # full: replaces the given (selected) slot
    assert lay.visible(4) == ["a", "d", "e", "c"]
    assert lay.index_of("b") is None
    assert lay.add("b", 4) == 3  # full, no fallback: last visible slot
    assert lay.visible(4) == ["a", "d", "e", "b"]


def test_add_camera_from_hidden_slot_swaps():
    lay = ViewLayout(IDS)  # a..e in slots 0..4; a 2x2 grid shows slots 0..3
    assert lay.add("e", 4, fallback=0) == 0
    assert lay.slots()[:5] == ["e", "b", "c", "d", "a"]


def test_sync_new_cameras_fill_first_empty_slot_removed_leave_gap():
    lay = ViewLayout(IDS, ["a", None, "b", "c"])
    changed = lay.sync(["a", "b", "c", "d", "e", "f"])
    assert changed is True
    assert lay.slots()[:4] == ["a", "f", "b", "c"]  # f is new; d/e were known and hidden
    assert lay.sync(["a", "c", "d", "e", "f"]) is True  # b removed
    assert lay.slots()[:4] == ["a", "f", None, "c"]
    assert lay.sync(["a", "c", "d", "e", "f"]) is False


def test_sync_new_camera_without_free_slot_is_not_added():
    ids = [f"c{i}" for i in range(25)]
    lay = ViewLayout(ids)
    assert lay.sync(ids + ["new"]) is False
    assert lay.index_of("new") is None


def test_camera_removed_and_readded_is_new_again():
    lay = ViewLayout(["a", "b"], ["a", None])
    lay.sync(["a"])
    lay.sync(["a", "b"])
    assert lay.slots()[:2] == ["a", "b"]


def test_removed_from_view_camera_is_not_re_added_by_sync():
    lay = ViewLayout(IDS)
    lay.remove("b")
    assert lay.sync(IDS) is False
    assert lay.index_of("b") is None


def test_to_setting_is_a_json_ready_copy():
    lay = ViewLayout(IDS)
    out = lay.to_setting()
    assert out == _pad(*IDS) and len(out) == 25
    out[0] = "zzz"
    assert lay.slots()[0] == "a"


_slot_item = st.one_of(st.none(), st.sampled_from(IDS + ["ghost"]), st.integers(), st.text(max_size=3))


@settings(max_examples=150, deadline=None)
@given(saved=st.one_of(st.none(), st.lists(_slot_item, max_size=40), st.integers(), st.text()),
       ops=st.lists(st.tuples(st.sampled_from(["place", "swap", "clear", "remove", "add", "sync"]),
                              st.integers(0, 24), st.integers(0, 24), st.sampled_from(IDS)), max_size=20))
def test_invariants_hold_for_any_setting_and_operations(saved, ops):
    cams = list(IDS)
    lay = ViewLayout(cams, saved)
    for op, i, j, cid in ops:
        if op == "place" and cid in cams:
            lay.place(i, cid)
        elif op == "swap":
            lay.swap(i, j)
        elif op == "clear":
            lay.clear(i)
        elif op == "remove":
            lay.remove(cid)
        elif op == "add" and cid in cams:
            lay.add(cid, max(1, (j % 5 + 1) ** 2), fallback=i)
        elif op == "sync":
            cams = [c for c in IDS if c != cid] if cid in cams else cams + [cid]
            lay.sync(cams)
        slots = lay.slots()
        assert len(slots) == MAX_SLOTS
        present = [s for s in slots if s is not None]
        assert len(present) == len(set(present)), "a camera must never be in two slots"
        assert all(s in cams for s in present), "only existing cameras may be in a slot"
