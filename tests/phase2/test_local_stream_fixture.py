"""Self-check of the mock camera's :8800 live preview (lane P2-live fixture extension).

Driven with the production ``MediaSession`` (handshake + decryption) and a small TS parser, so the
supervisor tests in test_local_stream*.py can rely on: encrypted preview parts, HD/VGA clip sizes,
continuous PTS / continuity counters across clip loops, keyframe-aligned start, flow control,
``do stop``, ``drop_media_connections()`` and the 2-session limit.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time

import pytest

from tapo_desktop.core.errors import StreamLimitError, TapoConnectionError
from tapo_desktop.core.vod.session import MediaSession
from tests.fixtures.mock_tapo_server import ts_loop

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]


def _preview(resolution: str = "VGA") -> dict:
    return {"type": "request", "params": {"method": "get", "preview": {
        "channels": [0], "resolutions": [resolution], "audio": ["default"]}}}


def _ack(session: MediaSession, seq: int) -> None:
    session.send_json({"type": "notification", "params": {"event_type": "stream_sequence"}},
                      headers={"X-Data-Received": str(seq), "X-Session-Id": session.session_id or ""})


def _collect(cam, resolution: str, n_parts: int, *, ack: bool = True, timeout: float = 20.0):
    """Open a session, request a preview, return (response, media parts) after ``n_parts`` parts."""
    session = MediaSession(cam.host, cam.cloud_password, port=cam.media_port, timeout=timeout)
    session.connect()
    try:
        session.send_json(_preview(resolution))
        response, media = None, []
        for part in session.parts():
            if "json" in part.headers.get("Content-Type", ""):
                if response is None:
                    response = json.loads(part.body)
                continue
            media.append(part)
            seq = int(part.headers["X-Data-Sequence"])
            if ack and seq % 25 == 0:
                _ack(session, seq)
            if len(media) >= n_parts:
                break
        return response, media
    finally:
        session.close()


def _ts_stats(data: bytes, video_pid: int = 256) -> tuple[list[int], int]:
    """(video PTS list in stream order, continuity errors over all PIDs with payload)."""
    pts, errors, last_cc = [], 0, {}
    for i in range(0, len(data) - 187, 188):
        p = data[i:i + 188]
        assert p[0] == 0x47
        pk = ts_loop.parse_packet(data, i)
        cc = p[3] & 0x0F
        if pk.has_payload:
            prev = last_cc.get(pk.pid)
            if prev is not None and cc != (prev + 1) & 0x0F:
                errors += 1
            last_cc[pk.pid] = cc
        if pk.pid == video_pid and pk.pts >= 0:
            pts.append(ts_loop.read_pts(data, i + pk.pts))
    return pts, errors


def _probe_size(ffprobe_path: str, data: bytes) -> str:
    out = subprocess.run([ffprobe_path, "-v", "error", "-f", "mpegts", "-i", "pipe:0", "-select_streams", "v:0",
                          "-show_entries", "stream=width,height", "-of", "csv=p=0"],
                         input=data, capture_output=True, timeout=30)
    return out.stdout.decode().strip().splitlines()[0]


@pytest.mark.parametrize("resolution,size", [("VGA", "640,360"), ("HD", "1280,720")])
def test_preview_parts_are_encrypted_ts_of_requested_resolution(mock_camera, ffprobe_path, resolution, size):
    mock_camera.media_preview_speed = 20.0
    response, media = _collect(mock_camera, resolution, 30)
    assert response["result"]["error_code"] == 0 and response["result"]["session_id"]
    assert all(p.headers["X-If-Encrypt"] == "1" and p.headers["Content-Type"] == "video/mp2t" for p in media)
    assert media[0].headers["X-If-IFrame"] == "1", "preview must start on a keyframe"
    assert [int(p.headers["X-Data-Sequence"]) for p in media] == list(range(1, 31))
    assert _probe_size(ffprobe_path, b"".join(p.body for p in media)) == size
    assert mock_camera.media_preview_requests == 1


def test_preview_loops_with_continuous_pts_and_continuity_counters(mock_camera):
    mock_camera.media_preview_speed = 50.0
    _, media = _collect(mock_camera, "VGA", 200)  # 90 frames per clip loop -> > 2 loops
    pts, cc_errors = _ts_stats(b"".join(p.body for p in media))
    assert len(pts) == 200
    steps = {b - a for a, b in zip(pts, pts[1:])}
    assert steps == {6000}, f"video PTS must advance one frame (6000) per part, got steps {sorted(steps)[:5]}"
    assert cc_errors == 0
    header_pts = [int(p.headers["X-Data-PTS"]) for p in media]
    assert header_pts == pts
    keyframes = [i for i, p in enumerate(media) if p.headers["X-If-IFrame"] == "1"]
    assert keyframes[:8] == [0, 15, 30, 45, 60, 75, 90, 105], "keyframe every second, loop keyframe-aligned"


def test_preview_is_paced_in_real_time(mock_camera):
    t0 = time.monotonic()
    _, media = _collect(mock_camera, "VGA", 31)  # 30 frame intervals = 2.0 s of stream time
    elapsed = time.monotonic() - t0
    assert 1.6 < elapsed < 4.0, f"31 parts took {elapsed:.2f} s"


def test_preview_without_acks_stalls_then_camera_closes(mock_camera):
    mock_camera.media_preview_speed = 50.0
    mock_camera.media_ack_timeout = 1.0
    session = MediaSession(mock_camera.host, mock_camera.cloud_password, port=mock_camera.media_port, timeout=10)
    session.connect()
    media, notes = 0, []
    try:
        session.send_json(_preview())
        for part in session.parts():
            if "json" in part.headers.get("Content-Type", ""):
                notes.append(part.body)
            else:
                media += 1
    except TapoConnectionError:
        pass
    finally:
        session.close()
    assert media == mock_camera.media_ack_window
    assert any(b"connection_closed" in n for n in notes)


def test_preview_stop_request_ends_stream(mock_camera):
    mock_camera.media_preview_speed = 5.0
    session = MediaSession(mock_camera.host, mock_camera.cloud_password, port=mock_camera.media_port, timeout=5)
    session.connect()
    try:
        session.send_json(_preview())
        parts = session.parts()
        while "json" in next(parts).headers.get("Content-Type", ""):
            pass
        session.send_json({"type": "request", "params": {"method": "do", "stop": "null"}})
        sent = mock_camera.media_parts_sent
        time.sleep(0.8)
        assert mock_camera.media_parts_sent - sent <= 2
    finally:
        session.close()


def test_drop_media_connections_resets_preview(mock_camera):
    session = MediaSession(mock_camera.host, mock_camera.cloud_password, port=mock_camera.media_port, timeout=5)
    session.connect()
    session.send_json(_preview())
    errors: list[BaseException] = []
    ended = threading.Event()

    def reader():
        try:
            for _ in session.parts():
                pass
        except TapoConnectionError as exc:
            errors.append(exc)
        ended.set()

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    time.sleep(0.5)
    assert mock_camera.media_active_sessions == 1
    mock_camera.drop_media_connections()
    assert ended.wait(3), "client did not notice the dropped connection"
    session.close()
    t.join(2)
    deadline = time.monotonic() + 3
    while mock_camera.media_active_sessions and time.monotonic() < deadline:
        time.sleep(0.05)
    assert mock_camera.media_active_sessions == 0


def test_third_preview_session_is_refused(mock_camera):
    held = []
    try:
        for _ in range(2):
            s = MediaSession(mock_camera.host, mock_camera.cloud_password, port=mock_camera.media_port, timeout=5)
            s.connect()
            s.send_json(_preview())
            held.append(s)
        third = MediaSession(mock_camera.host, mock_camera.cloud_password, port=mock_camera.media_port, timeout=5)
        with pytest.raises(StreamLimitError):
            third.connect()
    finally:
        for s in held:
            s.close()


def test_invalid_preview_resolution_rejected(mock_camera):
    session = MediaSession(mock_camera.host, mock_camera.cloud_password, port=mock_camera.media_port, timeout=5)
    session.connect()
    try:
        session.send_json(_preview("4K"))
        part = next(session.parts())
        assert b"-52402" in part.body
    finally:
        session.close()
