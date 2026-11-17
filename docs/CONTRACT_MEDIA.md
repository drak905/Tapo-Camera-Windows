# Contract: media engine (RTSP via FFmpeg) and SD-card VOD (:8800)

## `tapo_desktop.core.media_engine.profiles`  [spec:stream.rtsp.*]
```python
def build_rtsp_url(host: str, username: str, password: str, quality: StreamQuality = StreamQuality.HD, port: int = 554) -> str
    # rtsp://<urlquoted user>:<urlquoted pass>@host:port/stream1 ; IPv6 hosts bracketed
def redact_url(url: str) -> str     # replaces password with ***
```

## `tapo_desktop.core.media_engine.limiter`
```python
class StreamLimiter:                       # camera firmware allows max 2 concurrent RTSP sessions
    def __init__(self, max_per_host: int = 2)
    def acquire(self, host: str, timeout: float = 0.0) -> "StreamLease"   # raises StreamLimitError
    def active(self, host: str) -> int
class StreamLease:                          # context manager; release() idempotent
    def release(self) -> None
```

## `tapo_desktop.core.media_engine.job_object`
```python
class JobObject:                            # Windows Job Object, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE (ctypes)
    def __init__(self, name: str | None = None)
    def assign(self, proc: subprocess.Popen | int) -> None
    def terminate_all(self, exit_code: int = 1) -> None
    def close(self) -> None                 # closing the last handle kills every assigned process
    @property
    def handle(self) -> int
def default_job() -> JobObject             # process-wide singleton; children die with the app
```

## `tapo_desktop.core.media_engine.ffmpeg`
```python
def find_ffmpeg() -> str                   # env TAPO_FFMPEG, then ffmpeg.exe beside sys.executable / in _MEIPASS, then PATH; FileNotFoundError
def build_ffmpeg_args(url: str, width: int, height: int, *, hwaccel: str | None = "d3d11va",
                      transport: str = "tcp", fps: int | None = None, audio_url: str | None = None) -> list[str]
    # includes: -rtsp_transport tcp -fflags nobuffer -flags low_delay [-hwaccel d3d11va] -i URL
    #   -map 0:v:0 -vf scale=W:H:force_original_aspect_ratio=decrease,pad=W:H:(ow-iw)/2:(oh-ih)/2
    #   -pix_fmt rgb24 -f rawvideo pipe:1 ; if audio_url: -map 0:a:0? -ac 1 -ar 8000 -f s16le <audio_url>
    # never includes the password in any log message (use redact_url)
```

## `tapo_desktop.core.media_engine.alaw`
```python
def alaw_to_pcm16(data: bytes) -> bytes    # G.711 A-law -> s16le, any length (truncated input OK)
def pcm16_to_alaw(data: bytes) -> bytes    # odd trailing byte ignored
```

## `tapo_desktop.core.media_engine.supervisor`
```python
class StreamState(enum.Enum): IDLE, CONNECTING, PLAYING, RECONNECTING, STOPPED, FAILED
@dataclass(frozen=True)
class Frame: width: int; height: int; data: bytes; index: int; timestamp: float   # rgb24, time.monotonic()
class StreamSupervisor:
    def __init__(self, url: str, width: int = 640, height: int = 360, *,
                 on_frame: Callable[[Frame], None],
                 on_state: Callable[[StreamState, str | None], None] | None = None,
                 on_audio: Callable[[bytes], None] | None = None,      # s16le 8 kHz mono chunks; None = no audio
                 ffmpeg_path: str | None = None, hwaccel: str = "auto", # "auto": d3d11va then software fallback
                 limiter: StreamLimiter | None = None, limiter_key: str | None = None,
                 job: JobObject | None = None,                          # default_job() when None
                 backoff_initial: float = 0.5, backoff_max: float = 10.0,
                 stall_timeout: float = 5.0, max_retries: int | None = None)
    def start(self) -> None                 # non-blocking; worker thread owns the ffmpeg process
    def stop(self, timeout: float = 3.0) -> None   # kills ffmpeg, joins threads, releases lease; idempotent
    def switch(self, url: str, width: int, height: int) -> None   # e.g. stream2 grid -> stream1 fullscreen
    @property
    def state(self) -> StreamState
    @property
    def pid(self) -> int | None
    def stats(self) -> dict                 # {"frames", "reconnects", "fps", "last_frame_age", "decoder"}
```
Behaviour: reconnect with exponential backoff (initial..max, reset after a successful frame) when
ffmpeg exits or no frame arrives within `stall_timeout`; first frame after network recovery
within 3 s of the camera being reachable again (backoff capped while "recently playing").
Holds a `StreamLimiter` lease while running; if acquire fails -> state FAILED with reason.
No zombie ffmpeg: every child is assigned to the job; `stop()` and interpreter exit kill it.

## `tapo_desktop.core.vod`  [spec:stream.local8800.*, vod.*]
`session.py`
```python
@dataclass
class MediaPart: headers: dict[str, str]; body: bytes          # body already decrypted
class MediaSession:                                             # one POST /stream connection
    def __init__(self, host: str, password: str, *, port: int = 8800, username: str = "admin",
                 timeout: float = 10.0)                         # password = cloud password (plaintext)
    def connect(self) -> None    # Digest auth (MD5 or SHA-256 per challenge, encrypt_type=3 -> SHA256U pwd),
                                 # parses Key-Exchange -> AES-128-CBC key=MD5(nonce:pwd'), iv=MD5(user:nonce)
    def send_json(self, message: dict) -> int    # adds "seq" if missing; returns seq
    def parts(self) -> Iterator[MediaPart]       # multipart "--device-stream-boundary--"; decrypts X-If-Encrypt: 1
    def close(self) -> None
```
`downloader.py`
```python
class RecordingDownloader:
    def __init__(self, host: str, password: str, *, port: int = 8800, ffmpeg_path: str | None = None,
                 session_factory: Callable[..., MediaSession] | None = None)
    def download(self, clip: RecordingClip, out_path: pathlib.Path, *,
                 progress: Callable[[float], None] | None = None,
                 cancel: threading.Event | None = None) -> pathlib.Path
        # sends download request [spec:vod.download.session], acks [spec:vod.stream.flow_control_ack],
        # writes MPEG-TS, stops on stream_status finished, then remuxes to MP4 with ffmpeg
        # (-c:v copy -c:a aac); returns out_path (.mp4). Raises TapoError on failure; partial files removed.
```
