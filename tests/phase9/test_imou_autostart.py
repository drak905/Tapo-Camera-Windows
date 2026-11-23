"""iMou live view starts by itself (service side): Kami-style retries kept in the iMou service, the 60-second play rule,
the daily cap of automatic starts, the daily minutes, the cap of live views, the online state from the runtime, the
click text after a recording. Mock cloud, fake supervisors, no network."""
from __future__ import annotations

import inspect
import logging
import re
import threading
import time

import pytest

from tapo_desktop.core.imou.errors import ImouError
from tapo_desktop.core.imou.runtime import ImouLimits
from tapo_desktop.models import StreamQuality
from tests.fixtures.mock_imou_cloud import MOCK_APP_SECRET, MOCK_RTSP_QUERY, make_device
from tests.phase9.test_imou_service import FakeSup, Rec, imou_cfg, make_runtime, service, wait_for

CID = "imou:MOCKSERIAL0001"
DELAY = 0.12  # the automatic retry delay used by these tests (30 s in the app)
QUICK = 0.05  # the restart after a clean end of a long run (2 s in the app)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    from tapo_desktop.services import imou_camera_service

    FakeSup.instances = []
    monkeypatch.setattr(imou_camera_service, "AUTO_RETRY_DELAY_S", DELAY, raising=False)
    monkeypatch.setattr(imou_camera_service, "AUTO_QUICK_DELAY_S", QUICK, raising=False)


def auto(svc, rec, quality=StreamQuality.SD, size=(640, 360)):
    """What the grid does for a visible tile: an automatic start."""
    svc.start_stream(quality, *size, rec.on_frame, rec.on_state)


def click(svc, rec):
    """What a click on a waiting tile does: the controller passes preempt=True."""
    svc.start_stream(StreamQuality.SD, 640, 360, rec.on_frame, rec.on_state, preempt=True)


def last_failed(rec):
    return rec.reasons("FAILED")[-1]


def play_for(clock, seconds, state="FAILED", reason="ffmpeg exited (code 1)"):
    """The newest fake stream reaches PLAYING, runs ``seconds`` of runtime time, then ends."""
    sup = FakeSup.instances[-1]
    sup.emit("PLAYING")
    clock.advance(seconds)
    sup.emit(state, reason)


def test_the_defaults():
    from tapo_desktop.services import imou_camera_service as mod

    assert (mod.AUTO_RETRY_MAX, mod.STABLE_PLAY_S, mod.AUTO_STARTS_PER_DAY) == (3, 60.0, 12)


def test_the_real_delays_are_thirty_and_two_seconds(monkeypatch):
    from tapo_desktop.services import imou_camera_service as mod

    monkeypatch.undo()  # the autouse fixture shortened them for the other tests
    assert (mod.AUTO_RETRY_DELAY_S, mod.AUTO_QUICK_DELAY_S) == (30.0, 2.0)


def test_start_stream_takes_the_controllers_preempt_keyword_and_is_off_by_default():
    from tapo_desktop.services.imou_camera_service import ImouCameraService

    param = inspect.signature(ImouCameraService.start_stream).parameters["preempt"]
    assert param.default is False and param.kind is inspect.Parameter.KEYWORD_ONLY


# ================================================================================================ retries (M5, B2)
def test_a_lost_stream_of_an_automatic_start_is_retried_three_times_then_waits_for_a_click(cloud, clock):
    rt = make_runtime(cloud, clock)
    svc, rec = service(), Rec()
    auto(svc, rec)
    for n in range(3):
        FakeSup.instances[-1].emit("FAILED", "no first frame for 30.0 s (stalled)")
        assert "trying again" in last_failed(rec).lower() and last_failed(rec).startswith("iMou: ")
        assert rt.live_ids() == set()
        assert wait_for(lambda n=n: len(FakeSup.instances) == n + 2, 3)  # a fresh address and a fresh stream
    FakeSup.instances[-1].emit("FAILED", "no first frame for 30.0 s (stalled)")
    assert "trying again" not in last_failed(rec).lower() and last_failed(rec).startswith("iMou: ")
    time.sleep(DELAY * 4)
    assert len(FakeSup.instances) == 4 and rt.live_ids() == set()
    click(svc, rec)  # the click starts at once and a new streak begins
    assert len(FakeSup.instances) == 5


def test_a_view_change_while_a_retry_is_pending_shows_the_text_and_starts_nothing(cloud, clock):
    make_runtime(cloud, clock)
    svc, rec = service(), Rec()
    auto(svc, rec)
    FakeSup.instances[0].emit("FAILED", "ffmpeg exited (code 1)")
    again = Rec()
    auto(svc, again)  # the grid asks again (quality switch, resize): not before the retry is due
    assert "trying again" in again.reasons("FAILED")[-1].lower() and len(FakeSup.instances) == 1
    assert wait_for(lambda: len(FakeSup.instances) == 2, 3)


def test_streams_that_play_less_than_sixty_seconds_do_not_reset_the_retry_count(cloud, clock):
    make_runtime(cloud, clock)
    svc, rec = service(), Rec()
    auto(svc, rec)
    for n in range(3):
        play_for(clock, 59)
        assert wait_for(lambda n=n: len(FakeSup.instances) == n + 2, 3)
    play_for(clock, 59)
    time.sleep(DELAY * 4)
    assert len(FakeSup.instances) == 4 and "trying again" not in last_failed(rec).lower()


def test_a_stream_that_played_sixty_seconds_unbroken_starts_a_new_streak(cloud, clock):
    make_runtime(cloud, clock)
    svc, rec = service(), Rec()
    auto(svc, rec)
    for n in range(6):  # more failures than the three retries: each came after a good run
        play_for(clock, 61)
        assert wait_for(lambda n=n: len(FakeSup.instances) == n + 2, 3)


def test_a_clean_end_after_a_minute_restarts_once_quickly_and_is_not_one_of_the_three_retries(cloud, clock):
    make_runtime(cloud, clock)
    svc, rec = service(), Rec()
    auto(svc, rec)
    play_for(clock, 61, reason="the stream ended")  # the address probably expired
    assert wait_for(lambda: len(FakeSup.instances) == 2, 3)
    for n in range(3):  # all three retries are still there
        FakeSup.instances[-1].emit("FAILED", "ffmpeg exited (code 1)")
        assert wait_for(lambda n=n: len(FakeSup.instances) == n + 3, 3)
    FakeSup.instances[-1].emit("FAILED", "ffmpeg exited (code 1)")
    time.sleep(DELAY * 4)
    assert len(FakeSup.instances) == 5


def test_a_clean_end_before_a_minute_is_a_normal_retry(cloud, clock):
    make_runtime(cloud, clock)
    svc, rec = service(), Rec()
    auto(svc, rec)
    for n in range(3):
        play_for(clock, 10, reason="the stream ended")
        assert wait_for(lambda n=n: len(FakeSup.instances) == n + 2, 3)
    play_for(clock, 10, reason="the stream ended")
    time.sleep(DELAY * 4)
    assert len(FakeSup.instances) == 4


def test_twelve_automatic_starts_a_day_then_a_text_that_waits_for_a_click(cloud, clock):
    make_runtime(cloud, clock)
    svc, rec = service(), Rec()
    auto(svc, rec)
    for n in range(12):  # each long run ends cleanly: a quick restart, counted in the day but not in the retries
        play_for(clock, 61, reason="the stream ended")
        if n < 11:
            assert wait_for(lambda n=n: len(FakeSup.instances) == n + 2, 3)
    time.sleep(DELAY * 3)
    assert len(FakeSup.instances) == 12
    assert last_failed(rec) == "iMou: too many automatic restarts today. Click the tile to try again."
    shown = Rec()
    auto(svc, shown)  # a view change: the same text, nothing sent
    assert shown.reasons("FAILED")[-1] == last_failed(rec) and len(FakeSup.instances) == 12
    click(svc, rec)  # a click is not counted
    assert len(FakeSup.instances) == 13
    svc.stop_stream()
    clock.advance(86_400)  # the next UTC day: a new count
    auto(svc, Rec())
    assert len(FakeSup.instances) == 14


def test_a_start_that_came_from_a_click_is_never_retried_by_itself(cloud, clock):
    make_runtime(cloud, clock)
    svc, rec = service(), Rec()
    click(svc, rec)
    FakeSup.instances[0].emit("FAILED", "ffmpeg exited (code 1)")
    time.sleep(DELAY * 4)
    assert len(FakeSup.instances) == 1 and "trying again" not in last_failed(rec).lower()


def test_a_network_failure_of_an_automatic_start_is_reported_and_retried_after_the_delay(cloud, clock):
    flaky = {"left": 1}
    real = cloud.post

    def post(url, body, timeout=30.0):
        if flaky["left"] > 0:
            flaky["left"] -= 1
            raise ConnectionResetError("boom")
        return real(url, body, timeout)

    cloud.post = post
    make_runtime(cloud, clock)
    svc, rec = service(), Rec()
    auto(svc, rec)  # does not raise: the failure is shown and a retry is planned
    assert "cannot reach the iMou servers" in last_failed(rec) and "trying again" in last_failed(rec).lower()
    assert FakeSup.instances == []
    assert wait_for(lambda: len(FakeSup.instances) == 1, 3)


@pytest.mark.parametrize("code", ["DV1009", "DV2102", "DV2103", "DV2104"])
def test_busy_and_no_answer_codes_are_retried(cloud, clock, code):
    make_runtime(cloud, clock)
    cloud.fail("createVideoDownloadStreamUrl", code, "words", times=1)
    svc, rec = service(), Rec()
    auto(svc, rec)
    assert "trying again" in last_failed(rec).lower()
    assert wait_for(lambda: len(FakeSup.instances) == 1, 3)


@pytest.mark.parametrize("code", ["FL1002", "FL1004", "FL1005", "OP1013", "OP1014", "OP1040", "OP1039", "SN1001",
                                  "DV1007", "LV1008", "OP1009"])
def test_refusals_are_never_retried_and_are_not_asked_again_by_a_view_change(cloud, clock, code):
    make_runtime(cloud, clock)
    path = "accessToken" if code == "SN1001" else "createVideoDownloadStreamUrl"
    cloud.fail(path, code, "words", times=None)
    svc, rec = service(), Rec()
    with pytest.raises(ImouError):
        auto(svc, rec)
    calls = len(cloud.calls)
    shown = Rec()
    auto(svc, shown)  # the grid asks again (a resize, a view change): the same text, nothing sent
    assert shown.reasons("FAILED") and shown.reasons("FAILED")[-1].startswith("iMou: ")
    time.sleep(DELAY * 4)
    assert len(cloud.calls) == calls and FakeSup.instances == []


def test_a_click_asks_again_after_a_refusal(cloud, clock):
    make_runtime(cloud, clock)
    cloud.fail("createVideoDownloadStreamUrl", "LV1008", "words", times=1)
    svc, rec = service(), Rec()
    with pytest.raises(ImouError):
        auto(svc, rec)
    click(svc, rec)
    assert len(FakeSup.instances) == 1 and FakeSup.instances[0].started


def test_the_memory_survives_a_rebuilt_service_object(cloud, clock):
    make_runtime(cloud, clock)
    cloud.fail("createVideoDownloadStreamUrl", "LV1008", "words", times=None)
    with pytest.raises(ImouError):
        auto(service(), Rec())
    calls = len(cloud.calls)
    shown = Rec()
    auto(service(), shown)  # the controller made a new service object for the same camera
    assert shown.reasons("FAILED") and len(cloud.calls) == calls


def test_hiding_the_tile_cancels_a_pending_retry(cloud, clock):
    make_runtime(cloud, clock)
    svc, rec = service(), Rec()
    auto(svc, rec)
    FakeSup.instances[0].emit("FAILED", "ffmpeg exited (code 1)")
    svc.stop_stream()  # the tile left the view
    time.sleep(DELAY * 4)
    assert len(FakeSup.instances) == 1


def test_a_refused_tile_that_is_hidden_and_shown_again_does_not_ask_again(cloud, clock):
    make_runtime(cloud, clock)
    cloud.fail("createVideoDownloadStreamUrl", "LV1008", "words", times=None)
    svc = service()
    with pytest.raises(ImouError):
        auto(svc, Rec())
    svc.stop_stream()
    calls = len(cloud.calls)
    shown = Rec()
    auto(svc, shown)
    assert shown.reasons("FAILED") and len(cloud.calls) == calls


def test_retries_log_no_url_or_secret(cloud, clock, caplog):
    caplog.set_level(logging.DEBUG)
    make_runtime(cloud, clock)
    svc, rec = service(), Rec()
    auto(svc, rec)
    FakeSup.instances[0].emit("FAILED", f"boom {FakeSup.instances[0].kw['url']}")
    assert wait_for(lambda: len(FakeSup.instances) == 2, 3)
    svc.stop_stream()
    text = "\n".join(r.getMessage() for r in caplog.records) + last_failed(rec)
    for secret in (MOCK_RTSP_QUERY, MOCK_APP_SECRET, cloud.last_issued_token()):
        assert secret not in text


# ================================================================================================ daily minutes (M4)
def test_the_budget_text_says_when_it_starts_again_in_local_time_and_nothing_is_sent_until_a_click(cloud, clock):
    rt = make_runtime(cloud, clock)
    rt.meter.add(CID, 1200)
    svc, rec = service(), Rec()
    with pytest.raises(ImouError) as err:
        auto(svc, rec)
    text = str(err.value)
    assert re.fullmatch(r"iMou: the daily live time for this camera \(20 minutes\) is used\. It starts again after "
                        r"\d\d:\d\d \(00:00 UTC\) when you click the tile, or raise the limit in the iMou connection "
                        r"window\.", text)
    again = Rec()
    auto(svc, again)  # a view change: the same text, nothing sent
    assert again.reasons("FAILED")[-1] == text
    time.sleep(DELAY * 3)
    assert cloud.calls == [] and FakeSup.instances == [] and svc._retry_timer is None  # no midnight timer


def test_a_click_before_midnight_contacts_nothing_and_a_start_after_it_works(cloud, clock):
    rt = make_runtime(cloud, clock)
    rt.meter.add(CID, 1200)
    svc = service()
    with pytest.raises(ImouError):
        auto(svc, Rec())
    with pytest.raises(ImouError):
        click(svc, Rec())
    assert cloud.calls == [] and FakeSup.instances == []
    clock.advance(86_400)  # the next UTC day: the meter is empty again
    click(svc, Rec())
    assert len(FakeSup.instances) == 1 and FakeSup.instances[0].started


def test_a_running_stream_that_uses_up_the_minutes_stops_with_the_budget_text_and_waits(cloud, clock):
    rt = make_runtime(cloud, clock, limits=ImouLimits(daily_seconds=0.3), real_clock=True)
    svc, rec = service(), Rec()
    auto(svc, rec)
    assert wait_for(lambda: "FAILED" in rec.names(), 3)
    assert "the daily live time for this camera" in last_failed(rec) and "(00:00 UTC)" in last_failed(rec)
    assert FakeSup.instances[0].stopped == 1 and rt.live_ids() == set()
    calls = len(cloud.calls)
    auto(svc, Rec())  # a view change does not ask the cloud
    time.sleep(DELAY * 3)
    assert len(cloud.calls) == calls and len(FakeSup.instances) == 1 and svc._retry_timer is None


def test_the_live_timer_ends_at_the_remaining_minutes_and_not_before(cloud, clock):
    make_runtime(cloud, clock, limits=ImouLimits(daily_seconds=1200, idle_seconds=0.2), real_clock=True)
    svc, rec = service(), Rec()
    auto(svc, rec)
    time.sleep(0.6)
    assert "FAILED" not in rec.names() and FakeSup.instances[0].stopped == 0


# ================================================================================================ cap, connection, online state
def test_a_tile_over_the_cap_gets_the_cap_text_and_waits_for_a_click(cloud, clock):
    cloud.devices.append(make_device("MOCKSERIAL0003"))
    rt = make_runtime(cloud, clock)
    a, b, c = (service(imou_cfg(s)) for s in ("MOCKSERIAL0001", "MOCKSERIAL0002", "MOCKSERIAL0003"))
    auto(a, Rec())
    auto(b, Rec())
    calls = len(cloud.calls)
    with pytest.raises(ImouError) as err:
        auto(c, Rec())
    assert str(err.value) == ("iMou: 2 live views at once (limit). Hide a tile or raise the limit in the iMou "
                              "connection window.")
    assert len(cloud.calls) == calls
    a.stop_stream()
    time.sleep(DELAY * 3)
    assert len(FakeSup.instances) == 2  # nothing queues: the waiting tile starts on a click
    click(c, Rec())
    assert rt.live_ids() == {"imou:MOCKSERIAL0002", "imou:MOCKSERIAL0003"}


def test_a_start_without_a_connection_says_so_sends_nothing_and_does_not_start_later(cloud, clock):
    svc, rec = service(), Rec()
    with pytest.raises(ImouError) as err:
        auto(svc, rec)
    assert "not connected" in str(err.value)
    make_runtime(cloud, clock)
    time.sleep(DELAY * 3)
    assert cloud.calls == [] and FakeSup.instances == []
    click(svc, Rec())
    assert len(FakeSup.instances) == 1


def test_the_online_state_comes_from_the_runtime_once_a_refresh_filled_it(cloud, clock):
    rt = make_runtime(cloud, clock)
    svc = service(imou_cfg(online=False))  # the config the service was built with says offline
    with pytest.raises(ImouError):
        auto(svc, Rec())
    assert cloud.calls == []
    rt.set_online({CID: True})  # a Refresh found the camera online
    click(svc, Rec())
    assert len(FakeSup.instances) == 1


def test_a_runtime_that_says_offline_beats_a_config_that_says_online(cloud, clock):
    rt = make_runtime(cloud, clock)
    rt.set_online({CID: False})
    with pytest.raises(ImouError):
        click(service(), Rec())
    assert cloud.calls == [] and FakeSup.instances == []


def test_two_tiles_starting_together_ask_for_one_token(cloud, clock):
    make_runtime(cloud, clock)
    a, b = service(imou_cfg("MOCKSERIAL0001")), service(imou_cfg("MOCKSERIAL0002"))
    real = cloud.post

    def slow(url, body, timeout=30.0):
        time.sleep(0.1)
        return real(url, body, timeout)

    from tapo_desktop.services import imou_camera_service

    imou_camera_service.get_runtime().client._post = slow
    threads = [threading.Thread(target=auto, args=(s, Rec())) for s in (a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert cloud.count("accessToken") == 1 and len(FakeSup.instances) == 2


# ================================================================================================ after a recording (M3)
def sd_env(cloud, clock):
    from tests.phase9 import test_imou_sd_service as sd

    sd.FakeSup.instances = []
    rt = make_runtime(cloud, clock, factory=sd.FakeSup)
    sd.card(cloud)
    return sd, rt


def live(svc, rec, preempt=False):
    svc.start_stream(StreamQuality.SD, 640, 360, rec.on_frame, rec.on_state, preempt=preempt)


def test_after_a_recording_the_tile_says_click_to_start_once_and_nothing_starts_by_itself(cloud, clock):
    from tapo_desktop.core.imou import texts

    sd, rt = sd_env(cloud, clock)
    svc, tile = service(), sd.Rec()
    live(svc, tile)
    sd.play(svc, sd.Rec(), sd.wall(9))
    assert tile.states[-1] == ("FAILED", texts.PLAYING_TEXT)
    svc.stop_playback()  # the player window closed
    assert tile.states[-1] == ("FAILED", texts.CLICK_TEXT)
    time.sleep(DELAY * 3)
    assert rt.live_ids() == set() and len(sd.FakeSup.instances) == 2 and [s for s in tile.states].count(
        ("FAILED", texts.CLICK_TEXT)) == 1
    live(svc, tile, preempt=True)  # the click
    assert rt.owner(sd.CID) == "live" and len(sd.FakeSup.instances) == 3


def test_a_tile_that_left_the_view_gets_no_click_text_after_the_recording(cloud, clock):
    sd, rt = sd_env(cloud, clock)
    svc, tile = service(), sd.Rec()
    live(svc, tile)
    sd.play(svc, sd.Rec(), sd.wall(9))
    svc.stop_stream()  # the grid hid the tile
    n = len(tile.states)
    svc.stop_playback()
    assert len(tile.states) == n


def test_after_a_recording_that_used_the_last_minutes_the_tile_shows_the_budget_text(cloud, clock):
    sd, rt = sd_env(cloud, clock)
    svc, tile = service(), sd.Rec()
    live(svc, tile)
    sd.play(svc, sd.Rec(), sd.wall(9))
    rt.meter.add(sd.CID, 1200)
    calls = len(cloud.calls)
    svc.stop_playback()
    assert "the daily live time for this camera" in str(tile.states[-1][1]) and len(cloud.calls) == calls


def test_a_pending_retry_that_comes_due_during_a_recording_does_nothing(cloud, clock):
    sd, rt = sd_env(cloud, clock)
    svc = service()
    live(svc, sd.Rec())
    sd.FakeSup.instances[0].emit("FAILED", "ffmpeg exited (code 1)")  # a retry is pending
    sd.play(svc, sd.Rec(), sd.wall(9))
    time.sleep(DELAY * 4)
    kinds = [c.params.get("businessType") for c in cloud.calls if c.path == "createVideoDownloadStreamUrl"]
    assert kinds == ["real", "localRecord"] and rt.owner(sd.CID) == "playback"


def test_the_playing_text_is_never_a_failure_that_waits(cloud, clock):
    sd, rt = sd_env(cloud, clock)
    svc = service()
    live(svc, sd.Rec())
    sd.play(svc, sd.Rec(), sd.wall(9))
    svc.stop_playback()
    shown = sd.Rec()
    live(svc, shown)  # an automatic start after the recording: nothing remembered, so it starts
    assert rt.owner(sd.CID) == "live" and "CONNECTING" in shown.names()
