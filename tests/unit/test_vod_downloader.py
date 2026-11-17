"""Tier 1: RecordingDownloader driven through an injected fake session (session_factory).

[spec:vod.download.session, vod.download.response, vod.stream.flow_control_ack,
vod.stream.notifications, vod.download.mp4_conversion]
The fake replays a real MPEG-TS (H.264 + PCMA, generated once with ffmpeg) as video/mp2t parts.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tapo_desktop.core.errors import StreamLimitError, TapoDeviceError, TapoError
from tapo_desktop.core.vod.downloader import (
    RecordingDownloader,
    build_download_request,
    build_remux_args,
    build_stop_download_request,
)
from tapo_desktop.core.vod.session import MediaPart
from tapo_desktop.models import RecordingClip

pytestmark = pytest.mark.unit

START = datetime(2025, 1, 3, 10, 26, 40, tzinfo=timezone.utc)  # 1735900000
CLIP = RecordingClip(start=START, end=START + timedelta(seconds=120), event_type=2)
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not on PATH")


# ------------------------------------------------------------------ pure builders
def test_build_download_request_matches_spec():
    req = build_download_request(CLIP, player_id="p-1")
    assert req == {
        "type": "request",
        "params": {
            "method": "get",
            "download": {
                "client_id": 1,
                "channels": [0],
                "media_type": 0,
                "start_time": "1735900000",
                "end_time": "1735900120",
                "event_type": [2],
                "download_type": "normal",
                "player_id": "p-1",
            },
        },
    }


def test_build_download_request_defaults():
    clip = RecordingClip(start=START, end=START + timedelta(seconds=30))
    dl = build_download_request(clip)["params"]["download"]
    assert "event_type" not in dl
    assert dl["start_time"] == "1735900000" and dl["end_time"] == "1735900030"
    assert isinstance(dl["player_id"], str) and len(dl["player_id"]) >= 8
    assert dl["player_id"] != build_download_request(clip)["params"]["download"]["player_id"]
    assert "seq" not in build_download_request(clip)


def test_build_download_request_channel():
    assert build_download_request(CLIP, player_id="x", channel=1)["params"]["download"]["channels"] == [1]


def test_build_stop_download_request():
    assert build_stop_download_request() == {"type": "request", "params": {"method": "do", "stop": "download"}}


def test_build_remux_args(tmp_path):
    ts, mp4 = tmp_path / "a.ts", tmp_path / "a.mp4"
    args = build_remux_args(ts, mp4)
    assert args[-1] == str(mp4)
    assert args[args.index("-i") + 1] == str(ts)
    assert args[args.index("-c:v") + 1] == "copy"
    assert args[args.index("-c:a") + 1] == "aac"
    assert "-y" in args
    assert args.index("-i") < args.index("-c:v")
    assert not Path(args[0]).name.lower().startswith("ffmpeg")


# ------------------------------------------------------------------ fake session
@pytest.fixture(scope="module")
def sample_ts(tmp_path_factory) -> bytes:
    if not FFMPEG:
        pytest.skip("ffmpeg not on PATH")
    out = tmp_path_factory.mktemp("ts") / "clip.ts"
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=8000",
         "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-profile:v", "baseline", "-g", "15",
         "-c:a", "pcm_alaw", "-f", "mpegts", str(out)],
        check=True, timeout=60,
    )
    return out.read_bytes()


def _json_part(obj) -> MediaPart:
    body = json.dumps(obj).encode()
    return MediaPart({"Content-Type": "application/json", "Content-Length": str(len(body))}, body)


def _ts_parts(ts: bytes, n: int = 60) -> list[MediaPart]:
    size = max(188, len(ts) // n // 188 * 188)
    chunks = [ts[i:i + size] for i in range(0, len(ts), size)]
    parts = []
    for i, c in enumerate(chunks, start=1):
        parts.append(MediaPart(
            {"Content-Type": "video/mp2t", "Content-Length": str(len(c)), "X-Data-Sequence": str(i),
             "X-Data-PTS": str((i - 1) * 3000), "X-If-IFrame": "1" if i == 1 else "0", "X-Session-Id": "9"},
            c,
        ))
    return parts


RESPONSE_OK = {"type": "response", "seq": 1, "result": {"error_code": 0, "session_id": "9"}}
FINISHED = {"type": "notification", "params": {"event_type": "stream_status", "status": "finished"}}


class FakeSession:
    def __init__(self, script, *, on_part=None):
        self.script = script
        self.on_part = on_part
        self.sent: list[tuple[dict, dict | None]] = []
        self.connected = False
        self.closed = False
        self._seq = 0

    def connect(self):
        self.connected = True

    def send_json(self, message, *, headers=None):
        msg = dict(message)
        if "seq" not in msg:
            self._seq += 1
            msg["seq"] = self._seq
        self.sent.append((msg, dict(headers) if headers else None))
        return msg["seq"]

    def parts(self):
        assert self.connected, "parts() before connect()"
        assert self.sent, "download request must be sent before reading parts"
        for i, p in enumerate(self.script):
            if self.on_part:
                self.on_part(i, p)
            yield p

    def close(self):
        self.closed = True


def _downloader(session: FakeSession, created: list | None = None):
    def factory(*args, **kwargs):
        if created is not None:
            created.append((args, kwargs))
        return session
    return RecordingDownloader("192.168.1.10", "cloudpass", port=8800, ffmpeg_path=FFMPEG, session_factory=factory)


def _probe(path: Path) -> dict:
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "stream=codec_type,codec_name:format=duration,format_name",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True, timeout=30,
    )
    return json.loads(out.stdout)


@needs_ffmpeg
def test_download_produces_mp4_and_acks_every_25_parts(sample_ts, tmp_path):
    ts_parts = _ts_parts(sample_ts)
    assert len(ts_parts) >= 55
    session = FakeSession([_json_part(RESPONSE_OK), *ts_parts, _json_part(FINISHED)])
    created: list = []
    progress: list[float] = []
    out = tmp_path / "clip.mp4"
    result = _downloader(session, created).download(CLIP, out, progress=progress.append)
    assert result == out
    assert out.is_file() and out.stat().st_size > 1000
    info = _probe(out)
    assert "mp4" in info["format"]["format_name"]
    assert float(info["format"]["duration"]) > 1.0
    assert [s["codec_name"] for s in info["streams"] if s["codec_type"] == "video"] == ["h264"]
    # exactly one download request, first, matching the spec
    first, first_headers = session.sent[0]
    assert first["params"]["method"] == "get"
    dl = first["params"]["download"]
    assert dl["start_time"] == "1735900000" and dl["end_time"] == "1735900120"
    assert dl["media_type"] == 0 and dl["download_type"] == "normal" and dl["channels"] == [0]
    assert sum(1 for m, _ in session.sent if m.get("params", {}).get("download")) == 1
    # flow-control acks for X-Data-Sequence 25 and 50, nothing else
    acks = [(m, h) for m, h in session.sent if m.get("params", {}).get("event_type") == "stream_sequence"]
    assert [h["X-Data-Received"] for _, h in acks] == ["25", "50"]
    for m, h in acks:
        assert m["type"] == "notification"
        assert h["X-Session-Id"] == "9"
    # progress
    assert progress and progress[-1] == 1.0
    assert all(0.0 <= p <= 1.0 for p in progress)
    assert progress == sorted(progress)
    assert session.closed
    assert sorted(p.name for p in tmp_path.iterdir()) == ["clip.mp4"]  # temp .ts removed
    assert created and ("192.168.1.10" in created[0][0] or created[0][1].get("host") == "192.168.1.10")


@needs_ffmpeg
def test_ts_bytes_are_written_in_order(sample_ts, tmp_path):
    """Remuxed video must have every frame of the source: 2 s at 15 fps = 30 frames."""
    session = FakeSession([_json_part(RESPONSE_OK), *_ts_parts(sample_ts), _json_part(FINISHED)])
    out = _downloader(session).download(CLIP, tmp_path / "x.mp4")
    probe = subprocess.run(
        [FFPROBE, "-v", "error", "-count_frames", "-select_streams", "v:0",
         "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(out)],
        capture_output=True, text=True, check=True, timeout=30,
    )
    assert int(probe.stdout.strip()) == 30


@needs_ffmpeg
def test_heartbeat_and_unknown_notifications_are_ignored(sample_ts, tmp_path):
    hb = {"type": "notification", "params": {"event_type": "heartbeat"}}
    parts = _ts_parts(sample_ts, n=10)
    session = FakeSession([_json_part(RESPONSE_OK), _json_part(hb), *parts[:5], _json_part(hb), *parts[5:],
                           _json_part(FINISHED)])
    out = _downloader(session).download(CLIP, tmp_path / "y.mp4")
    assert out.is_file()


@pytest.mark.parametrize(
    ("code", "exc"),
    [(-52407, StreamLimitError), (-52417, TapoDeviceError), (-52402, TapoDeviceError)],
)
def test_error_response_raises_and_leaves_no_file(code, exc, tmp_path):
    session = FakeSession([_json_part({"type": "response", "seq": 1, "result": {"error_code": code}})])
    with pytest.raises(exc):
        _downloader(session).download(CLIP, tmp_path / "e.mp4")
    assert list(tmp_path.iterdir()) == []
    assert session.closed


def test_device_error_carries_code(tmp_path):
    session = FakeSession([_json_part({"type": "response", "seq": 1, "result": {"error_code": -52417}})])
    with pytest.raises(TapoDeviceError) as ei:
        _downloader(session).download(CLIP, tmp_path / "e.mp4")
    assert ei.value.code == -52417


def test_empty_session_id_is_failure(tmp_path):
    session = FakeSession([_json_part({"type": "response", "seq": 1, "result": {"error_code": 0, "session_id": ""}}),
                           _json_part(FINISHED)])
    with pytest.raises(TapoError):
        _downloader(session).download(CLIP, tmp_path / "e.mp4")
    assert list(tmp_path.iterdir()) == []


def test_finished_without_media_is_failure(tmp_path):
    session = FakeSession([_json_part(RESPONSE_OK), _json_part(FINISHED)])
    with pytest.raises(TapoError):
        _downloader(session).download(CLIP, tmp_path / "e.mp4")
    assert list(tmp_path.iterdir()) == []


@needs_ffmpeg
def test_sd_card_offline_is_failure(sample_ts, tmp_path):
    offline = {"type": "notification", "params": {"event_type": "disk_status", "status": "offline"}}
    session = FakeSession([_json_part(RESPONSE_OK), *_ts_parts(sample_ts)[:10], _json_part(offline)])
    with pytest.raises(TapoError):
        _downloader(session).download(CLIP, tmp_path / "e.mp4")
    assert list(tmp_path.iterdir()) == []
    assert session.closed


@needs_ffmpeg
def test_connection_closed_before_finished_is_failure(sample_ts, tmp_path):
    session = FakeSession([_json_part(RESPONSE_OK), *_ts_parts(sample_ts)[:20]])  # iterator just ends
    with pytest.raises(TapoError):
        _downloader(session).download(CLIP, tmp_path / "e.mp4")
    assert list(tmp_path.iterdir()) == []


@needs_ffmpeg
def test_corrupt_ts_remux_failure_is_tapo_error(tmp_path):
    junk = MediaPart({"Content-Type": "video/mp2t", "Content-Length": "376", "X-Data-Sequence": "1"}, b"\x00" * 376)
    session = FakeSession([_json_part(RESPONSE_OK), junk, _json_part(FINISHED)])
    with pytest.raises(TapoError):
        _downloader(session).download(CLIP, tmp_path / "e.mp4")
    assert list(tmp_path.iterdir()) == []


@needs_ffmpeg
def test_cancel_sends_stop_and_removes_partial_files(sample_ts, tmp_path):
    cancel = threading.Event()

    def on_part(i, _p):
        if i == 15:
            cancel.set()

    session = FakeSession([_json_part(RESPONSE_OK), *_ts_parts(sample_ts), _json_part(FINISHED)], on_part=on_part)
    out = tmp_path / "c.mp4"
    try:
        _downloader(session).download(CLIP, out, cancel=cancel)
    except TapoError:
        pass  # raising TapoError on cancel is allowed; returning is allowed too
    assert not out.exists()
    assert list(tmp_path.iterdir()) == []
    assert session.closed
    assert any(m.get("params") == {"method": "do", "stop": "download"} for m, _ in session.sent)


def test_cancel_already_set_does_nothing_harmful(tmp_path):
    cancel = threading.Event()
    cancel.set()
    session = FakeSession([_json_part(RESPONSE_OK), _json_part(FINISHED)])
    try:
        _downloader(session).download(CLIP, tmp_path / "c.mp4", cancel=cancel)
    except TapoError:
        pass
    assert list(tmp_path.iterdir()) == []
    assert session.closed
