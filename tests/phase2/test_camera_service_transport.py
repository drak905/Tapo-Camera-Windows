"""TapoCameraService: live transport selection (local :8800 vs RTSP), "Not on this network", and
motion via getLastAlarmInfo polling when no camera account exists."""
from __future__ import annotations

import threading
import time

import pytest

from tapo_desktop.core.errors import TapoConnectionError
from tapo_desktop.core.media_engine.supervisor import StreamState
from tapo_desktop.models import CameraConfig, CredentialKind, Credentials, StreamQuality
from tapo_desktop.services.camera_service import NOT_ON_NETWORK, TapoCameraService
from tests.e2e.fakes import FakeVault
from tests.phase2.app_fakes import FakeControlClient, SupervisorFactory

pytestmark = pytest.mark.unit

CLOUD = Credentials("admin", "tplink-id-password")
ACCOUNT = Credentials("camuser", "camera-account-pw")


def _service(*, camera_account: bool = False, control=None, **cfg):
    config = CameraConfig(**{"name": "Hall", "host": "192.168.1.20", "id": "dev1", "source": "cloud", **cfg})
    vault = FakeVault()
    vault.set(config.id, CredentialKind.CLOUD, CLOUD)
    if camera_account:
        vault.set(config.id, CredentialKind.CAMERA_ACCOUNT, ACCOUNT)
    local, rtsp = SupervisorFactory("local"), SupervisorFactory("rtsp")
    svc = TapoCameraService(config, vault, local_stream_factory=local, stream_factory=rtsp,
                            control_factory=(lambda **_kw: control) if control is not None else None,
                            alarm_poll_interval=0.05)
    return svc, local, rtsp


def _noop(*_args):
    return None


@pytest.mark.parametrize("quality,resolution,channel", [
    (StreamQuality.SD, "VGA", 0), (StreamQuality.HD, "HD", 0),
    (StreamQuality.TELE_SD, "VGA", 1), (StreamQuality.TELE_HD, "HD", 1),
])
def test_auto_without_camera_account_uses_local_stream(quality, resolution, channel):
    svc, local, rtsp = _service(media_port=18800)
    svc.start_stream(quality, 640, 360, _noop, _noop)
    sup = local.last
    assert rtsp.created == []
    assert sup.started == 1
    assert sup.args[:2] == ("192.168.1.20", CLOUD.password)
    assert sup.args[2:4] == (640, 360) or (sup.kwargs.get("width"), sup.kwargs.get("height")) == (640, 360)
    assert sup.kwargs["resolution"] == resolution
    assert sup.kwargs["port"] == 18800
    assert sup.kwargs["channel"] == channel
    assert callable(sup.kwargs["on_frame"]) and callable(sup.kwargs["on_state"])
    svc.close()
    assert sup.stopped == 1


def test_local_transport_used_even_with_camera_account():
    svc, local, rtsp = _service(camera_account=True, live_transport="local")
    svc.start_stream(StreamQuality.HD, 1280, 720, _noop, _noop)
    assert local.last.kwargs["resolution"] == "HD" and rtsp.created == []


@pytest.mark.parametrize("transport", ["rtsp", "auto"])
def test_camera_account_with_rtsp_or_auto_uses_rtsp(transport):
    svc, local, rtsp = _service(camera_account=True, live_transport=transport)
    svc.start_stream(StreamQuality.SD, 640, 360, _noop, _noop)
    assert local.created == []
    url = rtsp.last.args[0]
    assert url.startswith("rtsp://") and url.endswith("/stream2") and "192.168.1.20:554" in url
    assert rtsp.last.started == 1


def test_rtsp_without_camera_account_falls_back_to_local():
    svc, local, rtsp = _service(live_transport="rtsp")
    svc.start_stream(StreamQuality.SD, 640, 360, _noop, _noop)
    assert rtsp.created == [] and local.last.started == 1


def test_running_local_stream_is_switched_not_restarted():
    svc, local, _ = _service()
    svc.start_stream(StreamQuality.SD, 640, 360, _noop, _noop)
    svc.start_stream(StreamQuality.HD, 1280, 720, _noop, _noop)
    assert len(local.created) == 1
    assert local.last.switches == [("HD", 1280, 720)]
    svc.stop_stream()
    assert local.last.stopped == 1


def test_local_state_and_frames_are_forwarded():
    svc, local, _ = _service()
    states, frames = [], []
    svc.start_stream(StreamQuality.SD, 640, 360, frames.append, lambda s, r: states.append((s, r)))
    sup = local.last
    sup.kwargs["on_state"](StreamState.PLAYING, None)
    sup.kwargs["on_frame"]("frame-1")
    assert states == [(StreamState.PLAYING, None)] and frames == ["frame-1"]
    svc.stop_stream()
    sup.kwargs["on_frame"]("stale")  # a stopped generation is muted
    assert frames == ["frame-1"]


@pytest.mark.parametrize("cfg", [{"lan_found": False}, {"host": ""}])
def test_not_on_this_network(cfg):
    svc, local, rtsp = _service(**cfg)
    states = []
    svc.start_stream(StreamQuality.SD, 640, 360, _noop, lambda s, r: states.append((s, r)))
    assert states == [(StreamState.FAILED, NOT_ON_NETWORK)]
    assert NOT_ON_NETWORK == "Not on this network"
    assert local.created == [] and rtsp.created == []
    with pytest.raises(TapoConnectionError, match="Not on this network"):
        svc.connect()


def test_motion_without_camera_account_polls_last_alarm_info():
    control = FakeControlClient([("100", "motion"), ("100", "motion"), ("250", "person"), ("250", "person")])
    svc, _, _ = _service(control=control)
    events = []
    got = threading.Event()

    def on_event(ev):
        events.append(ev)
        got.set()

    svc.start_events(on_event)
    try:
        assert got.wait(3), "no motion event from getLastAlarmInfo polling"
        time.sleep(0.2)
    finally:
        svc.stop_events()
    polls = control.alarm_polls()
    assert polls >= 3
    assert len(events) == 1  # the baseline value ("100") is not an event
    ev = events[0]
    assert ev.active and ev.source == "last_alarm_info" and ev.camera_host == "192.168.1.20"
    assert ev.data["last_alarm_type"] == "person"
    assert ev.timestamp.timestamp() == 250
    req = control.requests[0]
    assert req == {"method": "getLastAlarmInfo", "params": {"system": {"name": ["last_alarm_info"]}}}
    time.sleep(0.2)
    assert control.alarm_polls() == polls  # the poller thread has stopped
    assert not [t for t in threading.enumerate() if t.name.startswith("tapo-alarm-") and t.is_alive()]
    svc.close()


def test_motion_with_camera_account_uses_onvif(monkeypatch):
    import tapo_desktop.core.onvif_service.events as events_mod
    import tapo_desktop.core.onvif_service.soap as soap_mod

    created = []

    class FakeOnvif:
        def __init__(self, host, user, password, port=2020):
            created.append(("client", host, user, port))

    class FakeSub:
        def __init__(self, client, cb):
            created.append(("sub",))

        def start(self):
            created.append(("start",))

        def stop(self):
            created.append(("stop",))

    monkeypatch.setattr(soap_mod, "OnvifClient", FakeOnvif)
    monkeypatch.setattr(events_mod, "PullPointSubscriber", FakeSub)
    control = FakeControlClient()
    svc, _, _ = _service(camera_account=True, control=control)
    svc.start_events(lambda ev: None)
    svc.stop_events()
    assert created[0] == ("client", "192.168.1.20", "camuser", 2020)
    assert ("start",) in created and ("stop",) in created
    assert control.alarm_polls() == 0


def test_alarm_poll_errors_do_not_stop_polling():
    class Flaky(FakeControlClient):
        def request(self, req):
            if len(self.requests) < 2:
                self.requests.append(req)
                raise TapoConnectionError("blip")
            return super().request(req)

    control = Flaky([("1", "motion"), ("1", "motion"), ("1", "motion"), ("2", "motion")])
    svc, _, _ = _service(control=control)
    got = threading.Event()
    svc.start_events(lambda ev: got.set())
    try:
        assert got.wait(3)
    finally:
        svc.close()
