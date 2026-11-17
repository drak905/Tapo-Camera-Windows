""":8800 media with a local access token (LAT) [spec:lat.media.digest].

In LAT mode the camera's media digest password is SHA256_HEX_UPPER(LAT) for encrypt_type 3
challenges (the transform of the raw LAT); the cloud password is used only when no LAT exists.
"""
from __future__ import annotations

import datetime as dt
import hashlib

import pytest

from tapo_desktop.core.errors import TapoAuthError
from tapo_desktop.core.media_engine.local_stream import LocalStreamSupervisor
from tapo_desktop.core.media_engine.supervisor import StreamState
from tapo_desktop.core.vod import downloader as downloader_mod
from tapo_desktop.core.vod.session import MediaSession, digest_authorization, transform_password
from tapo_desktop.models import RecordingClip
from tests.fixtures.mock_tapo_server import MockTapoCamera
from tests.integration._media_helpers import Recorder, wait_until

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]

PASSWORD = "cloudpass"
LAT = "8c1f2d0b-LAT-6a1e0f9c3b2d4e5f"


@pytest.fixture
def lat_camera():
    cam = MockTapoCamera(cloud_password=PASSWORD, protocol="tpap", model="C200", tpap_credential="lat",
                         local_token=LAT).start()
    try:
        yield cam
    finally:
        cam.stop()


def test_transform_of_the_lat_is_sha256_hex_upper():
    assert transform_password(LAT, "3") == hashlib.sha256(LAT.encode()).hexdigest().upper()
    challenge = {"realm": "r", "nonce": "n", "qop": "auth", "encrypt_type": "3", "algorithm": "MD5"}
    a = digest_authorization(challenge, LAT, cnonce="c")
    b = digest_authorization(challenge, PASSWORD, cnonce="c")
    assert a != b


def test_media_session_uses_the_lat(lat_camera):
    s = MediaSession(lat_camera.host, PASSWORD, port=lat_camera.media_port, timeout=5, local_token=LAT)
    try:
        s.connect()
        assert LAT not in repr(s)
    finally:
        s.close()


def test_media_session_without_lat_is_rejected_by_a_lat_camera(lat_camera):
    s = MediaSession(lat_camera.host, PASSWORD, port=lat_camera.media_port, timeout=5)
    with pytest.raises(TapoAuthError) as err:
        s.connect()
    assert "cloud password" in str(err.value)
    s2 = MediaSession(lat_camera.host, PASSWORD, port=lat_camera.media_port, timeout=5, local_token="wrong")
    with pytest.raises(TapoAuthError) as err2:
        s2.connect()
    assert "local access token" in str(err2.value) and "wrong" not in str(err2.value)


def test_password_camera_still_works_without_lat(mock_camera):
    s = MediaSession(mock_camera.host, mock_camera.cloud_password, port=mock_camera.media_port, timeout=5,
                     local_token=None)
    try:
        s.connect()
    finally:
        s.close()


def test_live_view_with_lat(lat_camera, ffmpeg_path):
    rec = Recorder()
    sup = LocalStreamSupervisor(lat_camera.host, PASSWORD, 640, 360, port=lat_camera.media_port,
                                ffmpeg_path=ffmpeg_path, stall_timeout=3.0, local_token=LAT, **rec.kwargs())
    try:
        sup.start()
        assert wait_until(lambda: sup.state is StreamState.PLAYING, 15), (rec.state_names(), rec.reasons())
        assert wait_until(lambda: rec.frame_count() >= 10, 10)
        assert LAT not in repr(sup)
    finally:
        sup.stop()


def test_live_view_without_lat_fails_with_auth(lat_camera, ffmpeg_path):
    rec = Recorder()
    sup = LocalStreamSupervisor(lat_camera.host, PASSWORD, 640, 360, port=lat_camera.media_port,
                                ffmpeg_path=ffmpeg_path, stall_timeout=3.0, **rec.kwargs())
    try:
        sup.start()
        assert wait_until(lambda: sup.state is StreamState.FAILED, 15), rec.state_names()
        assert any("auth" in (r or "") for r in rec.reasons())
    finally:
        sup.stop()


def test_wrong_lat_failure_reason_hides_the_token(lat_camera, ffmpeg_path):
    rec = Recorder()
    bad = "wrong-token-123456"
    sup = LocalStreamSupervisor(lat_camera.host, PASSWORD, 640, 360, port=lat_camera.media_port,
                                ffmpeg_path=ffmpeg_path, stall_timeout=3.0, local_token=bad, **rec.kwargs())
    try:
        sup.start()
        assert wait_until(lambda: sup.state is StreamState.FAILED, 15), rec.state_names()
        assert all(bad not in (r or "") for r in rec.reasons())
    finally:
        sup.stop()


def test_downloader_passes_the_lat_to_the_session():
    made: list[tuple[tuple, dict]] = []

    class FakeSession:
        def __init__(self, *args, **kwargs):
            made.append((args, kwargs))

        def connect(self):
            raise TapoAuthError("stop here")

        def close(self):
            pass

    clip = RecordingClip(start=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
                         end=dt.datetime(2026, 1, 1, 0, 1, tzinfo=dt.timezone.utc))
    import pathlib
    import tempfile
    out = pathlib.Path(tempfile.mkdtemp()) / "clip.mp4"
    d = downloader_mod.RecordingDownloader("10.0.0.5", PASSWORD, session_factory=FakeSession, local_token=LAT)
    with pytest.raises(TapoAuthError):
        d.download(clip, out)
    assert made[-1][1].get("local_token") == LAT
    assert LAT not in repr(d)
    made.clear()
    d2 = downloader_mod.RecordingDownloader("10.0.0.5", PASSWORD, session_factory=FakeSession)
    with pytest.raises(TapoAuthError):
        d2.download(clip, out)
    assert "local_token" not in made[-1][1]         # old factories keep working unchanged
