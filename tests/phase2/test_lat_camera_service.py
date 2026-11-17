"""TapoCameraService with a per-camera local access token (vault ``CredentialKind.LOCAL_TOKEN``).

The LAT goes to the control client, the :8800 live view and the recording downloader; when the vault
LAT changes the control client is rebuilt (fresh auth breaker). Diagnostics only say yes/no.
"""
from __future__ import annotations

import pytest

from tapo_desktop.core.control_client import tpap
from tapo_desktop.core.errors import TapoAuthError
from tapo_desktop.models import CameraConfig, CredentialKind, Credentials, StreamQuality
from tapo_desktop.services.camera_service import TapoCameraService
from tests.e2e.fakes import FakeVault
from tests.fixtures.mock_tapo_server import MockTapoCamera
from tests.phase2.app_fakes import SupervisorFactory

CLOUD = Credentials("admin", "tplink-id-password")
LAT = "LAT-0123456789abcdef-camera"
LAT2 = "LAT-rotated-fedcba9876543210"


class FakeClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.logged_in = False
        self.closed = False
        self.breaker_resets = 0

    def login(self):
        self.logged_in = True

    def get_device_info(self):
        from tapo_desktop.models import DeviceInfo
        return DeviceInfo(model="C200", alias="x", firmware="1.5.7")

    def get_privacy(self):
        return False

    def get_day_night(self):
        from tapo_desktop.models import DayNightMode
        return DayNightMode.AUTO

    def get_led(self):
        return True

    def get_motion_detection(self):
        return (True, None)

    def close(self):
        self.closed = True


class ControlFactory:
    def __init__(self):
        self.created: list[FakeClient] = []

    def __call__(self, **kwargs):
        c = FakeClient(**kwargs)
        self.created.append(c)
        return c


def _service(*, lat: str | None = LAT, **cfg):
    config = CameraConfig(**{"name": "Hall", "host": "192.168.1.20", "id": "dev1", "source": "cloud", **cfg})
    vault = FakeVault()
    vault.set(config.id, CredentialKind.CLOUD, CLOUD)
    if lat is not None:
        vault.set(config.id, CredentialKind.LOCAL_TOKEN, Credentials("admin", lat))
    local = SupervisorFactory("local")
    control = ControlFactory()
    svc = TapoCameraService(config, vault, local_stream_factory=local, stream_factory=SupervisorFactory("rtsp"),
                            control_factory=control)
    return svc, vault, local, control


def _noop(*_a):
    return None


@pytest.mark.unit
def test_control_client_gets_the_lat():
    svc, _vault, _local, control = _service()
    svc.connect()
    assert control.created[-1].kwargs["local_token"] == LAT
    assert control.created[-1].kwargs["password"] == CLOUD.password
    svc.close()


@pytest.mark.unit
def test_no_lat_means_no_local_token_kwarg():
    svc, _vault, local, control = _service(lat=None)
    svc.connect()
    assert "local_token" not in control.created[-1].kwargs   # Phase 1 factories keep working
    svc.start_stream(StreamQuality.SD, 640, 360, _noop, _noop)
    assert "local_token" not in local.last.kwargs
    svc.close()


@pytest.mark.unit
def test_lat_change_rebuilds_the_control_client():
    svc, vault, _local, control = _service()
    svc.connect()
    first = control.created[-1]
    svc.get_status()
    assert len(control.created) == 1                  # unchanged LAT: same client
    vault.set("dev1", CredentialKind.LOCAL_TOKEN, Credentials("admin", LAT2))
    svc.get_status()
    assert len(control.created) == 2 and first.closed
    assert control.created[-1].kwargs["local_token"] == LAT2
    vault.delete("dev1")
    vault.set("dev1", CredentialKind.CLOUD, CLOUD)
    svc.get_status()
    assert len(control.created) == 3
    assert "local_token" not in control.created[-1].kwargs
    svc.close()


@pytest.mark.unit
def test_live_view_gets_the_lat_and_restarts_when_it_changes():
    svc, vault, local, _control = _service()
    svc.start_stream(StreamQuality.SD, 640, 360, _noop, _noop)
    first = local.last
    assert first.kwargs["local_token"] == LAT
    assert first.args[:2] == ("192.168.1.20", CLOUD.password)
    svc.start_stream(StreamQuality.HD, 1280, 720, _noop, _noop)
    assert local.last is first and first.switches            # same LAT: switch in place
    vault.set("dev1", CredentialKind.LOCAL_TOKEN, Credentials("admin", LAT2))
    svc.start_stream(StreamQuality.HD, 1280, 720, _noop, _noop)
    assert local.last is not first and first.stopped == 1
    assert local.last.kwargs["local_token"] == LAT2
    svc.close()


@pytest.mark.unit
def test_download_gets_the_lat(monkeypatch, tmp_path):
    seen = {}

    class FakeDownloader:
        def __init__(self, host, password, **kwargs):
            seen.update(host=host, password=password, **kwargs)

        def download(self, clip, out_path, **kw):
            return out_path

    from tapo_desktop.core.vod import downloader as mod
    monkeypatch.setattr(mod, "RecordingDownloader", FakeDownloader)
    svc, _vault, _local, _control = _service()
    svc.download_recording(object(), tmp_path / "x.mp4")
    assert seen["local_token"] == LAT and seen["password"] == CLOUD.password
    svc.close()


@pytest.mark.unit
def test_diagnostics_say_yes_or_no_only():
    svc, vault, _local, _control = _service()
    text = svc.diagnostics()
    assert "using local access token: yes" in text
    assert LAT not in text
    vault.delete("dev1")
    vault.set("dev1", CredentialKind.CLOUD, CLOUD)
    assert "using local access token: no" in svc.diagnostics()
    svc.close()


@pytest.mark.unit
def test_auth_failure_message_mentions_lat_use_but_not_the_token():
    class Failing(FakeClient):
        def login(self):
            raise TapoAuthError("camera rejected the local access token and the TP-Link ID password")

    svc, _vault, _local, _control = _service()
    svc._control_factory = lambda **kw: Failing(**kw)
    with pytest.raises(TapoAuthError) as err:
        svc.connect()
    assert "using local access token: yes" in str(err.value)
    assert LAT not in str(err.value)
    svc.close()


# ---------------------------------------------------------------- real mock camera
@pytest.fixture
def lat_cam():
    cam = MockTapoCamera(cloud_password=CLOUD.password, protocol="tpap", model="C200", tpap_credential="lat",
                         local_token=LAT).start()
    try:
        yield cam
    finally:
        cam.stop()


@pytest.mark.integration
def test_service_against_lat_camera_and_breaker_reset_on_new_lat(lat_cam, monkeypatch):
    monkeypatch.setattr(tpap, "RELOGIN_RETRY_DELAY_S", 0.05)
    config = CameraConfig(name="C200", host=lat_cam.host, id="dev-lat", source="cloud", control_protocol="tpap",
                          https_port=lat_cam.https_port, media_port=lat_cam.media_port)
    vault = FakeVault()
    vault.set(config.id, CredentialKind.CLOUD, CLOUD)
    vault.set(config.id, CredentialKind.LOCAL_TOKEN, Credentials("admin", "stale-lat"))
    svc = TapoCameraService(config, vault)
    try:
        with pytest.raises(TapoAuthError):
            svc.connect()
        assert lat_cam.tpap_share_attempts == 2
        with pytest.raises(TapoAuthError):
            svc.connect()                              # breaker: no new password attempts
        assert lat_cam.tpap_share_attempts == 2
        vault.set(config.id, CredentialKind.LOCAL_TOKEN, Credentials("admin", LAT))
        assert svc.connect().model == "C200"           # new LAT -> new client, breaker reset
        assert lat_cam.tpap_share_attempts == 3
    finally:
        svc.close()
