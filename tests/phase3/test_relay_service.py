"""TapoCameraService routing (local vs relay vs offline), remote-only restrictions and the remote stream
concurrency gate. Unit tests with fake supervisors: no network, no ffmpeg."""
from __future__ import annotations

import threading

import pytest

from tapo_desktop.core.errors import TapoConnectionError
from tapo_desktop.core.media_engine.supervisor import StreamState
from tapo_desktop.models import CameraConfig, CloudDevice, CredentialKind, Credentials, StreamQuality
from tapo_desktop.services.camera_service import NOT_ON_NETWORK, TapoCameraService
from tapo_desktop.services.remote import (
    CAMERA_OFFLINE,
    DEFAULT_MAX_REMOTE_STREAMS,
    NETWORK_ONLY,
    REMOTE_CLICK_TO_VIEW,
    RelayProvider,
    RemoteStreamGate,
)
from tests.e2e.fakes import FakeVault
from tests.phase2.app_fakes import FakeControlClient, SupervisorFactory

pytestmark = pytest.mark.unit

CLOUD = Credentials("admin", "tplink-id-password")


class _Relay:
    """Stand-in RelayClient (the provider only hands it to the supervisor factory)."""


def _provider(max_streams: int = DEFAULT_MAX_REMOTE_STREAMS, devices=None):
    sups = SupervisorFactory("relay")
    provider = RelayProvider(object(), terminal_uuid="T" * 32, max_streams=max_streams,
                             relay_factory=lambda _cloud: _Relay(), supervisor_factory=sups)
    if devices:
        provider.update_devices(devices)
    return provider, sups


def _service(provider=None, *, idx: int = 1, control=None, **cfg):
    cid = f"DEV{idx:03d}"
    base = {"name": f"Away {idx}", "host": "", "id": cid, "source": "cloud", "device_id": cid, "model": "C210",
            "lan_found": False, "online": True}
    config = CameraConfig(**{**base, **cfg})
    vault = FakeVault()
    vault.set(config.id, CredentialKind.CLOUD, CLOUD)
    local = SupervisorFactory("local")
    created_controls = []

    def control_factory(**kw):
        created_controls.append(kw)
        return control or FakeControlClient()

    svc = TapoCameraService(config, vault, local_stream_factory=local, stream_factory=SupervisorFactory("rtsp"),
                            control_factory=control_factory, remote=(lambda: provider))
    svc.created_controls = created_controls  # type: ignore[attr-defined]
    return svc, local


class _States:
    def __init__(self):
        self.items: list[tuple[StreamState, str | None]] = []

    def __call__(self, state, reason):
        self.items.append((state, reason))

    @property
    def last(self):
        return self.items[-1] if self.items else None


def _noop(*_a):
    return None


# ---------------------------------------------------------------------------------- routing
def test_lan_camera_stays_local_even_with_relay():
    provider, sups = _provider()
    svc, local = _service(provider, host="192.168.1.20", lan_found=True)
    svc.start_stream(StreamQuality.SD, 640, 360, _noop, _noop)
    assert local.last.started == 1 and sups.created == []
    assert svc.live_transport() == "local"


@pytest.mark.parametrize("quality,resolution", [(StreamQuality.SD, "VGA"), (StreamQuality.HD, "HD")])
def test_remote_camera_uses_relay(quality, resolution):
    provider, sups = _provider(devices=[CloudDevice(device_id="DEV001", mac="", model="C210", alias="Away 1",
                                                    device_type="SMART.TAPODOORBELL",
                                                    raw={"isSupportIoTCloud": True})])
    svc, local = _service(provider)
    svc.start_stream(quality, 640, 360, _noop, _noop)
    sup = sups.last
    assert local.created == [] and sup.started == 1
    assert isinstance(sup.args[0], _Relay) and sup.args[1] == "DEV001" and sup.args[2:4] == (640, 360)
    assert sup.kwargs["resolution"] == resolution
    assert sup.kwargs["model"] == "C210" and sup.kwargs["device_type"] == "SMART.TAPODOORBELL"
    assert sup.kwargs["cloud_type"] == 2
    assert callable(sup.kwargs["on_frame"]) and callable(sup.kwargs["on_state"])
    assert svc.live_transport() == "relay"
    svc.close()
    assert sup.stopped == 1


def test_legacy_non_iot_camera_uses_cloud_type_1():
    provider, sups = _provider(devices=[CloudDevice(device_id="DEV001", mac="", model="C100", alias="Old",
                                                    raw={"isSupportIoTCloud": False})])
    svc, _ = _service(provider)
    svc.start_stream(StreamQuality.SD, 640, 360, _noop, _noop)
    assert sups.last.kwargs["cloud_type"] == 1


def test_unknown_device_defaults():
    provider, sups = _provider()
    svc, _ = _service(provider)
    svc.start_stream(StreamQuality.SD, 640, 360, _noop, _noop)
    assert sups.last.kwargs["cloud_type"] == 2 and sups.last.kwargs["device_type"] == "SMART.IPCAMERA"


def test_running_relay_stream_is_switched_and_keeps_its_slot():
    provider, sups = _provider(max_streams=1)
    svc, _ = _service(provider)
    svc.start_stream(StreamQuality.SD, 640, 360, _noop, _noop)
    svc.start_stream(StreamQuality.HD, 1280, 720, _noop, _noop)
    assert len(sups.created) == 1 and sups.last.switches == [("HD", 1280, 720)]
    assert provider.gate.holders() == ["DEV001"]


def test_no_relay_provider_means_not_on_this_network():
    svc, local = _service(None)
    states = _States()
    svc.start_stream(StreamQuality.SD, 640, 360, _noop, states)
    assert states.items == [(StreamState.FAILED, NOT_ON_NETWORK)] and local.created == []
    with pytest.raises(TapoConnectionError, match="Not on this network"):
        svc.connect()


def test_relay_provider_not_signed_in_means_not_on_this_network():
    provider, sups = _provider()
    provider.detach()
    svc, _ = _service(provider)
    states = _States()
    svc.start_stream(StreamQuality.SD, 640, 360, _noop, states)
    assert states.last == (StreamState.FAILED, NOT_ON_NETWORK) and sups.created == []


@pytest.mark.parametrize("with_provider", [True, False])
def test_offline_cloud_camera(with_provider):
    provider, sups = _provider()
    svc, local = _service(provider if with_provider else None, online=False)
    states = _States()
    svc.start_stream(StreamQuality.SD, 640, 360, _noop, states)
    assert states.items == [(StreamState.FAILED, CAMERA_OFFLINE)]
    assert sups.created == [] and local.created == []
    with pytest.raises(TapoConnectionError, match="Camera is offline"):
        svc.connect()


def test_remote_connect_needs_no_network_and_controls_are_lan_only():
    provider, _ = _provider()
    svc, _ = _service(provider)
    info = svc.connect()
    assert (info.model, info.alias, info.device_id) == ("C210", "Away 1", "DEV001")
    assert svc.created_controls == []
    for call in (lambda: svc.get_status(), lambda: svc.set_privacy(True), lambda: svc.ptz_move(1, 0),
                 lambda: svc.get_presets(), lambda: svc.recordings_for_day(__import__("datetime").date.today())):
        with pytest.raises(TapoConnectionError, match="Available on the camera's network only"):
            call()
    with pytest.raises(TapoConnectionError, match=NETWORK_ONLY):
        svc.start_events(_noop)
    assert svc.created_controls == []
    assert NETWORK_ONLY == "Available on the camera's network only"


def test_relay_failure_releases_the_slot():
    provider, sups = _provider(max_streams=1)
    svc, _ = _service(provider)
    svc.start_stream(StreamQuality.SD, 640, 360, _noop, _noop)
    sups.last.kwargs["on_state"](StreamState.FAILED, "Camera is offline (the TP-Link cloud relay cannot reach it).")
    assert provider.gate.holders() == []


# ---------------------------------------------------------------------------------- concurrency gate
def test_gate_basics():
    gate = RemoteStreamGate(2)
    evicted = []
    assert gate.acquire("a", lambda: evicted.append("a"))
    assert gate.acquire("b", lambda: evicted.append("b"))
    assert gate.acquire("a", lambda: evicted.append("a2"))  # already held: still yes, no eviction
    assert not gate.acquire("c", lambda: evicted.append("c"))
    assert gate.holders() == ["a", "b"] and evicted == []
    assert gate.acquire("c", lambda: evicted.append("c"), preempt=True)
    assert evicted == ["a"] and gate.holders() == ["b", "c"]
    gate.release("b")
    gate.release("b")  # idempotent
    assert gate.holders() == ["c"]
    assert gate.acquire("d", lambda: None)
    with pytest.raises(ValueError):
        RemoteStreamGate(0)


def test_gate_is_thread_safe():
    gate = RemoteStreamGate(4)
    wins = []
    lock = threading.Lock()

    def worker(i):
        if gate.acquire(f"k{i}", lambda: None):
            with lock:
                wins.append(i)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(32)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(wins) == 4 and len(gate.holders()) == 4


def test_extra_remote_streams_wait_for_a_click():
    provider, sups = _provider(max_streams=2)
    services = [_service(provider, idx=i)[0] for i in range(1, 4)]
    states = [_States() for _ in services]
    for svc, st in zip(services, states):
        svc.start_stream(StreamQuality.SD, 640, 360, _noop, st)
    assert len(sups.created) == 2
    assert states[2].items == [(StreamState.FAILED, REMOTE_CLICK_TO_VIEW)]
    assert REMOTE_CLICK_TO_VIEW == "Remote - click to view"
    # click on the third tile: it takes the slot of the stream that runs longest (the first)
    first_sup = sups.created[0]
    services[2].start_stream(StreamQuality.SD, 640, 360, _noop, states[2], preempt=True)
    assert len(sups.created) == 3 and sups.created[2].started == 1
    assert first_sup.stopped == 1
    assert states[0].last == (StreamState.FAILED, REMOTE_CLICK_TO_VIEW)
    assert sorted(provider.gate.holders()) == ["DEV002", "DEV003"]
    # stopping one frees a slot for the next start
    services[1].stop_stream()
    assert provider.gate.holders() == ["DEV003"]
    services[0].start_stream(StreamQuality.SD, 640, 360, _noop, states[0])
    assert len(sups.created) == 4 and sorted(provider.gate.holders()) == ["DEV001", "DEV003"]
    for svc in services:
        svc.close()
    assert provider.gate.holders() == []


def test_default_limit_is_four_for_a_3x3_grid():
    provider, sups = _provider()
    services = [_service(provider, idx=i)[0] for i in range(1, 10)]
    waiting = 0
    for svc in services:
        st = _States()
        svc.start_stream(StreamQuality.SD, 640, 360, _noop, st)
        waiting += st.last == (StreamState.FAILED, REMOTE_CLICK_TO_VIEW)
    assert DEFAULT_MAX_REMOTE_STREAMS == 4
    assert len(sups.created) == 4 and waiting == 5
