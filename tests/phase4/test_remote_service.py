"""TapoCameraService routing for remote and shared cameras: control / status / PTZ / presets / recordings
through the cloud passthrough, downloads through the relay, shared cameras never logged in locally,
the remote motion-polling interval, and AccountManager filling ``role`` / ``share_type``."""
from __future__ import annotations

import datetime as dt
import hashlib
import time

import pytest

from tapo_desktop.core.errors import TapoConnectionError
from tapo_desktop.core.media_engine.supervisor import StreamState
from tapo_desktop.models import CameraConfig, CredentialKind, Credentials, DayNightMode, LanDevice, RecordingClip
from tapo_desktop.services.camera_service import TapoCameraService
from tests.e2e.fakes import FakeVault
from tests.phase2.app_fakes import FakeControlClient, SupervisorFactory
from tests.phase4.conftest import DEFAULT_EMAIL, DEFAULT_PASSWORD, DEVICE, DEVICE_ID, TERMINAL

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]

CLOUD = Credentials("admin", "tplink-id-password")


@pytest.fixture
def provider(signed_in_cloud, mock_relay):
    from tapo_desktop.core.cloud.relay import RelayClient
    from tapo_desktop.services.remote import RelayProvider

    ctx = mock_relay.client_context()
    prov = RelayProvider(signed_in_cloud, terminal_uuid=TERMINAL,
                         relay_factory=lambda c: RelayClient(c, terminal_uuid=TERMINAL, timeout=5, ssl_context=ctx))
    signed_in_cloud.local_access_tokens()
    return prov


def _service(provider, *, control=None, alarm_poll_interval: float = 5.0, **cfg):
    base = {"name": "Front Door", "host": "", "id": DEVICE_ID, "source": "cloud", "device_id": DEVICE_ID,
            "model": "C210", "lan_found": False, "online": True}
    config = CameraConfig(**{**base, **cfg})
    vault = FakeVault()
    vault.set(config.id, CredentialKind.CLOUD, CLOUD)
    created = []

    def control_factory(**kw):
        created.append(kw)
        return control or FakeControlClient()

    svc = TapoCameraService(config, vault, local_stream_factory=SupervisorFactory("local"),
                            stream_factory=SupervisorFactory("rtsp"), control_factory=control_factory,
                            alarm_poll_interval=alarm_poll_interval, remote=(lambda: provider))
    svc.created_controls = created  # type: ignore[attr-defined]
    return svc


def _sync_count(cloud) -> int:
    return len(cloud.passthrough_log)


def test_remote_camera_controls_through_the_cloud(provider, relay_cloud, remote_camera):
    svc = _service(provider)
    try:
        state = remote_camera.state
        state.privacy, state.led = True, False
        before = _sync_count(relay_cloud)
        status = svc.get_status()
        assert status == {"privacy": True, "day_night": DayNightMode.AUTO, "led": False,
                          "motion_detection": True, "alarm": False}
        assert _sync_count(relay_cloud) == before + 1  # one batched cloud call
        svc.set_privacy(False)
        svc.set_led(True)
        svc.set_day_night(DayNightMode.OFF)
        svc.set_motion_detection(False)
        svc.set_alarm(True)
        assert (state.privacy, state.led, state.inf_type, state.motion_enabled, state.alarm_active) == \
            (False, True, "off", False, True)
        x0 = state.ptz_x
        svc.ptz_move(1, 0)
        assert state.ptz_x > x0
        svc.ptz_move(1, 1)
        svc.ptz_stop()
        presets = svc.get_presets()
        assert {p.name for p in presets} >= {"Door", "Gate"}
        svc.goto_preset("1")
        assert (state.ptz_x, state.ptz_y) == (20, 0)
        today = dt.date.today()
        assert isinstance(svc.days_with_recordings(today - dt.timedelta(days=7), today), list)
        assert isinstance(svc.recordings_for_day(today), list)
        assert svc.created_controls == []  # never a LAN control client
        assert remote_camera.login_count == 0
    finally:
        svc.close()


def test_remote_download_goes_through_the_relay(provider, relay_cloud, mock_relay, remote_camera, ffmpeg_path,
                                                ffprobe_path, tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setattr("tapo_desktop.core.vod.downloader.find_ffmpeg", lambda *a, **k: ffmpeg_path)
    svc = _service(provider)
    start = dt.datetime.now(dt.timezone.utc).replace(microsecond=0) - dt.timedelta(hours=2)
    clip = RecordingClip(start=start, end=start + dt.timedelta(seconds=6), event_type=1)
    try:
        progress = []
        out = svc.download_recording(clip, tmp_path / "remote.mp4", progress=progress.append)
    finally:
        svc.close()
    probe = subprocess.run([ffprobe_path, "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=codec_name", "-of", "csv=p=0", str(out)], capture_output=True, text=True,
                           timeout=30, check=True)
    assert probe.stdout.strip() == "h264"
    assert progress[-1] == 1.0
    assert relay_cloud.calls("/v2/relay/request")[-1]["body"]["deviceId"] == DEVICE_ID
    assert remote_camera.media_active_sessions == 0  # the LAN :8800 path was not used


def test_shared_camera_on_the_lan_is_never_logged_in_locally(provider, relay_cloud, remote_camera):
    svc = _service(provider, role=1, share_type="deviceShare", lan_found=True, host=remote_camera.host,
                   https_port=remote_camera.https_port, media_port=remote_camera.media_port)
    try:
        assert svc.live_transport() == "relay"
        info = svc.connect()
        assert info.device_id == DEVICE_ID
        assert svc.get_status()["privacy"] is False
        svc.set_privacy(True)
        assert remote_camera.state.privacy is True
        assert svc.get_presets()
        svc.recordings_for_day(dt.date.today())
        assert svc.created_controls == []
        assert remote_camera.login_count == 0 and remote_camera.login_attempts == 0
        assert remote_camera.media_active_sessions == 0
    finally:
        svc.close()


def test_shared_camera_without_cloud_does_nothing_locally(remote_camera):
    from tapo_desktop.services.remote import SHARED_CLOUD_ONLY

    svc = _service(None, role=1, lan_found=True, host=remote_camera.host, https_port=remote_camera.https_port)
    states = []
    try:
        with pytest.raises(TapoConnectionError, match=SHARED_CLOUD_ONLY):
            svc.get_status()
        with pytest.raises(TapoConnectionError, match=SHARED_CLOUD_ONLY):
            svc.connect()
        svc.start_stream(__import__("tapo_desktop.models", fromlist=["StreamQuality"]).StreamQuality.SD, 640, 360,
                         lambda f: None, lambda s, r: states.append((s, r)))
        assert states[-1] == (StreamState.FAILED, SHARED_CLOUD_ONLY)
        assert svc.created_controls == []
        assert remote_camera.login_count == 0 and remote_camera.login_attempts == 0
    finally:
        svc.close()


def test_owned_lan_camera_stays_local(provider, relay_cloud):
    svc = _service(provider, lan_found=True, host="127.0.0.1")
    try:
        assert svc.live_transport() == "local"
        svc.last_alarm_info()
        assert len(svc.created_controls) == 1
        assert relay_cloud.passthrough_log == []
    finally:
        svc.close()


def test_remote_motion_polling_is_slow(provider, relay_cloud, remote_camera):
    from tapo_desktop.services.camera_service import REMOTE_ALARM_POLL_INTERVAL_S

    assert REMOTE_ALARM_POLL_INTERVAL_S >= 30
    svc = _service(provider, alarm_poll_interval=0.05)
    try:
        svc.start_events(lambda e: None)
        time.sleep(0.5)
        polls = [r for e in relay_cloud.passthrough_log for r in e["requests"] if r["method"] == "getLastAlarmInfo"]
        assert len(polls) <= 1
        assert svc.alarm_poll_interval() >= REMOTE_ALARM_POLL_INTERVAL_S
    finally:
        svc.close()


def test_lan_motion_polling_interval_unchanged(provider):
    svc = _service(provider, lan_found=True, host="127.0.0.1", alarm_poll_interval=0.05)
    try:
        svc.start_events(lambda e: None)
        assert svc.alarm_poll_interval() == pytest.approx(0.05)
    finally:
        svc.close()


def test_offline_answer_from_the_cloud_is_user_readable(provider, relay_cloud):
    relay_cloud.passthrough_errors[DEVICE_ID] = (400, -20571)
    svc = _service(provider)
    try:
        with pytest.raises(TapoConnectionError, match="Camera is offline"):
            svc.set_led(True)
    finally:
        svc.close()


# ---------------------------------------------------------------------------------- account manager
def test_account_manager_fills_role_from_the_thing_list(tmp_path, qtbot, mock_cloud_factory, mock_relay):
    from tapo_desktop.core.cloud.client import TapoCloudClient
    from tapo_desktop.services.account import AccountManager
    from tapo_desktop.services.config_store import ConfigStore
    from tapo_desktop.services.controller import AppController
    from tests.e2e.fakes import FakeServiceFactory
    from tests.fixtures.mock_cloud import make_cloud_device

    shared = make_cloud_device(9, alias="Neighbour", deviceModel="C510W", role=1)
    cloud = mock_cloud_factory(devices=[dict(DEVICE), shared])
    cloud.relay = mock_relay
    lan = [LanDevice(ip="127.0.0.1", mac="", model="C510W", device_type="SMART.IPCAMERA",
                     device_id_md5=hashlib.md5(shared["deviceId"].encode()).hexdigest())]
    factory = FakeServiceFactory()
    controller = AppController(ConfigStore(tmp_path / "config.json"), FakeVault(), service_factory=factory)
    accounts = AccountManager(controller, discover=lambda: lan,
                              cloud_factory=lambda t: TapoCloudClient(terminal_uuid=t, base_url=cloud.base_url,
                                                                      timeout=5))
    try:
        with qtbot.waitSignal(accounts.signed_in, timeout=5000):
            accounts.sign_in(DEFAULT_EMAIL, DEFAULT_PASSWORD)
        assert accounts.wait_idle(5000)
        cfg = controller.camera(shared["deviceId"])
        assert (cfg.role, cfg.share_type, cfg.lan_found, cfg.host) == (1, "deviceShare", True, "127.0.0.1")
        assert controller.camera(DEVICE_ID).role == 0
        assert controller.remote_available(shared["deviceId"])  # shared: cloud path even on the LAN
        assert controller.remote_available(DEVICE_ID)
    finally:
        accounts.wait_idle(3000)
        controller.stop_all()
        factory.shutdown()
