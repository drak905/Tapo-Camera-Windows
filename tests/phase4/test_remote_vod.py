"""Remote SD-card recordings through the cloud relay [spec:remote.relay.playback_download.*]: download slot
(streamType 3) first, fallback to the verified playback stream (streamType 1), MP4 output checked with
ffprobe; acks, stop, cancel and cleanup."""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import threading

import pytest

from tapo_desktop.core.errors import TapoError
from tapo_desktop.models import RecordingClip
from tests.fixtures.mock_tapo_server import media_assets
from tests.phase4.conftest import DEVICE_ID

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


def _clip(seconds: int = media_assets.TS_SECONDS, event_type: int = 2) -> RecordingClip:
    start = dt.datetime.now(dt.timezone.utc).replace(microsecond=0) - dt.timedelta(hours=1)
    return RecordingClip(start=start, end=start + dt.timedelta(seconds=seconds), event_type=event_type)


def _downloader(relay_client, ffmpeg_path, **kw):
    from tapo_desktop.core.cloud.relay_vod import RemoteRecordingDownloader

    return RemoteRecordingDownloader(relay_client, DEVICE_ID, model="C210", ffmpeg_path=ffmpeg_path, timeout=10, **kw)


def _probe(ffprobe_path, path) -> dict:
    out = subprocess.run([ffprobe_path, "-v", "error", "-show_entries", "stream=codec_name,codec_type:format=duration",
                          "-of", "json", str(path)], capture_output=True, timeout=30, check=True)
    return json.loads(out.stdout)


def _slot_bodies(cloud) -> list[dict]:
    return [r["body"] for r in cloud.calls("/v2/relay/request")]


def _assert_h264_mp4(ffprobe_path, path):
    info = _probe(ffprobe_path, path)
    codecs = {s["codec_type"]: s["codec_name"] for s in info["streams"]}
    assert codecs.get("video") == "h264", info
    assert float(info["format"]["duration"]) > 1.0
    assert path.read_bytes()[4:8] == b"ftyp"


def _spy_sessions(relay_client):
    """(sent JSON messages, session_factory) recording what the downloader sends into the tunnel."""
    from tapo_desktop.core.cloud.relay_vod import RelayPlaybackSession

    sent: list[dict] = []

    class Spy(RelayPlaybackSession):
        def send_json(self, message, **kw):
            sent.append(message)
            return super().send_json(message, **kw)

    return sent, (lambda download: Spy(relay_client, DEVICE_ID, download=download, model="C210", timeout=10))


def _no_temp_files(folder):
    return [p.name for p in folder.iterdir() if p.name.startswith(".")] == []


def test_download_slot_produces_mp4(relay_client, relay_cloud, mock_relay, ffmpeg_path, ffprobe_path, tmp_path):
    progress = []
    clip = _clip()
    out = _downloader(relay_client, ffmpeg_path).download(clip, tmp_path / "clip.mp4", progress=progress.append)
    assert out == tmp_path / "clip.mp4"
    _assert_h264_mp4(ffprobe_path, out)
    body = _slot_bodies(relay_cloud)[-1]
    assert body["streamType"] == 3 and body["subType"] == "cover" and body["channelId"] == "0"
    assert "resolution" not in body and "preConnection" not in body
    assert json.loads(body["customParams"]) == {}
    assert body["deviceId"] == DEVICE_ID and body["cloudType"] == 2
    req = mock_relay.vod_requests[-1]
    assert req["verb"] == "download"
    assert req["params"]["start_time"] == str(int(clip.start.timestamp()))
    assert req["params"]["end_time"] == str(int(clip.end.timestamp()))
    assert progress and progress[-1] == 1.0 and progress == sorted(progress)
    assert mock_relay.acks  # flow-control acks every 25 parts
    assert _no_temp_files(tmp_path)


def test_last_mode_is_reported(relay_client, mock_relay, ffmpeg_path, tmp_path):
    dl = _downloader(relay_client, ffmpeg_path)
    dl.download(_clip(), tmp_path / "a.mp4")
    assert dl.last_mode == "download"


@pytest.mark.parametrize("how", ["slot_refused", "tunnel_reply_refused"])
def test_falls_back_to_playback_stream(how, relay_client, relay_cloud, mock_relay, ffmpeg_path, ffprobe_path,
                                       tmp_path):
    if how == "slot_refused":
        relay_cloud.relay_refuse_stream_types = {3}
    else:
        mock_relay.vod_refuse = {"download"}
    clip = _clip()
    dl = _downloader(relay_client, ffmpeg_path)
    out = dl.download(clip, tmp_path / "clip.mp4")
    assert dl.last_mode == "playback"
    _assert_h264_mp4(ffprobe_path, out)
    body = _slot_bodies(relay_cloud)[-1]
    assert body["streamType"] == 1 and body["channelId"] == "0" and "subType" not in body
    assert "resolution" not in body and "preConnection" not in body and json.loads(body["customParams"]) == {}
    req = mock_relay.vod_requests[-1]
    assert req["verb"] == "playback"
    assert req["params"] == {"client_id": 1, "channels": [0], "scale": "1/1",
                             "start_time": str(int(clip.start.timestamp())),
                             "end_time": str(int(clip.end.timestamp())), "event_type": [1, 2]}
    assert mock_relay.acks
    assert _no_temp_files(tmp_path)


def test_playback_without_finished_stops_at_clip_end(relay_client, relay_cloud, mock_relay, ffmpeg_path,
                                                     ffprobe_path, tmp_path):
    relay_cloud.relay_refuse_stream_types = {3}
    mock_relay.playback_finishes = False
    clip = _clip(4)
    sent, factory = _spy_sessions(relay_client)
    out = _downloader(relay_client, ffmpeg_path, session_factory=factory).download(clip, tmp_path / "clip.mp4")
    duration = float(_probe(ffprobe_path, out)["format"]["duration"])
    assert 3.0 <= duration <= 6.5
    assert sent[-1] == {"type": "request", "params": {"method": "do", "stop": "null"}}  # told the camera to stop


def test_both_paths_fail(relay_client, relay_cloud, ffmpeg_path, tmp_path):
    relay_cloud.relay_refuse_stream_types = {1, 3}
    with pytest.raises(TapoError):
        _downloader(relay_client, ffmpeg_path).download(_clip(), tmp_path / "clip.mp4")
    assert list(tmp_path.iterdir()) == []


def test_offline_camera_is_not_retried_with_playback(relay_client, relay_cloud, ffmpeg_path, tmp_path):
    relay_cloud.relay_errors[DEVICE_ID] = -1007
    with pytest.raises(TapoError, match="offline"):
        _downloader(relay_client, ffmpeg_path).download(_clip(), tmp_path / "clip.mp4")
    assert [b["streamType"] for b in _slot_bodies(relay_cloud)] == [3]


def test_cancel(relay_client, mock_relay, ffmpeg_path, tmp_path):
    from tapo_desktop.core.vod.downloader import DownloadCancelled

    cancel = threading.Event()
    cancel.set()
    with pytest.raises(DownloadCancelled):
        _downloader(relay_client, ffmpeg_path).download(_clip(), tmp_path / "clip.mp4", cancel=cancel)
    assert list(tmp_path.iterdir()) == []


def test_cancel_during_transfer(relay_client, relay_cloud, mock_relay, ffmpeg_path, tmp_path):
    from tapo_desktop.core.vod.downloader import DownloadCancelled

    relay_cloud.relay_refuse_stream_types = {3}
    mock_relay.playback_finishes = False
    cancel = threading.Event()

    def progress(value):
        if value > 0:
            cancel.set()

    sent, factory = _spy_sessions(relay_client)
    with pytest.raises(DownloadCancelled):
        _downloader(relay_client, ffmpeg_path, session_factory=factory).download(
            _clip(60), tmp_path / "clip.mp4", progress=progress, cancel=cancel)
    assert list(tmp_path.iterdir()) == []
    assert sent[-1]["params"].get("stop") == "null"


def test_playback_request_builder():
    from tapo_desktop.core.cloud.relay_vod import build_playback_request

    clip = _clip()
    assert build_playback_request(clip) == {"type": "request", "params": {"method": "get", "playback": {
        "client_id": 1, "channels": [0], "scale": "1/1", "start_time": str(int(clip.start.timestamp())),
        "end_time": str(int(clip.end.timestamp())), "event_type": [1, 2]}}}


def test_slot_request_for_playback_keeps_the_preview_body_unchanged(relay_client, relay_cloud):
    relay_client.request_slot(DEVICE_ID, "SMART.IPCAMERA", "C210", "VGA")
    preview = _slot_bodies(relay_cloud)[-1]
    assert preview["streamType"] == 0 and preview["resolution"] == "VGA" and preview["preConnection"] == 0
    assert "channelId" not in preview
    slot = relay_client.request_slot(DEVICE_ID, "SMART.IPCAMERA", "C210", stream_type=1)
    assert "type=sdvod" in slot.relay_path
    with pytest.raises(ValueError):
        relay_client.request_slot(DEVICE_ID, stream_type=7)
