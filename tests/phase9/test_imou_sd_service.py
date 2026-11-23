"""iMou SD playback, camera service: the recording list, play / pause / jump / stop, one stream per camera (playback
wins), shared minutes and cap, no fallback to the live address, no secret in any log. Mock cloud, fake supervisors."""
from __future__ import annotations

import logging
import threading
import time
from datetime import date

import pytest

from tapo_desktop.core.media_engine.supervisor import Frame, StreamState
from tests.fixtures.mock_imou_cloud import MOCK_APP_SECRET, MOCK_RTSP_QUERY, make_record
from tests.phase9.test_imou_service import imou_cfg, make_runtime, service, wait_for

SERIAL = "MOCKSERIAL0001"
CID = f"imou:{SERIAL}"
DAYD = date(2026, 9, 19)
DAY = "2026-09-19"
FPS = 10


def wall(hh, mm=0, ss=0, day=DAY):
    return f"{day} {hh:02d}:{mm:02d}:{ss:02d}"


def rec(b, e, **kw):
    return make_record(b, e, **kw)


def epoch(text):
    from datetime import datetime

    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").timestamp()


class FakeSup:
    instances: list = []

    def __init__(self, **kw):
        self.kw, self.started, self.stopped = kw, False, 0
        FakeSup.instances.append(self)

    def start(self):
        self.started = True

    def stop(self, timeout=3.0):
        self.stopped += 1

    def emit(self, state, reason=None):
        self.kw["on_state"](StreamState[state], reason)

    def frame(self, index):
        self.kw["on_frame"](Frame(self.kw["width"], self.kw["height"], b"", index, time.monotonic()))

    def clip_end(self):
        self.kw["on_clip_end"]()


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    FakeSup.instances = []
    from tapo_desktop.services import imou_playback

    monkeypatch.setattr(imou_playback, "SEEK_DEBOUNCE_S", 0.05)


class Rec:
    def __init__(self):
        self.states, self.positions, self.frames = [], [], []

    def on_frame(self, f):
        self.frames.append(f)

    def on_state(self, state, reason=None):
        self.states.append((state.name, reason))

    def on_position(self, utc):
        self.positions.append(utc)

    def names(self):
        return [s for s, _ in self.states]

    def last(self, name):
        return [r for s, r in self.states if s == name][-1]


def card(cloud, *rows):
    cloud.records[SERIAL] = list(rows) or [rec(wall(8), wall(8, 10)), rec(wall(8, 10, 2), wall(8, 20)),
                                           rec(wall(9), wall(9, 5))]


def listed(svc):
    clips = svc.recordings_for_day(DAYD)
    return clips, [(c.start.timestamp(), c.end.timestamp()) for c in clips]


def play(svc, rec_, start_text, *, segments="auto", end_text=None, clips=None):
    if segments == "auto":
        _, segments = listed(svc) if clips is None else (None, clips)
    end = epoch(end_text) if end_text else epoch(wall(23, 59, 59))
    svc.start_playback(epoch(start_text), end, 640, 360, rec_.on_frame, rec_.on_state, rec_.on_position,
                       segments=segments)


def urls_asked(cloud):
    return [c.params for c in cloud.calls if c.path == "createVideoDownloadStreamUrl"]


@pytest.fixture
def rt(cloud, clock):
    return make_runtime(cloud, clock, factory=FakeSup)


# ================================================================================================ the list
def test_flags_playback_is_on_for_imou_and_everything_else_stays_off():
    from tapo_desktop.services.imou_camera_service import ImouCameraService as S

    assert S.supports_live_view is True and S.supports_playback is True
    for flag in ("supports_ptz", "supports_controls", "supports_events", "supports_rename"):
        assert getattr(S, flag) is False, flag


def test_recordings_for_day_returns_utc_clips_of_the_merged_runs_in_one_call(cloud, rt):
    card(cloud)
    svc = service()
    clips = svc.recordings_for_day(DAYD)
    assert [(c.start.timestamp(), c.end.timestamp()) for c in clips] == [
        (epoch(wall(8)), epoch(wall(8, 20))), (epoch(wall(9)), epoch(wall(9, 5)))]
    assert all(c.start.tzinfo is not None and c.start.utcoffset().total_seconds() == 0 for c in clips)
    calls = [c for c in cloud.calls if c.path == "device.service.invoke"]
    assert len(calls) == 1
    assert calls[0].params["inputData"]["beginTime"] == wall(0) and calls[0].params["inputData"]["endTime"] == wall(23, 59, 59)
    assert calls[0].params["deviceId"] == SERIAL and calls[0].params["channelId"] == "0"


def test_a_recording_that_runs_past_midnight_is_clipped_to_the_pc_day(cloud, rt):
    from tapo_desktop.ui.player_window import local_day_range

    card(cloud, rec(wall(23, 50), wall(0, 20, day="2026-09-20")))
    (clip,) = service().recordings_for_day(DAYD)
    assert clip.start.timestamp() == epoch(wall(23, 50)) and clip.end.timestamp() == local_day_range(DAYD)[1]


def test_an_empty_card_day_is_an_empty_list(cloud, rt):
    card(cloud, rec(wall(8, day="2026-09-18"), wall(8, 10, day="2026-09-18")))
    assert service().recordings_for_day(DAYD) == []


def test_no_card_raises_the_no_card_text_and_asks_only_once(cloud, rt):
    from tapo_desktop.core.imou.errors import ImouNoCardError, is_no_card_text

    cloud.no_card.add(SERIAL)
    with pytest.raises(ImouNoCardError) as err:
        service().recordings_for_day(DAYD)
    assert is_no_card_text(str(err.value)) and cloud.count("device.service.invoke") == 1


def test_list_playback_and_pause_need_a_connection_an_online_camera_and_an_open_guard(cloud, clock):
    from tapo_desktop.core.imou.errors import ImouBlockedError, ImouError, ImouOfflineError

    with pytest.raises(ImouError) as err:
        service().recordings_for_day(DAYD)
    assert "not connected" in str(err.value).lower()
    rt = make_runtime(cloud, clock, factory=FakeSup)
    card(cloud)
    with pytest.raises(ImouOfflineError):
        service(imou_cfg(online=False)).recordings_for_day(DAYD)
    with pytest.raises(ImouOfflineError):
        play(service(imou_cfg(online=False)), Rec(), wall(8), segments=None)
    assert cloud.calls == []
    cloud.fail("accessToken", "FL1004")
    with pytest.raises(Exception):
        rt.client.access_token()
    cloud.calls.clear()
    with pytest.raises(ImouBlockedError):
        service().recordings_for_day(DAYD)
    with pytest.raises(ImouBlockedError):
        play(service(), Rec(), wall(8), segments=None)
    assert cloud.calls == [] and rt.live_ids() == set()


def test_a_pinned_camera_clock_changes_the_day_window_and_the_clip_times(cloud, rt):
    from tapo_desktop.core.imou.clock import ImouClock

    pinned = ImouClock(420)
    cfg = imou_cfg()
    cfg.vendor_data["utc_offset_min"] = 420
    card(cloud, rec(wall(8), wall(8, 10)))
    (clip,) = service(cfg).recordings_for_day(DAYD)
    call = [c for c in cloud.calls if c.path == "device.service.invoke"][0]
    assert call.params["inputData"]["beginTime"] == pinned.day_range(DAYD)[0]
    assert clip.start.timestamp() == pinned.to_utc(wall(8))


def test_the_list_does_not_hold_the_service_lock(cloud, rt):
    svc = service()
    gate, inside = threading.Event(), threading.Event()
    real = rt.client.list_local_records

    def slow(*a, **k):
        inside.set()
        gate.wait(5)
        return real(*a, **k)

    rt.client.list_local_records = slow
    card(cloud)
    worker = threading.Thread(target=svc.recordings_for_day, args=(DAYD,), daemon=True)
    worker.start()
    assert inside.wait(3)
    t0 = time.monotonic()
    svc.stop_stream()
    svc.stop_playback()
    assert time.monotonic() - t0 < 1.0
    gate.set()
    worker.join(5)


def test_a_truncated_list_and_two_clocks_leave_a_note_for_the_panel(cloud, rt):
    from tapo_desktop.core.imou import notes
    from tapo_desktop.core.imou.models import ImouRecordList

    card(cloud, rec(wall(1), wall(1, 10), local_begin=wall(8), local_end=wall(8, 10)))
    service().recordings_for_day(DAYD)
    assert "two clocks" in notes.get(CID).lower()
    rt.client.list_local_records = lambda *a, **k: ImouRecordList(files=[], truncated=True)
    service().recordings_for_day(DAYD)
    assert "first" in notes.get(CID).lower()
    card(cloud)
    del rt.client.list_local_records
    service().recordings_for_day(DAYD)
    assert notes.get(CID) == ""


# ================================================================================================ start
def test_start_asks_for_one_local_record_url_with_the_vendor_strings_and_never_the_live_address(cloud, rt):
    card(cloud)
    svc, r = service(), Rec()
    play(svc, r, wall(8))
    (asked,) = urls_asked(cloud)
    assert (asked["businessType"], asked["streamType"]) == ("localRecord", "0")
    assert (asked["beginTime"], asked["endTime"]) == (wall(8), wall(8, 20))  # the run's own strings
    assert {"bindDeviceLive", "liveList", "unbindLive"}.isdisjoint(cloud.paths())
    (sup,) = FakeSup.instances
    kw = sup.kw
    assert kw["playback"] is True and kw["kind"] == "rtsp" and kw["hwaccel"] == "none" and callable(kw["on_clip_end"])
    assert kw["url"].startswith("rtsp://") and f"starttime={wall(8).replace(' ', 'T')}" in kw["url"]
    assert (kw["width"], kw["height"]) == (640, 360) and sup.started
    assert rt.owner(CID) == "playback" and r.names()[0] == "CONNECTING"


def test_a_position_inside_a_run_is_the_runs_own_string_plus_whole_seconds(cloud, rt):
    card(cloud)
    play(service(), Rec(), wall(8, 3, 7))
    (asked,) = urls_asked(cloud)
    assert (asked["beginTime"], asked["endTime"]) == (wall(8, 3, 7), wall(8, 20))


def test_one_request_covers_at_most_one_hour(cloud, rt):
    card(cloud, rec(wall(8), wall(10)), rec(wall(10, 0, 1), wall(12)))
    play(service(), Rec(), wall(8))
    (asked,) = urls_asked(cloud)
    assert (asked["beginTime"], asked["endTime"]) == (wall(8), wall(9))


def test_a_start_in_a_gap_snaps_to_the_next_run_and_after_the_last_run_it_fails(cloud, rt):
    from tapo_desktop.core.imou.errors import ImouError

    card(cloud)
    play(service(), Rec(), wall(8, 30))
    assert urls_asked(cloud)[0]["beginTime"] == wall(9)
    svc2 = service(imou_cfg("MOCKSERIAL0002", "B"))
    cloud.records["MOCKSERIAL0002"] = list(cloud.records[SERIAL])
    cloud.calls.clear()
    with pytest.raises(ImouError) as err:
        play(svc2, Rec(), wall(10), segments=[(epoch(wall(8)), epoch(wall(8, 20)))])
    assert "no recording at that time" in str(err.value) and cloud.calls == [] and rt.owner("imou:MOCKSERIAL0002") is None


def test_without_clips_the_given_range_is_played(cloud, rt):
    card(cloud)
    play(service(), Rec(), wall(8, 5), segments=None, end_text=wall(8, 9))
    (asked,) = urls_asked(cloud)
    assert (asked["beginTime"], asked["endTime"]) == (wall(8, 5), wall(8, 9))


@pytest.mark.parametrize("code", ["OP1005", "OP1012", "OP1015", "DV1019", "DV1026", "OP1019", "OP1021", "DV1049"])
def test_a_refused_playback_address_never_falls_back_to_the_live_address(cloud, rt, code):
    from tapo_desktop.core.imou.errors import ImouError

    card(cloud)
    cloud.fail("createVideoDownloadStreamUrl", code)
    with pytest.raises(ImouError):
        play(service(), Rec(), wall(8))
    assert {"bindDeviceLive", "liveList", "unbindLive"}.isdisjoint(cloud.paths())
    assert FakeSup.instances == [] and rt.live_ids() == set() and rt.meter.used(CID) == 0


def test_a_malformed_playback_reply_does_not_fall_back_either(cloud, rt, monkeypatch):
    from tapo_desktop.core.imou.errors import ImouError

    card(cloud)
    monkeypatch.setattr(cloud, "_do_createVideoDownloadStreamUrl",
                        lambda token, params: {"code": "0", "msg": "ok", "data": {}})
    with pytest.raises(ImouError):
        play(service(), Rec(), wall(8))
    assert "bindDeviceLive" not in cloud.paths() and rt.live_ids() == set()


# ================================================================================================ position and states
def test_position_is_the_start_plus_frames_over_the_fixed_rate_five_times_a_second(cloud, rt):
    card(cloud)
    r = Rec()
    play(service(), r, wall(8, 0, 30))
    sup = FakeSup.instances[0]
    for i in range(5):
        sup.frame(i)
    start = epoch(wall(8, 0, 30))
    assert len(r.frames) == 5 and r.positions == pytest.approx([start, start + 0.2, start + 0.4])
    sup.frame(100)
    assert r.positions[-1] == pytest.approx(start + 10.0)


def test_states_are_forwarded_and_a_failure_ends_the_run_without_a_retry(cloud, rt):
    card(cloud)
    r = Rec()
    play(service(), r, wall(8))
    sup = FakeSup.instances[0]
    sup.emit("PLAYING")
    sup.frame(0)
    sup.emit("FAILED", f"ffmpeg exited (code 1): {sup.kw['url']}")
    text = r.last("FAILED")
    assert text.startswith("iMou: ") and MOCK_RTSP_QUERY not in text and "rtsp://" not in text
    assert rt.live_ids() == set() and len(urls_asked(cloud)) == 1 and len(FakeSup.instances) == 1


def test_a_failure_before_the_first_frame_is_a_clean_failure(cloud, rt):
    card(cloud)
    r = Rec()
    play(service(), r, wall(8))
    FakeSup.instances[0].emit("FAILED", "no first frame for 30.1 s (stalled)")
    assert "stalled" in r.last("FAILED") and rt.owner(CID) is None


# ================================================================================================ end of a clip (B6)
def test_the_end_of_a_request_inside_a_run_chains_the_next_request(cloud, rt):
    card(cloud, rec(wall(8), wall(10)))
    r = Rec()
    play(service(), r, wall(8))
    first = FakeSup.instances[0]
    first.clip_end()
    assert [(p["beginTime"], p["endTime"]) for p in urls_asked(cloud)] == [(wall(8), wall(9)), (wall(9), wall(10))]
    assert first.stopped >= 1 and len(FakeSup.instances) == 2 and rt.owner(CID) == "playback"
    assert "STOPPED" not in r.names() and "FAILED" not in r.names()


def test_the_end_of_a_run_continues_with_the_next_run_when_clips_were_passed(cloud, rt):
    card(cloud)
    r = Rec()
    play(service(), r, wall(8))
    FakeSup.instances[0].clip_end()
    assert urls_asked(cloud)[-1]["beginTime"] == wall(9) and "STOPPED" not in r.names()


def test_the_end_of_the_last_run_is_end_of_recordings_and_frees_the_slot(cloud, rt):
    card(cloud)
    r = Rec()
    play(service(), r, wall(9))
    FakeSup.instances[0].clip_end()
    assert r.states[-1] == ("STOPPED", "end of recordings") and rt.live_ids() == set()
    assert len(urls_asked(cloud)) == 1


def test_without_clips_the_end_of_the_range_is_end_of_recordings(cloud, rt):
    card(cloud)
    r = Rec()
    play(service(), r, wall(8), segments=None, end_text=wall(8, 5))
    FakeSup.instances[0].clip_end()
    assert r.states[-1] == ("STOPPED", "end of recordings") and len(urls_asked(cloud)) == 1


# ================================================================================================ pause, jump, stop
def test_pause_stops_the_stream_and_resume_asks_for_a_new_url_at_the_position(cloud, clock, rt):
    card(cloud)
    svc, r = service(), Rec()
    play(svc, r, wall(8))
    sup = FakeSup.instances[0]
    for i in range(50):
        sup.frame(i)  # 5 s
    clock.advance(5)
    svc.pause_playback()
    assert sup.stopped == 1 and rt.owner(CID) == "playback" and rt.meter.used(CID) == 5
    clock.advance(600)
    assert rt.meter.used(CID) == 5  # a pause costs no minutes
    assert len(urls_asked(cloud)) == 1
    svc.resume_playback()
    assert wait_for(lambda: len(FakeSup.instances) == 2, 3)
    assert urls_asked(cloud)[-1]["beginTime"] == wall(8, 0, 4) and FakeSup.instances[1].started
    assert rt.owner(CID) == "playback"


def test_pause_and_resume_without_a_session_do_nothing(cloud, rt):
    svc = service()
    svc.pause_playback()
    svc.resume_playback()
    svc.seek_playback(epoch(wall(8)))
    assert cloud.calls == [] and FakeSup.instances == []


def test_a_jump_stops_the_old_stream_at_once_and_starts_one_at_the_new_time(cloud, rt):
    card(cloud)
    svc, r = service(), Rec()
    play(svc, r, wall(8))
    old = FakeSup.instances[0]
    svc.seek_playback(epoch(wall(8, 10, 30)))
    assert old.stopped == 1
    assert wait_for(lambda: len(FakeSup.instances) == 2, 3)
    assert urls_asked(cloud)[-1]["beginTime"] == wall(8, 10, 30) and rt.owner(CID) == "playback"
    old.emit("FAILED", "late")  # a callback of the stopped stream is ignored
    assert "FAILED" not in r.names()


def test_the_last_of_several_quick_jumps_wins_and_makes_one_call(cloud, rt):
    card(cloud)
    svc = service()
    play(svc, Rec(), wall(8))
    for second in (10, 20, 30):
        svc.seek_playback(epoch(wall(8, 5, second)))
    assert wait_for(lambda: len(FakeSup.instances) == 2, 3)
    time.sleep(0.2)
    assert len(urls_asked(cloud)) == 2 and urls_asked(cloud)[-1]["beginTime"] == wall(8, 5, 30)


def test_the_gap_between_two_url_calls_is_one_and_a_half_seconds_by_default():
    import pathlib
    import re

    src = pathlib.Path(__file__).resolve().parents[2] / "src" / "tapo_desktop" / "services" / "imou_playback.py"
    assert re.search(r"^SEEK_DEBOUNCE_S = 1\.5\b", src.read_text(encoding="utf-8"), re.M)


def test_a_jump_into_a_gap_snaps_and_a_jump_after_the_last_run_ends_with_a_text(cloud, rt):
    card(cloud)
    svc, r = service(), Rec()
    play(svc, r, wall(8))
    svc.seek_playback(epoch(wall(8, 40)))
    assert wait_for(lambda: len(FakeSup.instances) == 2, 3) and urls_asked(cloud)[-1]["beginTime"] == wall(9)
    svc.seek_playback(epoch(wall(11)))
    assert wait_for(lambda: "FAILED" in r.names(), 3)
    assert "no recording at that time" in r.last("FAILED") and rt.live_ids() == set()


def test_only_1x_exists(cloud, rt):
    svc = service()
    svc.set_playback_speed("1/1")
    for bad in ("2/1", "4/1", "8/1", "1/2", ""):
        with pytest.raises(ValueError):
            svc.set_playback_speed(bad)
    assert cloud.calls == []


def test_stop_ends_the_stream_counts_the_minutes_and_is_idempotent(cloud, clock, rt):
    card(cloud)
    svc = service()
    play(svc, Rec(), wall(8))
    clock.advance(42)
    svc.stop_playback()
    svc.stop_playback()
    assert FakeSup.instances[0].stopped == 1 and rt.live_ids() == set() and rt.meter.used(CID) == 42


def test_close_stops_playback_too(cloud, rt):
    card(cloud)
    svc = service()
    play(svc, Rec(), wall(8))
    svc.close()
    assert FakeSup.instances[0].stopped == 1 and rt.live_ids() == set()


def test_a_cleared_runtime_ends_cleanly(cloud, rt):
    from tapo_desktop.services import imou_camera_service

    card(cloud)
    svc = service()
    play(svc, Rec(), wall(8))
    imou_camera_service.set_runtime(None)
    svc.stop_playback()
    FakeSup.instances[0].emit("FAILED", "late")  # no exception
    assert FakeSup.instances[0].stopped == 1


# ================================================================================================ cap, minutes, limits
def test_playback_at_the_cap_is_refused_and_no_other_camera_is_displaced(cloud, clock):
    from tapo_desktop.core.imou.errors import ImouError
    from tapo_desktop.core.imou.runtime import ImouLimits

    rt = make_runtime(cloud, clock, factory=FakeSup, limits=ImouLimits(max_live=1))
    card(cloud)
    start_live(service(imou_cfg("MOCKSERIAL0002", "B")), Rec())
    live_sup = FakeSup.instances[0]
    with pytest.raises(ImouError) as err:
        play(service(), Rec(), wall(8))
    assert "at once (limit)" in str(err.value)  # requirement change: the cap text no longer says "Stop one first"
    assert live_sup.stopped == 0 and rt.owner("imou:MOCKSERIAL0002") == "live" and rt.owner(CID) is None
    assert not [p for p in urls_asked(cloud) if p["businessType"] == "localRecord"]


def test_live_minutes_block_playback_and_playback_minutes_block_live(cloud, clock, rt):
    from tapo_desktop.core.imou.errors import ImouError
    from tapo_desktop.models import StreamQuality

    card(cloud)
    rt.meter.add(CID, 1200)
    with pytest.raises(ImouError) as err:
        play(service(), Rec(), wall(8))
    assert "daily" in str(err.value).lower() and urls_asked(cloud) == []
    svc2 = service(imou_cfg("MOCKSERIAL0002", "B"))
    cloud.records["MOCKSERIAL0002"] = list(cloud.records[SERIAL])
    play(svc2, Rec(), wall(8))
    clock.advance(1200)
    svc2.stop_playback()
    with pytest.raises(ImouError):
        svc2.start_stream(StreamQuality.SD, 640, 360, lambda f: None, lambda s, r: None)


def test_the_run_limit_stops_a_forgotten_playback_with_a_text(cloud, clock):
    from tapo_desktop.core.imou.runtime import ImouLimits

    rt = make_runtime(cloud, clock, factory=FakeSup, limits=ImouLimits(idle_seconds=0.2), real_clock=True)
    card(cloud)
    r = Rec()
    play(service(), r, wall(8))
    assert wait_for(lambda: "FAILED" in r.names(), 3)
    assert "playback stopped after" in r.last("FAILED") and "Press Play" in r.last("FAILED")
    assert FakeSup.instances[0].stopped == 1 and rt.live_ids() == set() and rt.meter.used(CID) >= 0.15


def test_the_end_of_the_daily_minutes_stops_playback_with_the_budget_text(cloud, clock):
    from tapo_desktop.core.imou.runtime import ImouLimits

    rt = make_runtime(cloud, clock, factory=FakeSup, limits=ImouLimits(daily_seconds=1200, idle_seconds=600),
                      real_clock=True)
    card(cloud)
    rt.meter.add(CID, 1199.8)
    r = Rec()
    play(service(), r, wall(8))
    assert wait_for(lambda: "FAILED" in r.names(), 3)
    assert "daily" in r.last("FAILED").lower() and rt.live_ids() == set()


# ================================================================================================ one stream per camera (M5)
def start_live(svc, rec_):
    from tapo_desktop.models import StreamQuality

    svc.start_stream(StreamQuality.SD, 640, 360, rec_.on_frame, rec_.on_state)


def test_playback_stops_the_live_stream_keeps_one_slot_and_tells_the_tile_a_fixed_text(cloud, clock, rt):
    from tapo_desktop.core.imou import texts

    card(cloud)
    svc, live_rec, r = service(), Rec(), Rec()
    start_live(svc, live_rec)
    live = FakeSup.instances[0]
    clock.advance(30)
    play(svc, r, wall(8))
    assert live.stopped == 1 and rt.owner(CID) == "playback" and rt.live_ids() == {CID}
    assert live_rec.states[-1] == ("FAILED", texts.PLAYING_TEXT) and not texts.PLAYING_TEXT.startswith("iMou: ")
    assert rt.meter.used(CID) == 30


def test_a_grid_style_stop_of_the_live_stream_does_not_free_the_playback_slot(cloud, rt):
    card(cloud)
    svc = service()
    start_live(svc, Rec())
    play(svc, Rec(), wall(8))
    svc.stop_stream()
    svc.stop_stream()
    assert rt.owner(CID) == "playback" and FakeSup.instances[1].stopped == 0


def test_a_click_on_the_tile_during_playback_contacts_nothing(cloud, rt):
    from tapo_desktop.core.imou import texts

    card(cloud)
    svc = service()
    play(svc, Rec(), wall(8))
    before = len(cloud.calls)
    with pytest.raises(Exception) as err:
        start_live(svc, Rec())
    assert str(err.value) == texts.PLAYING_TEXT and len(cloud.calls) == before and len(FakeSup.instances) == 1
    assert rt.owner(CID) == "playback"


def test_the_end_of_playback_does_not_start_live_again_but_asks_for_a_click(cloud, rt):
    from tapo_desktop.core.imou import texts

    # requirement change: the tile no longer keeps the stale "Playing a recording" text; it says "click to start" once
    card(cloud)
    svc, live_rec = service(), Rec()
    start_live(svc, live_rec)
    play(svc, Rec(), wall(9))
    FakeSup.instances[1].clip_end()
    assert rt.live_ids() == set() and len(FakeSup.instances) == 2 and live_rec.states[-1] == ("FAILED", texts.CLICK_TEXT)
    assert [p["businessType"] for p in urls_asked(cloud)] == ["real", "localRecord"]


def test_playback_of_another_camera_does_not_touch_a_live_stream_under_the_cap(cloud, rt):
    card(cloud)
    cloud.records["MOCKSERIAL0002"] = list(cloud.records[SERIAL])
    b = service(imou_cfg("MOCKSERIAL0002", "B"))
    start_live(b, Rec())
    play(service(), Rec(), wall(8))
    assert FakeSup.instances[0].stopped == 0 and rt.live_ids() == {CID, "imou:MOCKSERIAL0002"}
    start_c = service(imou_cfg("MOCKSERIAL0003", "C"))
    from tapo_desktop.core.imou.errors import ImouError

    with pytest.raises(ImouError):
        start_live(start_c, Rec())


# ================================================================================================ the rest
def test_downloads_and_the_day_calendar_stay_unsupported(cloud, rt):
    import pathlib

    from tapo_desktop.models import RecordingClip
    from tapo_desktop.services.imou_camera_service import ImouUnsupportedError

    svc = service()
    with pytest.raises(ImouUnsupportedError):
        svc.download_recording(RecordingClip(None, None), pathlib.Path("x.mp4"))
    with pytest.raises(ImouUnsupportedError):
        svc.days_with_recordings(DAYD, DAYD)
    assert cloud.calls == []


def test_nothing_is_called_before_a_list_or_a_play(cloud, rt):
    svc = service()
    svc.connect()
    svc.start_events(lambda e: None)
    svc.stop_events()
    svc.stop_stream()
    svc.stop_playback()
    assert cloud.calls == []


def test_no_log_record_holds_a_token_secret_url_or_file_name(cloud, rt, caplog):
    caplog.set_level(logging.DEBUG)
    card(cloud, rec(wall(8), wall(10), name="secret-card-file-name"))
    svc, r = service(), Rec()
    play(svc, r, wall(8))
    sup = FakeSup.instances[0]
    sup.frame(0)
    sup.clip_end()
    svc.seek_playback(epoch(wall(8, 30)))
    wait_for(lambda: len(FakeSup.instances) >= 3, 3)
    svc.pause_playback()
    svc.resume_playback()
    wait_for(lambda: len(FakeSup.instances) >= 4, 3)
    sup.emit("FAILED", f"x {sup.kw['url']}")
    svc.stop_playback()
    text = "\n".join(rr.getMessage() for rr in caplog.records) + repr(r.states)
    for secret in (MOCK_APP_SECRET, MOCK_RTSP_QUERY, "secret-card-file-name", cloud.last_issued_token(), "starttime="):
        assert secret not in text, secret


def test_removing_the_camera_through_the_controller_stops_playback_and_frees_the_slot(cloud, rt, tmp_path, qtbot):
    """The real controller wiring: start_playback on its playback strand, remove_cameras (disconnect, sign-out)."""
    from tapo_desktop.services.config_store import ConfigStore
    from tapo_desktop.services.controller import AppController
    from tests.e2e.fakes import FakeVault

    card(cloud)
    store = ConfigStore(tmp_path / "config.json")
    store.save([imou_cfg()])
    controller = AppController(store, FakeVault())
    try:
        clips = service().recordings_for_day(DAYD)
        controller.start_playback(CID, epoch(wall(8)), epoch(wall(23, 59, 59)), clips=clips)
        assert wait_for(lambda: bool(FakeSup.instances) and FakeSup.instances[0].started, 5)
        assert rt.owner(CID) == "playback"
        controller.remove_cameras([CID])
        assert wait_for(lambda: FakeSup.instances[0].stopped >= 1 and rt.live_ids() == set(), 5)
    finally:
        controller.stop_all()
