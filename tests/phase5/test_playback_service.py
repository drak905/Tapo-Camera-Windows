"""TapoCameraService playback routing (LAN :8800 vs cloud relay vs offline / not on network), control
forwarding, stale-callback muting, friendly stream-limit text and the CameraService defaults. Unit
tests with fake supervisors: no network, no ffmpeg."""
from __future__ import annotations

import pytest

from tapo_desktop.core.errors import TapoError
from tapo_desktop.core.media_engine.supervisor import StreamState
from tapo_desktop.models import CameraConfig, CloudDevice, CredentialKind, Credentials
from tapo_desktop.services.camera_service import NOT_ON_NETWORK, TapoCameraService
from tapo_desktop.services.remote import CAMERA_OFFLINE, SHARED_CLOUD_ONLY, RelayProvider
from tests.e2e.fakes import FakeCameraService, FakeVault, make_config
from tests.phase5._fakes import PlaybackSupervisorFactory

pytestmark = pytest.mark.unit

CLOUD = Credentials("admin", "tplink-id-password")
START, END = 1_700_000_000.0, 1_700_086_399.0
SEGMENTS = [(START, START + 60), (START + 120, START + 180)]


class _Relay:
    pass


def _provider():
    sups = PlaybackSupervisorFactory()
    provider = RelayProvider(object(), terminal_uuid="T" * 32, relay_factory=lambda _c: _Relay(),
                             playback_factory=sups)
    provider.update_devices([CloudDevice(device_id="DEV001", mac="", model="C510W", alias="Away",
                                         device_type="SMART.IPCAMERA", raw={"isSupportIoTCloud": True})])
    return provider, sups


def _service(provider=None, *, token: str | None = None, **cfg):
    base = {"name": "Cam", "host": "192.168.1.20", "id": "DEV001", "source": "manual", "device_id": "DEV001",
            "model": "C210", "lan_found": True, "online": True, "media_port": 8800}
    config = CameraConfig(**{**base, **cfg})
    vault = FakeVault()
    vault.set(config.id, CredentialKind.CLOUD, CLOUD)
    if token:
        vault.set(config.id, CredentialKind.LOCAL_TOKEN, Credentials("admin", token))
    lan = PlaybackSupervisorFactory()
    svc = TapoCameraService(config, vault, playback_factory=lan, remote=(lambda: provider))
    return svc, lan


class Sink:
    def __init__(self):
        self.states: list[tuple] = []
        self.frames: list = []
        self.positions: list[float] = []

    def on_state(self, state, reason):
        self.states.append((state, reason))

    def on_frame(self, frame):
        self.frames.append(frame)

    def on_position(self, utc):
        self.positions.append(utc)

    def args(self):
        return self.on_frame, self.on_state, self.on_position


def test_lan_camera_plays_over_8800():
    svc, lan = _service(token="LAT-123")
    sink = Sink()
    svc.start_playback(START, END, 1280, 720, *sink.args(), segments=SEGMENTS)
    sup = lan.last
    assert sup.args[:4] == ("192.168.1.20", "tplink-id-password", 1280, 720)
    kw = sup.kwargs
    assert kw["start_utc"] == START and kw["end_utc"] == END and list(kw["segments"]) == SEGMENTS
    assert kw["port"] == 8800 and kw["local_token"] == "LAT-123" and kw["limiter_key"] == "192.168.1.20"
    assert all(callable(kw[k]) for k in ("on_frame", "on_state", "on_position"))
    assert sup.names() == ["start"]
    assert svc.playback_transport() == "local"


def test_controls_are_forwarded_and_stop_is_idempotent():
    svc, lan = _service()
    svc.start_playback(START, END, 640, 360, *Sink().args())
    svc.pause_playback()
    svc.resume_playback()
    svc.seek_playback(START + 30)
    svc.set_playback_speed("4/1")
    svc.stop_playback()
    svc.stop_playback()
    assert lan.last.calls == [("start",), ("pause",), ("resume",), ("seek", START + 30), ("set_speed", "4/1"),
                              ("stop",)]
    svc.pause_playback()  # nothing running: no error


def test_restart_replaces_and_mutes_the_old_session():
    svc, lan = _service()
    old, new = Sink(), Sink()
    svc.start_playback(START, END, 640, 360, *old.args())
    first = lan.last
    svc.start_playback(START + 120, END, 640, 360, *new.args())
    assert first.names() == ["start", "stop"] and len(lan.created) == 2
    first.kwargs["on_state"](StreamState.PLAYING, None)
    first.kwargs["on_position"](START + 1)
    lan.last.kwargs["on_position"](START + 121)
    assert old.states == [] and old.positions == [] and new.positions == [START + 121]


def test_stream_limit_reason_is_explained():
    from tapo_desktop.services.camera_service import PLAYBACK_LIMIT_MESSAGE

    svc, lan = _service()
    sink = Sink()
    svc.start_playback(START, END, 640, 360, *sink.args())
    lan.last.kwargs["on_state"](StreamState.FAILED, "stream limit reached: camera refused (-52407)")
    assert sink.states[-1] == (StreamState.FAILED, PLAYBACK_LIMIT_MESSAGE)
    assert "live view" in PLAYBACK_LIMIT_MESSAGE.lower()


def test_remote_camera_plays_through_the_relay():
    provider, relay_sups = _provider()
    svc, lan = _service(provider, host="", lan_found=False, source="cloud", model="C510W")
    sink = Sink()
    svc.start_playback(START, END, 640, 360, *sink.args(), segments=SEGMENTS)
    assert lan.created == []
    sup = relay_sups.last
    assert isinstance(sup.args[0], _Relay) and sup.args[1:4] == ("DEV001", 640, 360)
    assert sup.kwargs["model"] == "C510W" and sup.kwargs["device_type"] == "SMART.IPCAMERA"
    assert sup.kwargs["cloud_type"] == 2 and sup.kwargs["start_utc"] == START
    assert sup.names() == ["start"]
    assert provider.gate.holders() == []  # playback does not take a live-view relay slot
    assert svc.playback_transport() == "relay"
    svc.close()
    assert sup.names()[-1] == "stop"


def test_shared_camera_on_the_lan_uses_the_relay():
    provider, relay_sups = _provider()
    svc, lan = _service(provider, source="cloud", role=1)
    svc.start_playback(START, END, 640, 360, *Sink().args())
    assert lan.created == [] and relay_sups.last.names() == ["start"]


@pytest.mark.parametrize("cfg,reason", [
    ({"source": "cloud", "lan_found": False, "host": "", "online": False}, CAMERA_OFFLINE),
    ({"lan_found": False, "host": ""}, NOT_ON_NETWORK),
    ({"source": "cloud", "role": 1}, SHARED_CLOUD_ONLY),
])
def test_unreachable_cameras_fail_fast(cfg, reason):
    svc, lan = _service(None, **cfg)
    sink = Sink()
    svc.start_playback(START, END, 640, 360, *sink.args())
    assert lan.created == []
    assert sink.states == [(StreamState.FAILED, reason)]


def test_offline_remote_camera_with_provider():
    provider, relay_sups = _provider()
    svc, _ = _service(provider, source="cloud", lan_found=False, host="", online=False)
    sink = Sink()
    svc.start_playback(START, END, 640, 360, *sink.args())
    assert relay_sups.created == [] and sink.states == [(StreamState.FAILED, CAMERA_OFFLINE)]


def test_closed_service_refuses_playback():
    svc, lan = _service()
    svc.close()
    with pytest.raises(TapoError):
        svc.start_playback(START, END, 640, 360, *Sink().args())


def test_camera_service_defaults_are_safe():
    svc = FakeCameraService(make_config(0))
    with pytest.raises(TapoError):
        svc.start_playback(START, END, 640, 360, lambda f: None, lambda s, r: None, lambda u: None)
    for call in (svc.pause_playback, svc.resume_playback, svc.stop_playback):
        call()
    svc.seek_playback(START)
    svc.set_playback_speed("1/1")
