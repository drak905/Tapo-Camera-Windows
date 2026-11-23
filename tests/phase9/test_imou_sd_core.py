"""iMou SD playback, core: the camera clock, record rows and runs, ``record.file.list`` / ``localRecord`` requests,
the second (serviceId) allowlist, the error map and the call guard. Mock cloud only; made-up serials and times."""
from __future__ import annotations

import calendar
import logging
import pathlib
import re
from datetime import date, datetime, timedelta

import pytest

from tests.fixtures.mock_imou_cloud import MOCK_APP_SECRET, MOCK_RTSP_QUERY, make_record
from tests.phase9.test_imou_core import make_client

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "tapo_desktop"
SERIAL = "MOCKSERIAL0001"
DAY = "2026-09-19"
INVOKE = "/openapi/v2/device.service.invoke"


def wall(hh: int, mm: int = 0, ss: int = 0, day: str = DAY) -> str:
    return f"{day} {hh:02d}:{mm:02d}:{ss:02d}"


def rec(b: str, e: str, **kw) -> dict:
    return make_record(b, e, **kw)


def utc_of(text: str) -> float:
    """The epoch of a wall-clock text read in the PC's own zone (the default camera clock)."""
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").timestamp()


# ============================================================================================ the camera clock (B4)
def test_default_clock_is_the_pc_zone_and_round_trips():
    from tapo_desktop.core.imou.clock import ImouClock

    clock = ImouClock()
    assert not clock.pinned
    text = wall(8, 30, 5)
    assert clock.to_utc(text) == utc_of(text)
    assert clock.from_utc(utc_of(text)) == text


@pytest.mark.parametrize("offset", [420, -300, 0, 345])
def test_pinned_clock_uses_a_fixed_offset(offset):
    from tapo_desktop.core.imou.clock import ImouClock

    clock = ImouClock(offset)
    assert clock.pinned
    epoch = calendar.timegm((2026, 9, 19, 1, 0, 0))
    local = datetime(2026, 9, 19, 1, 0, 0) + timedelta(minutes=offset)
    assert clock.from_utc(epoch) == local.strftime("%Y-%m-%d %H:%M:%S")
    assert clock.to_utc(local.strftime("%Y-%m-%d %H:%M:%S")) == epoch


@pytest.mark.parametrize("bad", ["", "2026-09-19", "2026-9-19 8:00:00", "2026-09-19T08:00:00", "2026-13-01 00:00:00",
                                 "2026-09-19 24:00:00", None, 5, "2026-09-19 08:00:00 "])
def test_to_utc_rejects_anything_but_the_vendor_format(bad):
    from tapo_desktop.core.imou.clock import ImouClock

    assert ImouClock().to_utc(bad) is None
    assert ImouClock(0).to_utc(bad) is None


def test_shift_is_wall_clock_arithmetic_in_whole_seconds():
    from tapo_desktop.core.imou.clock import ImouClock

    clock = ImouClock(420)
    assert clock.shift(wall(8, 0, 0), 125.9) == wall(8, 2, 5)
    assert clock.shift(wall(23, 59, 59), 2) == wall(0, 0, 1, day="2026-09-20")
    assert clock.shift(wall(8), 0) == wall(8)


def test_pc_day_maps_to_the_camera_clock_for_other_zones():
    from tapo_desktop.core.imou.clock import ImouClock
    from tapo_desktop.ui.player_window import local_day_range

    day = date(2026, 9, 19)
    start, end = local_day_range(day)
    for offset in (420, -300, 0):
        clock = ImouClock(offset)
        begin_text, end_text = clock.day_range(day)
        assert begin_text == clock.from_utc(start) and end_text == clock.from_utc(end - 1)
        assert clock.to_utc(end_text) - clock.to_utc(begin_text) == end - 1 - start
    assert ImouClock().day_range(day) == (wall(0), wall(23, 59, 59))


def test_clock_from_the_camera_config_reads_the_pinned_offset_only_when_valid():
    from tapo_desktop.core.imou.clock import clock_for
    from tapo_desktop.models import CameraConfig

    def cfg(**vd):
        return CameraConfig(name="x", host="", id="imou:X", source="imou", vendor_data=vd)

    assert clock_for(cfg()).pinned is False
    assert clock_for(cfg(utc_offset_min=420)).pinned is True
    for junk in ("420", 99999, True, None, 12.5):
        assert clock_for(cfg(utc_offset_min=junk)).pinned is False


def test_clock_module_uses_no_locale_dependent_formatting():
    text = (SRC / "core" / "imou" / "clock.py").read_text(encoding="utf-8")
    assert "strftime" not in text and not re.search(r"%[xXcZ]", text)


# ============================================================================================ rows and runs (B4, M3)
def test_rows_are_parsed_sorted_deduped_and_bad_rows_dropped():
    from tapo_desktop.core.imou.records import parse_files

    rows = [rec(wall(9), wall(9, 10)), rec(wall(8), wall(8, 10)), rec(wall(8), wall(8, 10)),
            "junk", None, 5, {}, {"beginTime": wall(10)}, rec("2026-09-19 8:00", wall(11)),
            rec(wall(12, 10), wall(12)),            # ends before it begins
            {**rec(wall(13), wall(13, 5)), "localBeginTime": "nonsense"}]
    files = parse_files(rows)
    assert [(f.begin, f.end) for f in files] == [(wall(8), wall(8, 10)), (wall(9), wall(9, 10)), (wall(13), wall(13, 5))]
    assert files[2].local_begin == wall(13) and files[2].local_end == wall(13, 5)  # a bad local field falls back


def test_a_row_equal_begin_and_end_is_kept():
    from tapo_desktop.core.imou.records import parse_files

    assert len(parse_files([rec(wall(8), wall(8))])) == 1


def test_adjacent_files_merge_into_one_run_and_a_bigger_gap_does_not():
    from tapo_desktop.core.imou.clock import ImouClock
    from tapo_desktop.core.imou.records import build_runs, parse_files

    rows = [rec(wall(8), wall(8, 10)), rec(wall(8, 10, 2), wall(8, 20)), rec(wall(8, 20, 5), wall(8, 30)),
            rec(wall(9, 0, 0), wall(9, 5))]
    runs = build_runs(parse_files(rows), ImouClock())
    assert [(r.begin, r.end) for r in runs] == [(wall(8), wall(8, 20)), (wall(8, 20, 5), wall(8, 30)),
                                                (wall(9), wall(9, 5))]
    assert runs[0].start_utc == utc_of(wall(8)) and runs[0].end_utc == utc_of(wall(8, 20))


def test_overlapping_rows_merge_and_keep_the_vendor_strings():
    from tapo_desktop.core.imou.clock import ImouClock
    from tapo_desktop.core.imou.records import build_runs, parse_files

    rows = [rec(wall(8), wall(8, 30)), rec(wall(8, 10), wall(8, 20), kind="2")]
    (run,) = build_runs(parse_files(rows), ImouClock())
    assert (run.begin, run.end) == (wall(8), wall(8, 30))


def test_substream_rows_are_ignored_when_main_rows_exist():
    from tapo_desktop.core.imou.clock import ImouClock
    from tapo_desktop.core.imou.records import build_runs, parse_files

    rows = [rec(wall(8), wall(8, 10)), rec(wall(8), wall(8, 10), stream_type="1"), rec(wall(9), wall(9, 10), stream_type="1")]
    runs = build_runs(parse_files(rows), ImouClock())
    assert [(r.begin, r.stream_type) for r in runs] == [(wall(8), "0")]


def test_only_substream_rows_use_the_lowest_stream_type_present():
    from tapo_desktop.core.imou.clock import ImouClock
    from tapo_desktop.core.imou.records import build_runs, parse_files

    rows = [rec(wall(8), wall(8, 10), stream_type="2"), rec(wall(9), wall(9, 10), stream_type="1"),
            rec(wall(10), wall(10, 10), stream_type="1")]
    runs = build_runs(parse_files(rows), ImouClock())
    assert [(r.begin, r.stream_type) for r in runs] == [(wall(9), "1"), (wall(10), "1")]


def test_two_clocks_in_one_row_are_flagged():
    from tapo_desktop.core.imou.records import parse_files, two_clocks

    same = parse_files([rec(wall(8), wall(8, 10))])
    diff = parse_files([rec(wall(1), wall(1, 10), local_begin=wall(8), local_end=wall(8, 10))])
    assert two_clocks(same) is False and two_clocks(diff) is True


# ============================================================================================ list_local_records
def test_list_request_shape(cloud, clock):
    cloud.records[SERIAL] = [rec(wall(8), wall(8, 10))]
    client = make_client(cloud, clock)
    res = client.list_local_records(SERIAL, "0", wall(0), wall(23, 59, 59))
    call = [c for c in cloud.calls if c.path == "device.service.invoke"][0]
    assert call.url.endswith(INVOKE)
    p = call.params
    assert set(p) == {"token", "deviceId", "channelId", "serviceId", "inputData"}
    assert (p["deviceId"], p["channelId"], p["serviceId"]) == (SERIAL, "0", "record.file.list")
    assert p["inputData"] == {"type": "0", "beginTime": wall(0), "endTime": wall(23, 59, 59), "limit": 100}
    assert p["token"] == cloud.last_issued_token() and MOCK_APP_SECRET not in str(call.params)
    assert [(f.begin, f.end) for f in res.files] == [(wall(8), wall(8, 10))] and res.truncated is False


def test_an_empty_card_or_a_missing_records_key_is_an_empty_list(cloud, clock):
    client = make_client(cloud, clock)
    assert client.list_local_records(SERIAL, "0", wall(0), wall(23, 59, 59)).files == []


def test_a_reply_with_the_wrong_shape_is_a_protocol_error(cloud, clock, monkeypatch):
    from tapo_desktop.core.imou.errors import ImouProtocolError

    client = make_client(cloud, clock)
    for out in ({"outputData": []}, {"outputData": {"records": "x"}}, {}):
        monkeypatch.setattr(cloud, "_do_invoke", lambda token, params, out=out: {"code": "0", "msg": "ok", "data": out})
        with pytest.raises(ImouProtocolError):
            client.list_local_records(SERIAL, "0", wall(0), wall(23, 59, 59))


def test_paging_slides_the_window_until_a_short_page(cloud, clock):
    cloud.records[SERIAL] = [rec(wall(k // 60, k % 60, 0), wall(k // 60, k % 60, 50)) for k in range(250)]
    res = make_client(cloud, clock).list_local_records(SERIAL, "0", wall(0), wall(23, 59, 59))
    assert len(res.files) == 250 and res.truncated is False
    assert cloud.count("device.service.invoke") == 3
    begins = [c.params["inputData"]["beginTime"] for c in cloud.calls if c.path == "device.service.invoke"]
    assert begins[0] == wall(0) and begins[1] > begins[0] and begins[2] > begins[1]


def test_paging_stops_when_the_window_does_not_advance(cloud, clock, monkeypatch):
    row = rec(wall(8), wall(8))
    monkeypatch.setattr(cloud, "_do_invoke", lambda token, params: {
        "code": "0", "msg": "ok", "data": {"outputData": {"records": [row] * 100}}})
    res = make_client(cloud, clock).list_local_records(SERIAL, "0", wall(8), wall(23, 59, 59))
    assert cloud.count("device.service.invoke") <= 2 and len(res.files) == 1


def test_the_page_cap_and_the_total_deadline_set_the_truncated_flag(cloud, clock):
    cloud.records[SERIAL] = [rec(wall(k // 60, k % 60, 0), wall(k // 60, k % 60, 50)) for k in range(400)]
    client = make_client(cloud, clock)
    capped = client.list_local_records(SERIAL, "0", wall(0), wall(23, 59, 59), max_pages=2)
    assert capped.truncated and len(capped.files) == 200 and cloud.count("device.service.invoke") == 2
    cloud.calls.clear()
    ticks = iter([0.0, 1.0, 100.0, 101.0, 102.0])
    late = client.list_local_records(SERIAL, "0", wall(0), wall(23, 59, 59), deadline_s=45.0, now=lambda: next(ticks))
    assert late.truncated and cloud.count("device.service.invoke") == 1


def test_a_list_failure_is_not_retried(cloud, clock):
    from tapo_desktop.core.imou.errors import ImouVendorError

    cloud.fail("device.service.invoke", "DV2104", times=None)
    with pytest.raises(Exception) as err:
        make_client(cloud, clock).list_local_records(SERIAL, "0", wall(0), wall(23, 59, 59))
    assert cloud.count("device.service.invoke") == 1 and isinstance(err.value, ImouVendorError)


def test_bad_time_text_is_refused_before_anything_is_sent(cloud, clock):
    client = make_client(cloud, clock)
    for bad in ("2026-09-19", "", "19/09/2026 00:00:00"):
        with pytest.raises(ValueError):
            client.list_local_records(SERIAL, "0", bad, wall(23))
        with pytest.raises(ValueError):
            client.create_playback_url(SERIAL, "0", bad, wall(23), "0")
    assert cloud.calls == []


# ============================================================================================ the playback URL
def test_play_request_uses_localrecord_with_the_vendor_strings(cloud, clock):
    cloud.records[SERIAL] = [rec(wall(8), wall(9))]
    url = make_client(cloud, clock).create_playback_url(SERIAL, "0", wall(8, 5), wall(8, 50), "1")
    call = [c for c in cloud.calls if c.path == "createVideoDownloadStreamUrl"][0]
    assert call.params == {"token": cloud.last_issued_token(), "deviceId": SERIAL, "channelId": "0",
                           "businessType": "localRecord", "streamType": "1", "beginTime": wall(8, 5),
                           "endTime": wall(8, 50)}
    assert url.startswith("rtsp://") and MOCK_RTSP_QUERY in url and f"starttime={wall(8, 5).replace(' ', 'T')}" in url


def test_the_playback_url_is_decrypted_with_the_request_token(cloud, clock):
    cloud.records[SERIAL] = [rec(wall(8), wall(9))]
    client = make_client(cloud, clock)
    first = client.create_playback_url(SERIAL, "0", wall(8), wall(8, 30), "0")
    cloud.expire_tokens()
    second = client.create_playback_url(SERIAL, "0", wall(8), wall(8, 30), "0")  # new token, one retry inside
    assert first.startswith("rtsp://") and second.startswith("rtsp://")


@pytest.mark.parametrize("kind", ["cloudRecord", "", "REAL", "download", None])
def test_any_other_business_type_is_refused_before_sending(cloud, clock, kind):
    from tapo_desktop.core.imou.errors import ImouNotAllowedError

    client = make_client(cloud, clock)
    with pytest.raises(ImouNotAllowedError):
        client._call("/openapi/createVideoDownloadStreamUrl", {"businessType": kind, "deviceId": SERIAL})
    assert cloud.calls == []


def test_live_still_uses_business_type_real(cloud, clock):
    make_client(cloud, clock).create_rtsp_url(SERIAL, "0", 1)
    assert cloud.calls[-1].params["businessType"] == "real"


# ============================================================================================ the second allowlist (B1)
@pytest.mark.parametrize("service", ["device.reboot", "ptz.aim.relative", "record.file.delete", "storage.local.format",
                                     "record.file.alarmList", "record.file.count", "record.file.bitmap", "", None,
                                     "RECORD.FILE.LIST", "record.file.list ", ["record.file.list"]])
def test_the_invoke_path_refuses_every_other_service_id(cloud, clock, service):
    from tapo_desktop.core.imou.errors import ImouNotAllowedError

    client = make_client(cloud, clock)
    with pytest.raises(ImouNotAllowedError):
        client._call(INVOKE, {"token": "At_x", "deviceId": SERIAL, "channelId": "0", "serviceId": service})
    assert cloud.calls == []


def test_the_invoke_path_without_a_service_id_is_refused(cloud, clock):
    from tapo_desktop.core.imou.errors import ImouNotAllowedError

    with pytest.raises(ImouNotAllowedError):
        make_client(cloud, clock)._call(INVOKE, {"token": "At_x"})
    assert cloud.calls == []


def test_the_allowed_services_are_exactly_record_file_list():
    from tapo_desktop.core.imou.client import ALLOWED_SERVICES

    assert ALLOWED_SERVICES == frozenset({"record.file.list"})


def test_the_client_has_no_public_method_taking_a_service_id():
    import inspect

    from tapo_desktop.core.imou.client import ImouClient

    for name, fn in inspect.getmembers(ImouClient, inspect.isfunction):
        if not name.startswith("_"):
            assert "service" not in " ".join(inspect.signature(fn).parameters), name


def test_the_source_mentions_only_the_one_service_and_no_write_service():
    names = set()
    for path in (SRC / "core" / "imou").glob("*.py"):
        names |= set(re.findall(r"(?<![/\w.])(?:record|storage|ptz|device|power|network|image)\.[a-z]+(?:\.[a-z]+)+",
                               path.read_text("utf-8")))
    assert names <= {"record.file.list"}, names


# ============================================================================================ errors (M9) and the guard
@pytest.mark.parametrize("code,needle", [
    ("DV1049", "memory card"), ("LD1001", "memory card"), ("DV2132", "memory card"),
    ("DV2003", "cannot list"), ("DV2001", "cannot list"), ("DV2116", "cannot list"),
    ("DV1009", "busy"), ("DV2102", "did not answer"), ("DV2103", "did not answer"), ("DV2104", "did not answer"),
    ("OP1019", "no recording"), ("OP1021", "playback"), ("DV1030", "mock error")])
def test_the_playback_error_map(cloud, clock, code, needle):
    from tapo_desktop.core.imou.errors import PREFIX, ImouError

    cloud.fail("device.service.invoke", code, "playback address could not be made" if code == "OP1021" else None)
    with pytest.raises(ImouError) as err:
        make_client(cloud, clock).list_local_records(SERIAL, "0", wall(0), wall(23, 59, 59))
    text = str(err.value)
    assert text.startswith(PREFIX) and needle in text.lower() and code in text and err.value.code == code
    assert cloud.count("device.service.invoke") == 1


def test_no_card_has_its_own_type_and_a_helper_that_finds_the_text(cloud, clock):
    from tapo_desktop.core.imou.errors import ImouNoCardError, is_no_card_text

    cloud.no_card.add(SERIAL)
    with pytest.raises(ImouNoCardError) as err:
        make_client(cloud, clock).list_local_records(SERIAL, "0", wall(0), wall(23, 59, 59))
    assert is_no_card_text(str(err.value)) and not is_no_card_text("iMou: the camera is offline (DV1007).")


def test_op1039_blocks_until_the_next_minute_only(cloud, clock):
    from tapo_desktop.core.imou.errors import ImouBlockedError, ImouError
    from tapo_desktop.core.imou.guard import ImouGuard

    clock.now = 1_700_000_030.0
    guard = ImouGuard(clock=clock)
    client = make_client(cloud, clock, guard=guard)
    cloud.fail("device.service.invoke", "OP1039")
    with pytest.raises(ImouError):
        client.list_local_records(SERIAL, "0", wall(0), wall(23, 59, 59))
    until, message = guard.blocked()
    assert until == (int(clock.now // 60) + 1) * 60 and "minute" in message
    with pytest.raises(ImouBlockedError):
        client.list_local_records(SERIAL, "0", wall(0), wall(23, 59, 59))
    clock.advance(31)
    assert guard.blocked() is None
    client.list_local_records(SERIAL, "0", wall(0), wall(23, 59, 59))


def test_op1040_blocks_until_utc_midnight_like_the_other_quota_codes(cloud, clock):
    from tapo_desktop.core.imou.errors import QUOTA_CODES
    from tapo_desktop.core.imou.guard import ImouGuard, next_utc_midnight

    assert {"OP1013", "OP1014", "OP1040"} <= QUOTA_CODES
    guard = ImouGuard(clock=clock)
    cloud.fail("device.service.invoke", "OP1040")
    with pytest.raises(Exception):
        make_client(cloud, clock, guard=guard).list_local_records(SERIAL, "0", wall(0), wall(23, 59, 59))
    assert guard.blocked()[0] == next_utc_midnight(clock.now)


def test_a_blocked_guard_sends_nothing_for_list_or_play(cloud, clock):
    from tapo_desktop.core.imou.errors import ImouBlockedError
    from tapo_desktop.core.imou.guard import ImouGuard

    guard = ImouGuard(clock=clock)
    cloud.fail("accessToken", "FL1004")
    client = make_client(cloud, clock, guard=guard)
    with pytest.raises(Exception):
        client.access_token()
    cloud.calls.clear()
    with pytest.raises(ImouBlockedError):
        client.list_local_records(SERIAL, "0", wall(0), wall(23))
    with pytest.raises(ImouBlockedError):
        client.create_playback_url(SERIAL, "0", wall(0), wall(1), "0")
    assert cloud.calls == []


# ============================================================================================ nothing secret in logs
def test_no_secret_url_token_or_file_name_in_any_log_record(cloud, clock, caplog):
    caplog.set_level(logging.DEBUG)
    cloud.records[SERIAL] = [rec(wall(8), wall(9), name="secret-card-file-name")]
    client = make_client(cloud, clock)
    client.list_local_records(SERIAL, "0", wall(0), wall(23, 59, 59))
    url = client.create_playback_url(SERIAL, "0", wall(8), wall(8, 30), "0")
    cloud.fail("device.service.invoke", "OP1009", f"denied {url}")
    with pytest.raises(Exception):
        client.list_local_records(SERIAL, "0", wall(0), wall(23, 59, 59))
    text = "\n".join(r.getMessage() + " " + str(r.exc_info and r.exc_info[1]) for r in caplog.records)
    token = cloud.last_issued_token()
    assert MOCK_APP_SECRET not in text and MOCK_RTSP_QUERY not in text and "secret-card-file-name" not in text
    assert token not in text and "starttime=" not in text
