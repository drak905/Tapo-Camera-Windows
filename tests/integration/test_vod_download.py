"""Tier 2: MediaSession + RecordingDownloader against the mock camera's :8800 endpoint.

[spec:stream.local8800.handshake, vod.download.session, vod.stream.flow_control_ack, vod.download.mp4_conversion]
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from tapo_desktop.core.errors import StreamLimitError, TapoAuthError, TapoError
from tapo_desktop.core.vod.downloader import RecordingDownloader, build_download_request
from tapo_desktop.core.vod.session import MediaSession
from tests.integration._media_helpers import cloud_password, ffmpeg_children, wait_until

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]


def _ffprobe(ffmpeg_path: str) -> str:
    sibling = Path(ffmpeg_path).with_name(Path(ffmpeg_path).name.lower().replace("ffmpeg", "ffprobe"))
    path = str(sibling) if sibling.exists() else shutil.which("ffprobe")
    if not path:
        pytest.skip("ffprobe not found")
    return path


def _probe(ffprobe: str, path: Path) -> dict:
    out = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "stream=codec_type,codec_name:format=duration,format_name",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True, timeout=30,
    )
    return json.loads(out.stdout)


def _session(cam, password=None, **kw) -> MediaSession:
    return MediaSession(cam.host, cloud_password(cam) if password is None else password, port=cam.media_port,
                        timeout=5.0, **kw)


def test_media_session_download_handshake(mock_camera):
    clip = mock_camera.state.recordings[0]
    with _session(mock_camera) as s:
        s.connect()
        seq = s.send_json(build_download_request(clip))
        assert seq == 1
        first_json = None
        media = 0
        head = b""
        for part in s.parts():
            ctype = part.headers.get("Content-Type", "")
            if ctype.startswith("application/json"):
                msg = json.loads(part.body)
                if first_json is None:
                    first_json = msg
                if msg.get("params", {}).get("event_type") == "stream_status":
                    break
            elif ctype.startswith("video/mp2t"):
                media += 1
                head += part.body[: 188 - len(head)] if len(head) < 188 else b""
                if media >= 30:
                    break
        assert head[:1] == b"\x47", "decrypted MPEG-TS must start with the 0x47 sync byte"
        assert first_json is not None
        assert first_json["type"] == "response" and first_json["seq"] == 1
        assert first_json["result"]["error_code"] == 0
        assert first_json["result"]["session_id"]
        assert media >= 1


def test_wrong_cloud_password_is_auth_error(mock_camera):
    s = _session(mock_camera, password="definitely-wrong")
    with pytest.raises(TapoAuthError):
        s.connect()
    s.close()


def test_download_seeded_clip_to_valid_mp4(mock_camera, ffmpeg_path, tmp_path):
    clip = mock_camera.state.recordings[0]
    progress: list[float] = []
    dl = RecordingDownloader(mock_camera.host, cloud_password(mock_camera), port=mock_camera.media_port,
                             ffmpeg_path=ffmpeg_path)
    out = tmp_path / "clip.mp4"
    result = dl.download(clip, out, progress=progress.append)
    assert result == out and out.is_file()
    info = _probe(_ffprobe(ffmpeg_path), out)
    assert "mp4" in info["format"]["format_name"]
    assert float(info["format"]["duration"]) > 0
    assert "h264" in [s["codec_name"] for s in info["streams"] if s["codec_type"] == "video"]
    assert progress and progress[-1] == 1.0 and progress == sorted(progress)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["clip.mp4"]
    assert wait_until(lambda: not ffmpeg_children(), 2)


def test_download_every_seeded_clip(mock_camera, ffmpeg_path, tmp_path):
    dl = RecordingDownloader(mock_camera.host, cloud_password(mock_camera), port=mock_camera.media_port,
                             ffmpeg_path=ffmpeg_path)
    for i, clip in enumerate(mock_camera.state.recordings):
        assert dl.download(clip, tmp_path / f"c{i}.mp4").stat().st_size > 0


def test_download_wrong_password_raises_and_leaves_nothing(mock_camera, ffmpeg_path, tmp_path):
    dl = RecordingDownloader(mock_camera.host, "wrong", port=mock_camera.media_port, ffmpeg_path=ffmpeg_path)
    with pytest.raises(TapoAuthError):
        dl.download(mock_camera.state.recordings[0], tmp_path / "x.mp4")
    assert list(tmp_path.iterdir()) == []


def test_cancel_removes_partial_file(mock_camera, ffmpeg_path, tmp_path):
    cancel = threading.Event()
    calls = []

    def progress(p):
        calls.append(p)
        cancel.set()  # cancel on first progress report

    dl = RecordingDownloader(mock_camera.host, cloud_password(mock_camera), port=mock_camera.media_port,
                             ffmpeg_path=ffmpeg_path)
    out = tmp_path / "cancel.mp4"
    try:
        dl.download(mock_camera.state.recordings[0], out, progress=progress, cancel=cancel)
    except TapoError:
        pass
    assert not out.exists()
    assert list(tmp_path.iterdir()) == []
    assert wait_until(lambda: not ffmpeg_children(), 2)


def test_third_concurrent_media_session_is_refused(mock_camera, ffmpeg_path, tmp_path):
    a, b = _session(mock_camera), _session(mock_camera)
    try:
        a.connect()
        b.connect()
        dl = RecordingDownloader(mock_camera.host, cloud_password(mock_camera), port=mock_camera.media_port,
                                 ffmpeg_path=ffmpeg_path)
        with pytest.raises(StreamLimitError):
            dl.download(mock_camera.state.recordings[0], tmp_path / "busy.mp4")
        assert list(tmp_path.iterdir()) == []
    finally:
        a.close()
        b.close()
