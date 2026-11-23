"""iMou route A, camera service: click-started live view, own cap, idle stop, daily budget, error map, no leaks.

The cloud is the in-process mock; the stream supervisor is a fake except in the two real-FFmpeg tests at the end.
"""
from __future__ import annotations

import inspect
import logging
import threading
import time

import pytest

from tapo_desktop.core.media_engine.supervisor import StreamState
from tapo_desktop.models import CameraConfig, StreamQuality
from tests.fixtures.mock_imou_cloud import MOCK_APP_ID, MOCK_APP_SECRET, MOCK_HLS_QUERY, MOCK_RTSP_QUERY


# ------------------------------------------------------------------------------------------ helpers
class FakeSup:
    """Stands in for ImouStreamSupervisor: records what it was given, never spawns anything."""

    instances: list = []

    def __init__(self, **kw):
        self.kw = kw
        self.started = False
        self.stopped = 0
        FakeSup.instances.append(self)

    def start(self):
        self.started = True

    def stop(self, timeout=3.0):
        self.stopped += 1

    def emit(self, state: str, reason: str | None = None):
        self.kw["on_state"](StreamState[state], reason)


@pytest.fixture(autouse=True)
def _fresh_fakes():
    FakeSup.instances = []


def imou_cfg(serial="MOCKSERIAL0001", name="Mock Camera A", **kw) -> CameraConfig:
    base = dict(name=name, host="", id=f"imou:{serial}", source="imou", device_id=serial, model="IPC-MOCK",
                online=True, lan_found=False,
                vendor_data={"serial": serial, "channel": "0", "cloud_name": name, "data_center": "sg"})
    base.update(kw)
    return CameraConfig(**base)


def make_runtime(cloud, clock, *, limits=None, factory=FakeSup, real_clock=False):
    from tapo_desktop.core.imou.client import ImouClient
    from tapo_desktop.core.imou.guard import ImouGuard
    from tapo_desktop.core.imou.runtime import ImouLimits, ImouRuntime, UsageMeter
    from tapo_desktop.services import imou_camera_service

    clk = time.time if real_clock else clock
    cloud.clock = clk
    guard = ImouGuard(clock=clk)
    client = ImouClient(MOCK_APP_ID, MOCK_APP_SECRET, "sg", transport=cloud.post, clock=clk, guard=guard)
    wanted = limits or ImouLimits()
    rt = ImouRuntime(client=client, guard=guard, clock=clk, limits=lambda: wanted, meter=UsageMeter(clock=clk),
                     supervisor_factory=factory)
    imou_camera_service.set_runtime(rt)
    return rt


def service(cfg=None):
    from tapo_desktop.services.imou_camera_service import ImouCameraService

    return ImouCameraService(cfg or imou_cfg(), None)


class Rec:
    def __init__(self):
        self.frames, self.states = [], []

    def on_frame(self, frame):
        self.frames.append(frame)

    def on_state(self, state, reason=None):
        self.states.append((state.name if hasattr(state, "name") else state, reason))

    def names(self):
        return [s for s, _r in self.states]

    def reasons(self, name):
        return [r for s, r in self.states if s == name]


def start(svc, rec, quality=StreamQuality.SD, size=(640, 360)):
    svc.start_stream(quality, *size, rec.on_frame, rec.on_state)


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


# ------------------------------------------------------------------------------------------ not supported
def test_capability_flags_only_live_view_and_playback_are_on():
    from tapo_desktop.services.imou_camera_service import ImouCameraService as S

    assert S.supports_live_view is True and S.supports_playback is True  # memory-card playback (requirement change)
    for flag in ("supports_ptz", "supports_controls", "supports_events", "supports_rename"):
        assert getattr(S, flag) is False, flag


def test_every_other_action_says_not_supported_for_imou():
    from datetime import date

    from tapo_desktop.models import DayNightMode, RecordingClip
    from tapo_desktop.services.camera_service import CameraService
    from tapo_desktop.services.imou_camera_service import IMOU_UNSUPPORTED, ImouUnsupportedError

    svc = service()
    calls = {
        "ptz_move": lambda: svc.ptz_move(1, 0), "ptz_stop": lambda: svc.ptz_stop(),
        "get_presets": lambda: svc.get_presets(), "goto_preset": lambda: svc.goto_preset("1"),
        "get_status": lambda: svc.get_status(), "set_privacy": lambda: svc.set_privacy(True),
        "set_day_night": lambda: svc.set_day_night(DayNightMode.AUTO), "set_led": lambda: svc.set_led(True),
        "set_alarm": lambda: svc.set_alarm(True), "set_motion_detection": lambda: svc.set_motion_detection(True),
        "days_with_recordings": lambda: svc.days_with_recordings(date(2024, 1, 1), date(2024, 1, 2)),
        "download_recording": lambda: svc.download_recording(
            RecordingClip(None, None), __import__("pathlib").Path("x.mp4")),
        "rename": lambda: svc.rename("x"),
    }
    # recordings_for_day and start_playback are built now (test_imou_sd_service.py): requirement change
    handled = set(calls) | {"connect", "start_stream", "stop_stream", "start_events", "stop_events", "close",
                            "recordings_for_day"}
    assert set(CameraService.__abstractmethods__) <= handled  # no abstract method is left out
    for name, call in calls.items():
        with pytest.raises(ImouUnsupportedError) as err:
            call()
        assert IMOU_UNSUPPORTED in str(err.value), name
    assert IMOU_UNSUPPORTED == "Not supported for iMou cameras yet"


def test_connect_and_close_touch_no_network(cloud, clock):
    make_runtime(cloud, clock)
    svc = service()
    info = svc.connect()
    svc.start_events(lambda e: None)
    svc.stop_events()
    svc.stop_stream()
    svc.close()
    assert info.alias == "Mock Camera A" and info.device_id == "MOCKSERIAL0001"
    assert cloud.calls == []


# ------------------------------------------------------------------------------------------ start / stop
def test_without_a_connection_start_says_so_and_calls_nothing(cloud):
    from tapo_desktop.core.imou.errors import ImouError

    with pytest.raises(ImouError) as err:
        start(service(), Rec())
    assert "not connected" in str(err.value).lower()


def test_start_sd_uses_the_rtsp_url_software_decode_and_shows_the_delay_text(cloud, clock):
    make_runtime(cloud, clock)
    svc, rec = service(), Rec()
    start(svc, rec)
    sup = FakeSup.instances[0]
    assert sup.started and sup.kw["kind"] == "rtsp"
    assert MOCK_RTSP_QUERY in sup.kw["url"] and "subtype=1" in sup.kw["url"]
    assert (sup.kw["width"], sup.kw["height"], sup.kw["hwaccel"]) == (640, 360, "none")
    assert sup.kw["stall_timeout"] == 30.0
    call = [c for c in cloud.calls if c.path == "createVideoDownloadStreamUrl"][0]
    assert call.params["streamType"] == "1" and call.params["businessType"] == "real"
    assert rec.states[0][0] == "CONNECTING" and "delay" in (rec.states[0][1] or "").lower()
    sup.emit("CONNECTING")  # the supervisor's own state must not hide the delay text
    assert "delay" in (rec.reasons("CONNECTING")[-1] or "").lower()
    sup.emit("PLAYING")
    assert rec.names()[-1] == "PLAYING"


def test_start_hd_uses_the_main_stream_and_hardware_decode(cloud, clock):
    make_runtime(cloud, clock)
    start(service(), Rec(), StreamQuality.HD, (1280, 720))
    sup = FakeSup.instances[0]
    assert sup.kw["hwaccel"] == "auto" and (sup.kw["width"], sup.kw["height"]) == (1280, 720)
    assert [c for c in cloud.calls if c.path == "createVideoDownloadStreamUrl"][0].params["streamType"] == "0"


def test_stop_stops_the_supervisor_once_and_is_idempotent(cloud, clock):
    make_runtime(cloud, clock)
    svc, rec = service(), Rec()
    start(svc, rec)
    svc.stop_stream()
    svc.stop_stream()
    assert FakeSup.instances[0].stopped == 1
    FakeSup.instances[0].emit("STOPPED")  # a late callback after our own stop is dropped
    assert "STOPPED" not in rec.names() and "FAILED" not in rec.names()


def test_the_camera_that_is_offline_in_the_list_does_not_call_the_cloud(cloud, clock):
    from tapo_desktop.core.imou.errors import ImouOfflineError

    make_runtime(cloud, clock)
    with pytest.raises(ImouOfflineError):
        start(service(imou_cfg(online=False)), Rec())
    assert cloud.calls == [] and FakeSup.instances == []


def test_dv1007_from_the_cloud_is_offline_and_not_retried_and_frees_the_slot(cloud, clock):
    from tapo_desktop.core.imou.errors import ImouOfflineError

    rt = make_runtime(cloud, clock)
    cloud.devices[0]["deviceStatus"] = "offline"
    with pytest.raises(ImouOfflineError):
        start(service(), Rec())
    assert cloud.count("createVideoDownloadStreamUrl") == 1 and cloud.count("bindDeviceLive") == 0
    assert rt.live_ids() == set()


# ------------------------------------------------------------------------------------------ HLS fallback
def test_hls_is_used_only_when_the_rtsp_call_is_refused_and_is_deleted_on_stop(cloud, clock):
    rt = make_runtime(cloud, clock)
    cloud.fail("createVideoDownloadStreamUrl", "DV1019")
    svc, rec = service(), Rec()
    start(svc, rec)
    sup = FakeSup.instances[0]
    assert sup.kw["kind"] == "hls" and MOCK_HLS_QUERY in sup.kw["url"]
    assert cloud.live_count() == 1 and cloud.count("bindDeviceLive") == 1
    svc.stop_stream()
    assert cloud.live_count() == 0 and cloud.count("unbindLive") == 1
    assert rt.live_ids() == set()
    svc.stop_stream()
    assert cloud.count("unbindLive") == 1


def test_an_existing_live_address_is_reused_and_still_deleted_on_stop(cloud, clock):
    make_runtime(cloud, clock)
    cloud.fail("createVideoDownloadStreamUrl", "DV1019")
    left_over = __import__("tapo_desktop.core.imou.client", fromlist=["x"]).ImouClient(
        MOCK_APP_ID, MOCK_APP_SECRET, "sg", transport=cloud.post, clock=clock)
    left_over.bind_live("MOCKSERIAL0001", "0", 1)  # an address a crashed run left behind
    svc = service()
    start(svc, Rec())
    assert FakeSup.instances[0].kw["kind"] == "hls"
    svc.stop_stream()
    assert cloud.live_count() == 0


def test_hls_delete_also_runs_when_the_stream_fails(cloud, clock):
    make_runtime(cloud, clock)
    cloud.fail("createVideoDownloadStreamUrl", "DV1026")
    svc, rec = service(), Rec()
    start(svc, rec)
    FakeSup.instances[0].emit("FAILED", "ffmpeg exited (code 1)")
    assert cloud.live_count() == 0
    assert rec.names()[-1] == "FAILED" and rec.reasons("FAILED")[-1].startswith("iMou: ")


@pytest.mark.parametrize("code,exc_name", [("SN1001", "ImouAuthError"), ("FL1004", "ImouQuotaError"),
                                           ("OP1009", "ImouVendorError"), ("LV1008", "ImouVendorError"),
                                           ("DV1007", "ImouOfflineError")])
def test_other_errors_do_not_fall_back_to_hls(cloud, clock, code, exc_name):
    from tapo_desktop.core.imou import errors

    rt = make_runtime(cloud, clock)
    cloud.fail("createVideoDownloadStreamUrl", code, "vendor words")
    with pytest.raises(getattr(errors, exc_name)):
        start(service(), Rec())
    assert cloud.count("bindDeviceLive") == 0 and FakeSup.instances == [] and rt.live_ids() == set()


def test_quota_error_blocks_the_next_start_without_any_call(cloud, clock):
    from tapo_desktop.core.imou.errors import ImouBlockedError, ImouQuotaError

    make_runtime(cloud, clock)
    cloud.fail("createVideoDownloadStreamUrl", "FL1002")
    with pytest.raises(ImouQuotaError):
        start(service(), Rec())
    before = len(cloud.calls)
    with pytest.raises(ImouBlockedError) as err:
        start(service(imou_cfg("MOCKSERIAL0002", "Mock Camera B")), Rec())
    assert "free quota used" in str(err.value).lower() and len(cloud.calls) == before


def test_signature_error_is_not_retried(cloud, clock):
    from tapo_desktop.core.imou.errors import ImouAuthError

    make_runtime(cloud, clock)
    cloud.fail("accessToken", "SN1001", times=None)
    with pytest.raises(ImouAuthError) as err:
        start(service(), Rec())
    assert "App ID" in str(err.value)
    assert cloud.paths() == ["accessToken"]


# ------------------------------------------------------------------------------------------ cap and switch
def test_own_cap_of_two_live_streams(cloud, clock):
    from tapo_desktop.core.imou.errors import ImouError

    cloud.devices.append(__import__("tests.fixtures.mock_imou_cloud", fromlist=["x"]).make_device("MOCKSERIAL0003"))
    rt = make_runtime(cloud, clock)
    a, b, c = (service(imou_cfg(s)) for s in ("MOCKSERIAL0001", "MOCKSERIAL0002", "MOCKSERIAL0003"))
    start(a, Rec())
    start(b, Rec())
    calls = len(cloud.calls)
    with pytest.raises(ImouError) as err:
        start(c, Rec())
    assert "2" in str(err.value) and len(cloud.calls) == calls  # refused before any cloud call
    a.stop_stream()
    start(c, Rec())
    assert rt.live_ids() == {"imou:MOCKSERIAL0002", "imou:MOCKSERIAL0003"}


def test_cap_comes_from_the_limits_callable(cloud, clock):
    from tapo_desktop.core.imou.errors import ImouError
    from tapo_desktop.core.imou.runtime import ImouLimits

    make_runtime(cloud, clock, limits=ImouLimits(max_live=1))
    start(service(), Rec())
    with pytest.raises(ImouError):
        start(service(imou_cfg("MOCKSERIAL0002", "B")), Rec())


def test_switching_quality_of_the_running_camera_keeps_its_slot(cloud, clock):
    from tapo_desktop.core.imou.runtime import ImouLimits

    rt = make_runtime(cloud, clock, limits=ImouLimits(max_live=1))
    svc, rec = service(), Rec()
    start(svc, rec)
    start(svc, rec, StreamQuality.HD, (1280, 720))
    assert [s.stopped for s in FakeSup.instances] == [1, 0]
    assert FakeSup.instances[1].kw["hwaccel"] == "auto"
    assert rt.live_ids() == {"imou:MOCKSERIAL0001"}


def test_a_failed_supervisor_frees_the_slot_and_reports_failed(cloud, clock):
    rt = make_runtime(cloud, clock)
    svc, rec = service(), Rec()
    start(svc, rec)
    FakeSup.instances[0].emit("FAILED", "no first frame for 30.0 s (stalled)")
    assert rt.live_ids() == set()
    assert rec.names()[-1] == "FAILED" and rec.reasons("FAILED")[-1].startswith("iMou: ")
    # requirement change: a lost stream now waits for its retry, so only a click (preempt) starts it again at once
    svc.start_stream(StreamQuality.SD, 640, 360, rec.on_frame, rec.on_state, preempt=True)
    assert len(FakeSup.instances) == 2


def test_the_failure_text_never_carries_a_live_url(cloud, clock):
    make_runtime(cloud, clock)
    svc, rec = service(), Rec()
    start(svc, rec)
    url = FakeSup.instances[0].kw["url"]
    FakeSup.instances[0].emit("FAILED", f"ffmpeg exited: {url}: Server returned 404")
    shown = rec.reasons("FAILED")[-1]
    assert MOCK_RTSP_QUERY not in shown and "rtsp://" not in shown


# ------------------------------------------------------------------------------------------ idle stop and budget
def test_idle_stop_after_the_limit_asks_for_a_click_and_frees_everything(cloud, clock):
    from tapo_desktop.core.imou.runtime import ImouLimits

    rt = make_runtime(cloud, clock, limits=ImouLimits(idle_seconds=0.2), real_clock=True)
    svc, rec = service(), Rec()
    start(svc, rec)
    assert wait_for(lambda: "FAILED" in rec.names(), 3)
    assert "click" in rec.reasons("FAILED")[-1].lower() and rec.reasons("FAILED")[-1].startswith("iMou: ")
    assert FakeSup.instances[0].stopped == 1 and rt.live_ids() == set()
    assert rt.meter.used("imou:MOCKSERIAL0001") >= 0.15


def test_the_default_idle_limit_is_ten_minutes():
    from tapo_desktop.core.imou.runtime import ImouLimits

    d = ImouLimits()
    assert (d.idle_seconds, d.daily_seconds, d.max_live, d.stall_timeout) == (600.0, 1200.0, 2, 30.0)


def test_daily_budget_refuses_a_start_when_used_up(cloud, clock):
    from tapo_desktop.core.imou.errors import ImouError

    rt = make_runtime(cloud, clock)
    rt.meter.add("imou:MOCKSERIAL0001", 1200)
    with pytest.raises(ImouError) as err:
        start(service(), Rec())
    assert "daily" in str(err.value).lower() and "20" in str(err.value)
    assert cloud.calls == [] and FakeSup.instances == [] and rt.live_ids() == set()
    start(service(imou_cfg("MOCKSERIAL0002", "B")), Rec())  # the budget is per camera


def test_daily_budget_stops_a_running_stream_when_it_runs_out(cloud, clock):
    from tapo_desktop.core.imou.runtime import ImouLimits

    rt = make_runtime(cloud, clock, limits=ImouLimits(daily_seconds=0.3), real_clock=True)
    svc, rec = service(), Rec()
    start(svc, rec)
    assert wait_for(lambda: "FAILED" in rec.names(), 3)
    assert "daily" in rec.reasons("FAILED")[-1].lower()
    assert FakeSup.instances[0].stopped == 1 and rt.live_ids() == set()


def test_usage_meter_rolls_over_at_the_utc_day_and_survives_a_restart(clock):
    from tapo_desktop.core.imou.runtime import UsageMeter

    saved = {}
    meter = UsageMeter(clock=clock, load=lambda: saved.get("v"), save=lambda v: saved.__setitem__("v", v))
    meter.add("a", 90)
    meter.add("a", 30)
    meter.add("b", 5)
    assert meter.used("a") == 120 and meter.remaining("a", 1200) == 1080 and meter.used("b") == 5
    again = UsageMeter(clock=clock, load=lambda: saved.get("v"), save=lambda v: saved.__setitem__("v", v))
    assert again.used("a") == 120
    clock.advance(86_400)
    assert again.used("a") == 0 and again.remaining("a", 1200) == 1200


@pytest.mark.parametrize("junk", [None, "x", 3, {"day": 5}, {"day": "2023-11-14", "seconds": "no"},
                                  {"day": "2023-11-14", "seconds": {"a": "lots", "b": -4, "c": float("nan")}}])
def test_usage_meter_ignores_damaged_saved_state(clock, junk):
    from tapo_desktop.core.imou.runtime import UsageMeter

    meter = UsageMeter(clock=clock, load=lambda: junk)
    assert meter.used("a") == 0 and meter.used("b") == 0 and meter.used("c") == 0


def test_usage_is_added_when_the_stream_stops(cloud, clock):
    rt = make_runtime(cloud, clock)
    svc = service()
    start(svc, Rec())
    clock.advance(95)
    svc.stop_stream()
    assert rt.meter.used("imou:MOCKSERIAL0001") == 95


# ------------------------------------------------------------------------------------------ nothing secret in logs
def test_no_log_record_holds_a_live_url_or_secret(cloud, clock, caplog):
    caplog.set_level(logging.DEBUG)
    make_runtime(cloud, clock)
    svc = service()
    start(svc, Rec())
    FakeSup.instances[0].emit("FAILED", f"boom {FakeSup.instances[0].kw['url']}")
    cloud.fail("createVideoDownloadStreamUrl", "DV1019")
    start(svc, Rec())
    FakeSup.instances[1].emit("FAILED", "x")
    svc.stop_stream()
    svc.close()
    text = "\n".join(r.getMessage() for r in caplog.records)
    for secret in (MOCK_RTSP_QUERY, MOCK_HLS_QUERY, MOCK_APP_SECRET, cloud.last_issued_token()):
        assert secret not in text


# ------------------------------------------------------------------------------------------ the real supervisor
def test_supervisor_commands_rtsp_vs_hls_and_the_secret_url_stays_out_of_logs(caplog):
    from tapo_desktop.core.imou.stream import ImouStreamSupervisor

    caplog.set_level(logging.DEBUG)
    rtsp = f"rtsp://203.0.113.7:554/live?channel=1&{MOCK_RTSP_QUERY}"
    hls = f"https://cmgw.invalid:8890/LCO/x.m3u8?{MOCK_HLS_QUERY}"
    a = ImouStreamSupervisor(rtsp, "rtsp", 640, 360, on_frame=lambda f: None, hwaccel="none")
    b = ImouStreamSupervisor(hls, "hls", 640, 360, on_frame=lambda f: None, hwaccel="none")
    cmd_a = a._decoder_command("ffmpeg", a._target, None, None)
    cmd_b = b._decoder_command("ffmpeg", b._target, None, None)
    assert cmd_a[cmd_a.index("-i") + 1] == rtsp and "-rtsp_transport" in cmd_a
    assert cmd_b[cmd_b.index("-i") + 1] == hls and "-rtsp_transport" not in cmd_b
    assert "-probesize" not in cmd_b
    assert repr(a).find(MOCK_RTSP_QUERY) < 0 and repr(b).find(MOCK_HLS_QUERY) < 0
    assert MOCK_RTSP_QUERY not in "\n".join(r.getMessage() for r in caplog.records)


def test_supervisor_makes_one_attempt_and_ends_failed_without_leaking(ffmpeg_path, caplog, monkeypatch):
    """Real ffmpeg against a closed loopback port: one attempt, FAILED (never RECONNECTING), no URL in any log."""
    from tapo_desktop.core.imou.stream import ImouStreamSupervisor

    caplog.set_level(logging.DEBUG)
    states = []
    query = "token=MOCKLEAKCHECK0123456789"
    sup = ImouStreamSupervisor(f"rtsp://127.0.0.1:1/live?{query}", "rtsp", 320, 180, on_frame=lambda f: None,
                               on_state=lambda s, r: states.append((s.name, r)), hwaccel="none",
                               ffmpeg_path=ffmpeg_path, stall_timeout=8.0)
    sup.start()
    assert wait_for(lambda: states and states[-1][0] == "FAILED", 20)
    sup.stop()
    assert "RECONNECTING" not in [s for s, _ in states]
    assert sup.stats()["reconnects"] == 0
    everything = "\n".join(r.getMessage() for r in caplog.records) + repr(states)
    assert query not in everything


def test_real_ffmpeg_plays_rtsp_through_the_service_and_stops_cleanly(cloud, clock, mock_camera, ffmpeg_path):
    from tapo_desktop.core.imou.runtime import ImouLimits
    from tapo_desktop.core.imou.stream import make_supervisor

    cloud.rtsp_url = mock_camera.rtsp_url("stream2")
    rt = make_runtime(cloud, clock, factory=lambda **kw: make_supervisor(ffmpeg_path=ffmpeg_path, **kw),
                      limits=ImouLimits(stall_timeout=15.0), real_clock=True)
    svc, rec = service(), Rec()
    start(svc, rec)
    assert wait_for(lambda: len(rec.frames) >= 2, 20), rec.states
    assert rec.frames[0].width == 640 and rec.frames[0].height == 360
    assert "PLAYING" in rec.names()
    svc.stop_stream()
    assert rt.live_ids() == set()
    assert wait_for(lambda: mock_camera.rtsp_active_clients == 0, 5)


def test_real_ffmpeg_plays_an_hls_address_from_loopback(cloud, clock, tmp_path, ffmpeg_path, require_loopback_http_stream):
    import http.server
    import subprocess

    from tapo_desktop.core.imou.runtime import ImouLimits
    from tapo_desktop.core.imou.stream import make_supervisor

    out = tmp_path / "hls"
    out.mkdir()
    subprocess.run([ffmpeg_path, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc=size=320x180:rate=10", "-t", "6", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-f", "hls",
                    "-hls_time", "2", "-hls_list_size", "0", "-hls_playlist_type", "vod", str(out / "live.m3u8")],
                   check=True, timeout=60)
    class Handler(http.server.SimpleHTTPRequestHandler):
        # the mock hands out <host>/LCO/<serial>/<channel>/<stream>/<token>.m3u8: serve the generated files by name
        def translate_path(self, path):
            name = path.split("?")[0].rsplit("/", 1)[-1]
            return str(out / ("live.m3u8" if name.endswith(".m3u8") else name))

        def log_message(self, *args, **kwargs):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        cloud.hls_host = f"http://127.0.0.1:{server.server_address[1]}"
        cloud.fail("createVideoDownloadStreamUrl", "DV1019")
        rt = make_runtime(cloud, clock, factory=lambda **kw: make_supervisor(ffmpeg_path=ffmpeg_path, **kw),
                          limits=ImouLimits(stall_timeout=20.0), real_clock=True)
        svc, rec = service(), Rec()
        start(svc, rec)
        assert wait_for(lambda: len(rec.frames) >= 2, 30), rec.states
        svc.stop_stream()
        assert cloud.live_count() == 0 and rt.live_ids() == set()
    finally:
        server.shutdown()
        server.server_close()


def test_service_module_has_no_kami_import_and_no_iMou_import_in_kami_files():
    import pathlib

    root = pathlib.Path(inspect.getfile(inspect.getmodule(service))).resolve().parents[2] / "src" / "tapo_desktop"
    for path in list(root.glob("**/*imou*.py")) + list((root / "core" / "imou").glob("*.py")):
        assert "kami" not in path.read_text(encoding="utf-8").lower(), path.name
    for path in list(root.glob("**/*kami*.py")) + list((root / "core" / "kami").glob("**/*.py")):
        assert "imou" not in path.read_text(encoding="utf-8").lower(), path.name


def test_supervisor_still_falls_back_to_software_decode_once_then_ends_after_one_attempt(monkeypatch):
    """Full screen asks for hardware decoding; if that fails before the first frame the base class retries in
    software at once. That is the same attempt budget (no extra cloud call): every other end is final."""
    from tapo_desktop.core.imou.stream import ImouStreamSupervisor
    from tapo_desktop.core.media_engine.supervisor import StreamSupervisor, _Outcome

    sup = ImouStreamSupervisor("rtsp://203.0.113.7/x?token=A", "rtsp", 1280, 720, on_frame=lambda f: None,
                               hwaccel="auto")
    monkeypatch.setattr(StreamSupervisor, "_attempt",
                        lambda self, exe, target: _Outcome(reason="hw failed", hw_unusable=True))
    assert sup._attempt("ffmpeg", sup._target).fatal is False  # hardware decoding not tried in software yet
    sup._hw_disabled = True
    assert sup._attempt("ffmpeg", sup._target).fatal is True
    monkeypatch.setattr(StreamSupervisor, "_attempt", lambda self, exe, target: _Outcome(reason="", played=True))
    outcome = sup._attempt("ffmpeg", sup._target)
    assert outcome.fatal is True and outcome.reason == "the stream ended"
