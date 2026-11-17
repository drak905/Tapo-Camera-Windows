# Contract: stateful mock Tapo camera + chaos proxy (tests/fixtures/mock_tapo_server)

Pure-Python test infrastructure (stdlib + `cryptography`; FFmpeg allowed only to pre-generate
media). Each server runs in daemon threads, binds 127.0.0.1, uses port 0 (ephemeral) by default.
The mock is the reference implementation of the camera side of `specs/tapo_protocol_spec.json`
and must be strict: wrong tag/seq/cipher/digest -> the error the real camera returns.

```python
from tests.fixtures.mock_tapo_server import MockTapoCamera, DeviceShadow, ChaosProxy

cam = MockTapoCamera(cloud_password="cloudpass", camera_user="camuser", camera_password="campass",
                     secure=True,            # False = legacy MD5 login, plain JSON
                     model="C210", dual_lens=False, max_rtsp_clients=2,
                     https_port=0, rtsp_port=0, onvif_port=0, media_port=0, host="127.0.0.1")
cam.start(); cam.stop()                     # also usable as context manager
cam.host; cam.https_port; cam.rtsp_port; cam.onvif_port; cam.media_port
cam.rtsp_url(quality="stream2")             # rtsp://camuser:campass@127.0.0.1:<port>/stream2
cam.state -> DeviceShadow                   # live, mutable, thread-safe
    # privacy: bool, led: bool, inf_type: str, night_vision_mode: str, motion_enabled: bool,
    # motion_sensitivity: int, alarm_active: bool, ptz_x: int, ptz_y: int,
    # presets: dict[str, dict], recordings: list[RecordingClip]  (seeded with 3 clips today UTC,
    # 1 clip yesterday), components: dict[str, int]
cam.request_log -> list[dict]               # decrypted {"method","params"} of every inner request
cam.login_count -> int
cam.expire_sessions()                       # next request with current stok -> -40401
cam.inject_token_expiry(every_n: int)       # every Nth request returns -40401 (0 disables)
cam.inject_error(method: str, code: int, times: int = 1)   # per-item error_code for that method
cam.inject_http_status(status: int, times: int = 1)        # e.g. 500 / 401 on HTTPS
cam.inject_rtsp_busy(times: int = 1)        # next RTSP DESCRIBE -> 453 Not Enough Bandwidth
cam.rtsp_active_clients -> int              # currently PLAYing sessions (all paths)
cam.rtsp_reject_count -> int                # connections refused because of max_rtsp_clients
cam.drop_rtsp_connections()                 # TCP RST every RTSP client
cam.set_rtsp_corrupt_audio(enabled: bool)   # send truncated/garbled PCMA RTP packets
cam.emit_motion_event(active: bool = True)  # queued for ONVIF PullMessages
cam.lock_login(seconds: float)              # login -> -40404 for that period
```
Protocol surfaces
- HTTPS (self-signed cert generated at start; `cam.cert_fingerprint` = sha256 hex lowercase of DER):
  secure login per [spec:auth.*] incl. -40413 challenge, device_confirm, digest_passwd, stok,
  start_seq, `seq`/`tapo_tag` header check (bad tag -> -40401 or HTTP 401 as spec says),
  securePassthrough, legacy login when `secure=False`; `multipleRequest` dispatcher implementing
  every command in docs/CONTRACT_CORE.md dtos against the DeviceShadow; unknown method -> -40106.
  `cam.rotate_certificate()` serves a new cert (for TOFU mismatch tests).
- RTSP (TCP, RTP/AVP/TCP interleaved only; also answer UDP SETUP with 461): OPTIONS, DESCRIBE (SDP:
  H.264 payload 96 + PCMA payload 8 at 8000 Hz), SETUP, PLAY, TEARDOWN, GET_PARAMETER keep-alive;
  Digest or Basic auth with camera account (401 otherwise); paths /stream1 (1280x720),
  /stream2 (640x360), /stream6, /stream7 only when dual_lens; ~15 fps real-time pacing from a
  pre-encoded looping H.264 clip (generated once with ffmpeg into a temp cache dir, keyframe every
  second, baseline, no B-frames); enforces `max_rtsp_clients` (extra client -> 453 at DESCRIBE/SETUP).
  Must be consumable by `ffmpeg -rtsp_transport tcp -i <url>`.
- ONVIF (HTTP SOAP 1.2 on `onvif_port`, paths /onvif/device_service, /onvif/service): WS-Security
  UsernameToken digest validated with camera account (else SOAP Fault NotAuthorized);
  GetDeviceInformation, GetCapabilities, GetProfiles, ContinuousMove (updates ptz_x/ptz_y), Stop,
  GetPresets, GotoPreset, CreatePullPointSubscription, PullMessages (long-poll up to Timeout,
  returns queued CellMotionDetector/Motion notifications with IsMotion true/false), Renew, Unsubscribe.
  `cam.ws_discovery_responder()` optional helper: parse a Probe and return a ProbeMatches payload (bytes).
- Media :8800 (`media_port`): POST /stream with HTTP Digest per [spec:stream.local8800.handshake]
  (user "admin", cloud password transformed per challenge), Key-Exchange header, multipart
  `--device-stream-boundary--` parts, AES-128-CBC per part per [spec:stream.local8800.encryption.md5];
  supports the download request [spec:vod.download.session] for clips in `state.recordings`
  (streams MPEG-TS bytes from the cached test clip, then a `stream_status` finished notification)
  and returns -52407 / HTTP 503 beyond 2 concurrent sessions.

ChaosProxy (TCP-level fault injection between client and any mock surface)
```python
proxy = ChaosProxy(target_host, target_port, listen_port=0); proxy.start(); proxy.stop()
proxy.port
proxy.reset_all()                 # RST all current connections (SO_LINGER 0)
proxy.blackhole(seconds: float)   # accept nothing / forward nothing for the period, then recover
proxy.set_refuse(enabled: bool)   # refuse new connections (simulates Wi-Fi down)
proxy.set_latency(ms: int)        # per-chunk delay
proxy.connection_count -> int; proxy.bytes_forwarded -> int
```
pytest fixtures (tests/conftest.py): `mock_camera` (secure, started, function scope),
`legacy_mock_camera`, `dual_lens_camera`, `chaos_proxy` (factory: `chaos_proxy(host, port) -> ChaosProxy`,
all stopped at teardown), `ffmpeg_path` (skip if ffmpeg missing), `tmp_data_dir` (sets
`TAPO_DESKTOP_HOME` to a tmp dir). Self-check tests: `tests/integration/test_fixture_selfcheck.py`
(drives every surface with raw sockets / ffmpeg / ffprobe, independent of `tapo_desktop`).
