"""TapoCameraService picks the decoder from the stream policy: software for grid (SD/VGA) streams, hardware
(``"auto"``: d3d11va with software fallback) for HD ones, on every live transport (local :8800, RTSP,
relay). A quality switch keeps the running supervisor (and its decoder). Fake supervisors, no network."""
from __future__ import annotations

import pytest

from tapo_desktop.models import CameraConfig, CredentialKind, Credentials, StreamQuality
from tapo_desktop.services.camera_service import TapoCameraService
from tapo_desktop.services.remote import RelayProvider
from tests.e2e.fakes import FakeVault
from tests.phase2.app_fakes import FakeControlClient, SupervisorFactory

pytestmark = pytest.mark.unit

CLOUD = Credentials("admin", "tplink-id-password")
ACCOUNT = Credentials("camuser", "camera-account-pw")
SD, HD = StreamQuality.SD, StreamQuality.HD


def _noop(*_a):
    return None


def _lan(*, camera_account: bool = False, **cfg):
    config = CameraConfig(**{"name": "Hall", "host": "192.168.1.20", "id": "dev1", **cfg})
    vault = FakeVault()
    vault.set(config.id, CredentialKind.CLOUD, CLOUD)
    if camera_account:
        vault.set(config.id, CredentialKind.CAMERA_ACCOUNT, ACCOUNT)
    local, rtsp = SupervisorFactory("local"), SupervisorFactory("rtsp")
    svc = TapoCameraService(config, vault, local_stream_factory=local, stream_factory=rtsp,
                            control_factory=lambda **_kw: FakeControlClient())
    return svc, local, rtsp


def _remote():
    sups = SupervisorFactory("relay")
    provider = RelayProvider(object(), terminal_uuid="T" * 32, relay_factory=lambda _c: object(),
                             supervisor_factory=sups)
    config = CameraConfig(name="Away", host="", id="DEV1", source="cloud", device_id="DEV1", model="C210",
                          lan_found=False, online=True)
    vault = FakeVault()
    vault.set(config.id, CredentialKind.CLOUD, CLOUD)
    svc = TapoCameraService(config, vault, local_stream_factory=SupervisorFactory("local"),
                            stream_factory=SupervisorFactory("rtsp"),
                            control_factory=lambda **_kw: FakeControlClient(), remote=lambda: provider)
    return svc, sups


@pytest.mark.parametrize("quality,decoder", [
    (StreamQuality.SD, "none"), (StreamQuality.TELE_SD, "none"),
    (StreamQuality.HD, "auto"), (StreamQuality.TELE_HD, "auto"),
])
def test_local_stream_decoder_follows_quality(quality, decoder):
    svc, local, _ = _lan()
    svc.start_stream(quality, 640, 360, _noop, _noop)
    assert local.last.kwargs["hwaccel"] == decoder
    svc.close()


@pytest.mark.parametrize("quality,decoder", [(SD, "none"), (HD, "auto")])
def test_rtsp_stream_decoder_follows_quality(quality, decoder):
    svc, _, rtsp = _lan(camera_account=True, live_transport="rtsp")
    svc.start_stream(quality, 640, 360, _noop, _noop)
    assert rtsp.last.kwargs["hwaccel"] == decoder
    svc.close()


@pytest.mark.parametrize("quality,decoder", [(SD, "none"), (HD, "auto")])
def test_relay_stream_decoder_follows_quality(quality, decoder):
    svc, sups = _remote()
    svc.start_stream(quality, 640, 360, _noop, _noop)
    assert sups.last.kwargs["hwaccel"] == decoder
    svc.close()


def test_switch_to_fullscreen_keeps_the_running_supervisor():
    """Grid -> fullscreen switches the stream in place (no restart, the picture stays up); the decoder
    chosen at start is kept."""
    svc, local, _ = _lan()
    svc.start_stream(SD, 640, 360, _noop, _noop)
    sup = local.last
    svc.start_stream(HD, 1280, 720, _noop, _noop)
    assert local.created == [sup] and sup.switches and sup.stopped == 0
    assert sup.kwargs["hwaccel"] == "none"
    svc.close()
