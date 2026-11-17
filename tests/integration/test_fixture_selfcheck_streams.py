"""Self-check of the mock camera's RTSP, :8800 media and ChaosProxy surfaces (raw sockets, ffmpeg, ffprobe)."""
from __future__ import annotations

import json
import re
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone

import pytest

from tests.fixtures.mock_tapo_server import MockTapoCamera
from tests.fixtures.raw_clients import MediaRaw, RtspRaw, SecureClient, mem_socketpair

pytestmark = pytest.mark.integration


def _ffmpeg_null(ffmpeg: str, url: str, seconds: int = 3, timeout: float = 20) -> subprocess.CompletedProcess:
    return subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-stats", "-threads", "1",
                           "-rtsp_transport", "tcp",
                           "-i", url, "-t", str(seconds), "-f", "null", "-"],
                          capture_output=True, text=True, timeout=timeout)


def _frames(stderr: str) -> int:
    m = re.findall(r"frame=\s*(\d+)", stderr)
    return int(m[-1]) if m else 0


# ------------------------------------------------------------------------------------------
# RTSP
# ------------------------------------------------------------------------------------------
def test_rtsp_handshake_auth_and_sdp(mock_camera):
    cam = mock_camera
    c = RtspRaw(cam.host, cam.rtsp_port)                    # no credentials
    st, h, _ = c.request("OPTIONS", c.url("stream1"))
    assert st == 200 and "DESCRIBE" in h["public"][0]
    st, h, _ = c.request("DESCRIBE", c.url("stream1"))
    assert st == 401 and any(w.startswith("Digest") for w in h["www-authenticate"])
    c.close()
    bad = RtspRaw(cam.host, cam.rtsp_port, cam.camera_user, "wrong")
    assert bad.request("DESCRIBE", bad.url("stream1"))[0] == 401
    bad.close()
    c = RtspRaw(cam.host, cam.rtsp_port, cam.camera_user, cam.camera_password)
    st, h, sdp = c.request("DESCRIBE", c.url("stream1"), {"Accept": "application/sdp"})
    assert st == 200 and h["content-type"] == ["application/sdp"]
    text = sdp.decode()
    assert "m=video 0 RTP/AVP 96" in text and "a=rtpmap:96 H264/90000" in text and "sprop-parameter-sets=" in text
    assert "m=audio 0 RTP/AVP 8" in text and "a=rtpmap:8 PCMA/8000" in text
    st, _, _ = c.request("SETUP", c.url("stream1") + "/track1", {"Transport": "RTP/AVP;unicast;client_port=5000-5001"})
    assert st == 461                                        # UDP unsupported
    assert c.request("DESCRIBE", c.url("stream9"))[0] == 404
    assert c.request("DESCRIBE", c.url("stream6"))[0] == 404  # single-lens camera
    c.close()
    b = RtspRaw(cam.host, cam.rtsp_port, cam.camera_user, cam.camera_password)
    b.auth = ("basic", {})
    assert b.request("DESCRIBE", b.url("stream2"))[0] == 200
    b.close()


def test_rtsp_raw_play_delivers_paced_rtp(mock_camera):
    cam = mock_camera
    c = RtspRaw(cam.host, cam.rtsp_port, cam.camera_user, cam.camera_password)
    c.play("stream2")
    assert cam.rtsp_active_clients == 1
    t0 = time.monotonic()
    pkts = c.read_rtp(400, timeout=6)
    elapsed = time.monotonic() - t0
    video = [p for ch, p in pkts if ch == 0]
    audio = [p for ch, p in pkts if ch == 2]
    assert video and audio
    assert all(p[0] == 0x80 and (p[1] & 0x7F) == 96 for p in video)
    assert all((p[1] & 0x7F) == 8 and len(p) == 12 + 160 for p in audio)
    vts = sorted({int.from_bytes(p[4:8], "big") for p in video})
    # 15 fps -> 6000 ticks per frame; real-time pacing: elapsed ~ frames / 15
    assert {b - a for a, b in zip(vts, vts[1:])} == {6000}
    assert elapsed >= (len(vts) - 1) / 15 - 0.3
    seqs = [int.from_bytes(p[2:4], "big") for p in video]
    assert all(((b - a) & 0xFFFF) == 1 for a, b in zip(seqs, seqs[1:]))
    st, _, _ = c.request("GET_PARAMETER", c.url("stream2"))
    assert st == 200
    st, _, _ = c.request("TEARDOWN", c.url("stream2"))
    assert st == 200
    deadline = time.monotonic() + 2
    while cam.rtsp_active_clients and time.monotonic() < deadline:
        time.sleep(0.05)
    assert cam.rtsp_active_clients == 0
    c.close()


@pytest.mark.parametrize("path,size", [("stream1", "1280x720"), ("stream2", "640x360")])
def test_rtsp_ffmpeg_and_ffprobe_decode(mock_camera, ffmpeg_path, ffprobe_path, path, size):
    url = mock_camera.rtsp_url(path)
    p = _ffmpeg_null(ffmpeg_path, url, 3)
    errors = [ln for ln in p.stderr.splitlines() if not ln.startswith("frame=") and ln.strip() and "frame=" not in ln]
    assert p.returncode == 0, p.stderr[-1500:]
    assert not errors, errors
    assert _frames(p.stderr) >= 40                          # ~15 fps x 3 s
    probe = subprocess.run([ffprobe_path, "-v", "error", "-rtsp_transport", "tcp", "-show_entries",
                            "stream=codec_type,codec_name,width,height,sample_rate", "-of", "json", url],
                           capture_output=True, text=True, timeout=20)
    assert probe.returncode == 0, probe.stderr
    streams = json.loads(probe.stdout)["streams"]
    assert len(streams) == 2
    v = next(s for s in streams if s["codec_type"] == "video")
    a = next(s for s in streams if s["codec_type"] == "audio")
    assert v["codec_name"] == "h264" and f"{v['width']}x{v['height']}" == size
    assert a["codec_name"] == "pcm_alaw" and a["sample_rate"] == "8000"


def test_rtsp_dual_lens_paths(dual_lens_camera, ffmpeg_path):
    cam = dual_lens_camera
    c = RtspRaw(cam.host, cam.rtsp_port, cam.camera_user, cam.camera_password)
    for path in ("stream1", "stream2", "stream6", "stream7"):
        assert c.request("DESCRIBE", c.url(path))[0] == 200, path
    c.close()
    p = _ffmpeg_null(ffmpeg_path, cam.rtsp_url("stream7"), 2)
    assert p.returncode == 0 and _frames(p.stderr) >= 20, p.stderr[-800:]


def test_rtsp_max_clients_and_busy_injection(mock_camera):
    cam = mock_camera
    a = RtspRaw(cam.host, cam.rtsp_port, cam.camera_user, cam.camera_password)
    b = RtspRaw(cam.host, cam.rtsp_port, cam.camera_user, cam.camera_password)
    a.play("stream2")
    b.play("stream1")
    assert cam.rtsp_active_clients == 2
    c = RtspRaw(cam.host, cam.rtsp_port, cam.camera_user, cam.camera_password)
    assert c.request("DESCRIBE", c.url("stream2"))[0] == 453
    assert cam.rtsp_reject_count == 1
    a.request("TEARDOWN", a.url("stream2"))
    assert c.request("DESCRIBE", c.url("stream2"))[0] == 200
    c.close()
    b.close()
    a.close()
    deadline = time.monotonic() + 3
    while cam.rtsp_active_clients and time.monotonic() < deadline:
        time.sleep(0.05)
    assert cam.rtsp_active_clients == 0
    cam.inject_rtsp_busy()
    d = RtspRaw(cam.host, cam.rtsp_port, cam.camera_user, cam.camera_password)
    assert d.request("DESCRIBE", d.url("stream2"))[0] == 453
    assert d.request("DESCRIBE", d.url("stream2"))[0] == 200
    assert cam.rtsp_reject_count == 2
    d.close()


def test_rtsp_drop_connections_resets_clients(mock_camera):
    cam = mock_camera
    c = RtspRaw(cam.host, cam.rtsp_port, cam.camera_user, cam.camera_password)
    c.play("stream2")
    c.read_rtp(5)
    cam.drop_rtsp_connections()
    with pytest.raises((ConnectionError, OSError)):
        for _ in range(200):
            c._recv()
    deadline = time.monotonic() + 3
    while cam.rtsp_active_clients and time.monotonic() < deadline:
        time.sleep(0.05)
    assert cam.rtsp_active_clients == 0
    c.close()


def test_rtsp_corrupt_audio_keeps_video_decodable(mock_camera, ffmpeg_path):
    mock_camera.set_rtsp_corrupt_audio(True)
    c = RtspRaw(mock_camera.host, mock_camera.rtsp_port, mock_camera.camera_user, mock_camera.camera_password)
    c.play("stream2")
    audio = [p for ch, p in c.read_rtp(300, timeout=6) if ch == 2]
    assert audio and all(12 < len(p) < 12 + 160 for p in audio)
    seqs = [int.from_bytes(p[2:4], "big") for p in audio]
    assert any(((b - a) & 0xFFFF) == 2 for a, b in zip(seqs, seqs[1:]))   # dropped packets
    c.close()
    p = subprocess.run([ffmpeg_path, "-hide_banner", "-nostdin", "-loglevel", "error", "-stats", "-threads", "1", "-rtsp_transport", "tcp",
                        "-i", mock_camera.rtsp_url("stream2"), "-t", "3", "-map", "0:v:0", "-f", "null", "-"],
                       capture_output=True, text=True, timeout=20)
    assert p.returncode == 0 and _frames(p.stderr) >= 40, p.stderr[-800:]


# ------------------------------------------------------------------------------------------
# :8800 media [spec:stream.local8800.*], [spec:vod.download.session]
# Each test runs over an in-memory socket pair ("mem", always) and over real loopback TCP
# ("tcp", skipped when a local HTTP filter such as AdGuard hijacks loopback HTTP streams).
# ------------------------------------------------------------------------------------------
class _MediaConns:
    def __init__(self, cam: MockTapoCamera, transport: str):
        self.cam, self.transport = cam, transport
        self.handlers: list[tuple[threading.Thread, object]] = []
        self.clients: list[MediaRaw] = []

    def open(self) -> MediaRaw:
        cam = self.cam
        if self.transport == "tcp":
            m = MediaRaw(cam.host, cam.media_port)
        else:
            client, server = mem_socketpair()
            server.settimeout(0.5)
            t = threading.Thread(target=cam._media._handle, args=(server, ("mem", 0)), name="selfcheck-mem-media",
                                 daemon=True)
            t.start()
            self.handlers.append((t, server))
            m = MediaRaw(cam.host, cam.media_port, sock=client)
        self.clients.append(m)
        return m

    def close(self) -> None:
        for m in self.clients:
            m.close()
        for t, s in self.handlers:
            s.close()
            t.join(3)


@pytest.fixture(params=["mem", "tcp"])
def media_transport(request) -> str:
    if request.param == "tcp":
        request.getfixturevalue("require_loopback_http_stream")
    return request.param


@pytest.fixture
def media_conns(mock_camera, media_transport):
    conns = _MediaConns(mock_camera, media_transport)
    yield conns
    conns.close()


def _download_req(clip) -> dict:
    return {"method": "get", "download": {"client_id": 1, "channels": [0], "media_type": 0,
                                          "start_time": str(int(clip.start.timestamp())),
                                          "end_time": str(int(clip.end.timestamp())),
                                          "event_type": [clip.event_type], "download_type": "normal", "player_id": "selfcheck"}}


def _run_download(m: MediaRaw, clip, ack: bool = True) -> tuple[bytes, list[dict], list[dict]]:
    seq = m.request(_download_req(clip))
    h, body = m.read_part()
    resp = json.loads(body)
    assert resp["type"] == "response" and resp["seq"] == seq and resp["result"]["error_code"] == 0, resp
    m.session_id = resp["result"]["session_id"]
    ts, media_headers, notes = b"", [], []
    while True:
        h, body = m.read_part()
        if h["content-type"] == "application/json":
            msg = json.loads(body)
            notes.append(msg)
            if msg["params"].get("event_type") in ("stream_status", "connection_closed"):
                break
            continue
        media_headers.append(h)
        ts += body
        n = int(h["x-data-sequence"])
        if ack and n % 25 == 0:
            m.send_part({"type": "notification", "params": {"event_type": "stream_sequence"}}, {"X-Data-Received": str(n)})
    return ts, media_headers, notes


@pytest.mark.parametrize("secure,algorithm", [(True, "MD5"), (True, "SHA-256"), (False, "MD5")])
def test_media_download_digest_keyexchange_acks(secure, algorithm, media_transport, ffprobe_path, tmp_path):
    cam = MockTapoCamera(secure=secure)
    cam.media_digest_algorithm = algorithm
    with cam:
        conns = _MediaConns(cam, media_transport)
        try:
            clip = cam.state.recordings[1]
            m = conns.open()
            st, h, _ = m.connect(cam.cloud_password, encrypt_v3=secure)
            assert st == 200 and "key-exchange" in h and h["content-type"].startswith("multipart/mixed")
            ts, mh, notes = _run_download(m, clip)
            assert notes[-1] == {"type": "notification", "params": {"event_type": "stream_status", "status": "finished"}}
            assert len(mh) > 50 and all(x["x-if-encrypt"] == "1" and x["content-type"] == "video/mp2t" for x in mh)
            assert [int(x["x-data-sequence"]) for x in mh] == list(range(1, len(mh) + 1))
            assert all(x["x-session-id"] == m.session_id for x in mh)
            assert cam.media_acks[:2] == [25, 50]
            assert ts[0] == 0x47 and len(ts) % 188 == 0
            out = tmp_path / "dl.ts"
            out.write_bytes(ts)
            probe = subprocess.run([ffprobe_path, "-v", "error", "-show_entries", "stream=codec_name", "-of", "json",
                                    str(out)], capture_output=True, text=True, timeout=20)
            assert probe.returncode == 0 and "h264" in probe.stdout, probe.stderr
            assert any(e["body"] and e["body"].get("params", {}).get("download") for e in cam.media_request_log)
        finally:
            conns.close()


def test_media_rejects_bad_password_unknown_clip_and_third_session(mock_camera, media_conns):
    cam = mock_camera
    m = media_conns.open()
    st, _, _ = m.connect("wrong-password", encrypt_v3=True)
    assert st == 401 and cam.media_auth_failures == 1
    a, b, c = (media_conns.open() for _ in range(3))
    assert a.connect(cam.cloud_password, True)[0] == 200
    assert b.connect(cam.cloud_password, True)[0] == 200
    st, _, body = c.connect(cam.cloud_password, True)
    assert st == 503 and json.loads(body)["error_code"] == -52407
    seq = a.request({"method": "get", "download": {"client_id": 1, "channels": [0], "media_type": 0,
                                                   "start_time": "1000", "end_time": "1060"}})
    _, body = a.read_part()
    assert json.loads(body) == {"type": "response", "seq": seq, "result": {"error_code": -52402}}
    for x in (a, b, c):
        x.close()
    deadline = time.monotonic() + 2
    while cam.media_active_sessions and time.monotonic() < deadline:
        time.sleep(0.05)
    assert cam.media_active_sessions == 0


def test_media_without_acks_stalls_then_closes(mock_camera, media_conns):
    cam = mock_camera
    cam.media_ack_timeout = 0.5
    m = media_conns.open()
    assert m.connect(cam.cloud_password, True)[0] == 200
    t0 = time.monotonic()
    _, mh, notes = _run_download(m, cam.state.recordings[0], ack=False)
    assert len(mh) == cam.media_ack_window and notes[-1]["params"]["event_type"] == "connection_closed"
    assert time.monotonic() - t0 >= 0.5
    with pytest.raises((ConnectionError, OSError)):
        for _ in range(50):
            m._recv()


def test_media_stop_request(mock_camera, media_conns):
    cam = mock_camera
    m = media_conns.open()
    assert m.connect(cam.cloud_password, True)[0] == 200
    seq = m.request(_download_req(cam.state.recordings[0]))
    _, body = m.read_part()
    m.session_id = json.loads(body)["result"]["session_id"]
    m.read_part()
    stop_seq = m.request({"method": "do", "stop": "download"})
    while True:
        h, body = m.read_part()
        if h["content-type"] == "application/json":
            msg = json.loads(body)
            if msg.get("type") == "response":
                assert msg == {"type": "response", "seq": stop_seq, "result": {"error_code": 0}} and seq < stop_seq
                break


# ------------------------------------------------------------------------------------------
# ChaosProxy
# ------------------------------------------------------------------------------------------
def _https_via(cam, port) -> SecureClient:
    c = SecureClient(cam.host, port, cam.cloud_password)
    c.conn.timeout = 3
    return c


@pytest.mark.chaos
def test_chaos_proxy_forwards_and_resets(mock_camera, chaos_proxy):
    cam = mock_camera
    proxy = chaos_proxy(cam.host, cam.https_port)
    c = _https_via(cam, proxy.port)
    c.login()
    led = {"method": "getLedStatus", "params": {"led": {"name": ["config"]}}}
    assert c.multi(led)[0]["error_code"] == 0
    assert proxy.connection_count == 1 and proxy.bytes_forwarded > 1000
    proxy.reset_all()
    with pytest.raises((ConnectionError, OSError, Exception)):
        c.multi(led)
    c.conn.close()
    c.login()                                                # reconnects through the proxy
    assert c.multi(led)[0]["error_code"] == 0
    assert proxy.connection_count == 2


@pytest.mark.chaos
def test_chaos_proxy_refuse_blackhole_latency(mock_camera, chaos_proxy):
    cam = mock_camera
    proxy = chaos_proxy(cam.host, cam.rtsp_port)
    proxy.set_refuse(True)
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection((cam.host, proxy.port), timeout=5)  # Windows loopback: ~2 s
    proxy.set_refuse(False)
    s = socket.create_connection((cam.host, proxy.port), timeout=2)
    s.sendall(b"OPTIONS * RTSP/1.0\r\nCSeq: 1\r\n\r\n")
    assert s.recv(4096).startswith(b"RTSP/1.0 200")
    proxy.blackhole(0.8)
    t0 = time.monotonic()
    s.sendall(b"OPTIONS * RTSP/1.0\r\nCSeq: 2\r\n\r\n")
    s.settimeout(0.4)
    with pytest.raises(TimeoutError):
        s.recv(4096)
    s.settimeout(3)
    data = s.recv(4096)                                      # delivered after the blackhole ends
    assert data.startswith(b"RTSP/1.0 200") and time.monotonic() - t0 >= 0.7
    proxy.set_latency(300)
    t0 = time.monotonic()
    s.sendall(b"OPTIONS * RTSP/1.0\r\nCSeq: 3\r\n\r\n")
    assert s.recv(4096).startswith(b"RTSP/1.0 200")
    assert time.monotonic() - t0 >= 0.55                     # one delay per direction
    proxy.set_latency(0)
    s.close()


@pytest.mark.chaos
def test_chaos_proxy_rtsp_stream_survives_latency_and_reset(mock_camera, chaos_proxy, ffmpeg_path):
    cam = mock_camera
    proxy = chaos_proxy(cam.host, cam.rtsp_port)
    url = f"rtsp://{cam.camera_user}:{cam.camera_password}@{cam.host}:{proxy.port}/stream2"
    p = _ffmpeg_null(ffmpeg_path, url, 2)
    assert p.returncode == 0 and _frames(p.stderr) >= 20, p.stderr[-800:]
    c = RtspRaw(cam.host, proxy.port, cam.camera_user, cam.camera_password)
    c.play("stream2")
    c.read_rtp(10)
    proxy.reset_all()
    with pytest.raises((ConnectionError, OSError)):
        for _ in range(200):
            c._recv()
    deadline = time.monotonic() + 3
    while cam.rtsp_active_clients and time.monotonic() < deadline:
        time.sleep(0.05)
    assert cam.rtsp_active_clients == 0                      # camera noticed the reset and freed the slot
    c.close()


def test_recordings_are_utc_and_match_media_clip_lookup(mock_camera):
    for clip in mock_camera.state.recordings:
        assert clip.start.tzinfo is not None and clip.start.utcoffset().total_seconds() == 0
        assert clip.end > clip.start
    assert datetime.now(timezone.utc).date() in {c.start.date() for c in mock_camera.state.recordings}
