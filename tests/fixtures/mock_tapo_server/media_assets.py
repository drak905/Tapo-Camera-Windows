"""Pre-generated media for the mock camera (FFmpeg is used only here, once per cache key).

Cache dir: env ``TAPO_MOCK_CACHE`` or ``%TEMP%/tapo_mock_cache``. Files are keyed by their
generation parameters and written atomically (temp file + ``os.replace``), so parallel test
processes never see partial files.

* H.264 Annex-B elementary stream: libx264 baseline, no B-frames, 15 fps, keyframe every
  second, SPS/PPS repeated before every IDR.
* PCMA (G.711 A-law) raw 8 kHz mono.
* MPEG-TS (H.264 640x360 + AAC 8 kHz mono) used by the :8800 download/playback flow.
"""
from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

CACHE_VERSION = 2
FPS = 15
CLIP_SECONDS = 4
TS_SECONDS = 6

_mem_lock = threading.Lock()
_video_cache: dict[tuple, "VideoAsset"] = {}
_bytes_cache: dict[str, bytes] = {}


def find_ffmpeg() -> str | None:
    env = os.environ.get("TAPO_FFMPEG")
    if env and Path(env).exists():
        return env
    return shutil.which("ffmpeg")


def cache_root() -> Path:
    root = Path(os.environ.get("TAPO_MOCK_CACHE") or (Path(tempfile.gettempdir()) / "tapo_mock_cache"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _cached(name: str, args_after_ffmpeg: list[str]) -> Path:
    key = hashlib.sha1(repr((CACHE_VERSION, name, args_after_ffmpeg)).encode()).hexdigest()[:16]
    suffix = Path(name).suffix
    out = cache_root() / f"{Path(name).stem}_{key}{suffix}"
    if out.exists() and out.stat().st_size > 0:
        return out
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise FileNotFoundError("ffmpeg not found (needed once to generate mock media assets)")
    tmp = out.with_name(f"{out.stem}.{os.getpid()}.{threading.get_ident()}.tmp{suffix}")
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args_after_ffmpeg, str(tmp)]
    proc = subprocess.run(cmd, capture_output=True, timeout=120)
    if proc.returncode != 0 or not tmp.exists():
        raise RuntimeError(f"ffmpeg asset generation failed: {proc.stderr.decode(errors='replace')[-800:]}")
    os.replace(tmp, out)
    return out


def _x264_args(gop: int) -> list[str]:
    return ["-c:v", "libx264", "-preset", "ultrafast", "-profile:v", "baseline", "-pix_fmt", "yuv420p",
            "-bf", "0", "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
            "-x264-params", f"keyint={gop}:min-keyint={gop}:scenecut=0:repeat-headers=1:sliced-threads=0",
            "-crf", "30", "-threads", "1"]


def h264_path(width: int, height: int, fps: int = FPS, seconds: int = CLIP_SECONDS) -> Path:
    src = f"testsrc2=size={width}x{height}:rate={fps}:duration={seconds}"
    return _cached(f"video_{width}x{height}.h264", ["-f", "lavfi", "-i", src, *_x264_args(fps), "-an", "-f", "h264"])


def pcma_path(seconds: int = CLIP_SECONDS) -> Path:
    src = f"sine=frequency=440:sample_rate=8000:duration={seconds}"
    return _cached("audio_8k.alaw", ["-f", "lavfi", "-i", src, "-ac", "1", "-c:a", "pcm_alaw", "-f", "alaw"])


def ts_path(width: int = 640, height: int = 360, seconds: int = TS_SECONDS, fps: int = FPS) -> Path:
    vsrc = f"testsrc2=size={width}x{height}:rate={fps}:duration={seconds}"
    asrc = f"sine=frequency=880:sample_rate=8000:duration={seconds}"
    return _cached(f"clip_{width}x{height}.ts",
                   ["-f", "lavfi", "-i", vsrc, "-f", "lavfi", "-i", asrc, *_x264_args(fps),
                    "-c:a", "aac", "-ar", "8000", "-ac", "1", "-b:a", "24k", "-f", "mpegts"])


# ---------------------------------------------------------------------------------------------
# H.264 Annex-B parsing
# ---------------------------------------------------------------------------------------------
def split_nals(data: bytes) -> list[bytes]:
    """Split an Annex-B byte stream into NAL units (start codes removed)."""
    nals: list[bytes] = []
    i, n = 0, len(data)
    starts: list[tuple[int, int]] = []  # (start_code_pos, payload_pos)
    while i < n - 2:
        if data[i] == 0 and data[i + 1] == 0:
            if data[i + 2] == 1:
                starts.append((i, i + 3))
                i += 3
                continue
            if i < n - 3 and data[i + 2] == 0 and data[i + 3] == 1:
                starts.append((i, i + 4))
                i += 4
                continue
        i += 1
    for idx, (_, p) in enumerate(starts):
        end = starts[idx + 1][0] if idx + 1 < len(starts) else n
        nal = data[p:end]
        while nal.endswith(b"\x00") and len(nal) > 1:  # trailing_zero_8bits
            nal = nal[:-1]
        if nal:
            nals.append(nal)
    return nals


def group_access_units(nals: list[bytes]) -> list[tuple[bytes, ...]]:
    aus: list[list[bytes]] = []
    cur: list[bytes] = []
    has_vcl = False
    for nal in nals:
        t = nal[0] & 0x1F
        if t == 9:  # AUD: drop, always starts a new AU
            if cur:
                aus.append(cur)
            cur, has_vcl = [], False
            continue
        if t in (6, 7, 8) and has_vcl:
            aus.append(cur)
            cur, has_vcl = [], False
        if t in (1, 5):
            first_mb_zero = len(nal) > 1 and (nal[1] & 0x80) != 0
            if has_vcl and first_mb_zero:
                aus.append(cur)
                cur = []
            has_vcl = True
        cur.append(nal)
    if cur:
        aus.append(cur)
    return [tuple(a) for a in aus if any((x[0] & 0x1F) in (1, 5) for x in a)]


@dataclass(frozen=True)
class VideoAsset:
    width: int
    height: int
    fps: int
    access_units: tuple[tuple[bytes, ...], ...]
    sps: bytes
    pps: bytes

    @property
    def profile_level_id(self) -> str:
        return self.sps[1:4].hex().upper()

    @property
    def sprop_parameter_sets(self) -> str:
        return base64.b64encode(self.sps).decode() + "," + base64.b64encode(self.pps).decode()


def load_video(width: int, height: int, fps: int = FPS, seconds: int = CLIP_SECONDS) -> VideoAsset:
    key = (width, height, fps, seconds)
    with _mem_lock:
        if key in _video_cache:
            return _video_cache[key]
    data = h264_path(width, height, fps, seconds).read_bytes()
    nals = split_nals(data)
    sps = next(n for n in nals if (n[0] & 0x1F) == 7)
    pps = next(n for n in nals if (n[0] & 0x1F) == 8)
    asset = VideoAsset(width, height, fps, tuple(group_access_units(nals)), sps, pps)
    with _mem_lock:
        _video_cache[key] = asset
    return asset


def _load_bytes(path: Path) -> bytes:
    k = str(path)
    with _mem_lock:
        if k in _bytes_cache:
            return _bytes_cache[k]
    data = path.read_bytes()
    with _mem_lock:
        _bytes_cache[k] = data
    return data


def load_pcma(seconds: int = CLIP_SECONDS) -> bytes:
    return _load_bytes(pcma_path(seconds))


def load_ts(width: int = 640, height: int = 360, seconds: int = TS_SECONDS) -> bytes:
    return _load_bytes(ts_path(width, height, seconds))


def ensure_all(dual_lens: bool = False) -> None:
    """Generate (or load from cache) everything a camera needs. Raises if ffmpeg is missing."""
    load_video(1280, 720)
    load_video(640, 360)
    load_pcma()
    load_ts()
