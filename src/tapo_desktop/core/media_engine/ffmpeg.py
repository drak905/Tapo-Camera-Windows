"""Locating ffmpeg and building its decode command line."""
from __future__ import annotations

import logging
import os
import shutil
import sys

from tapo_desktop.core.media_engine.profiles import redact_url

__all__ = ["find_ffmpeg", "build_ffmpeg_args"]

logger = logging.getLogger(__name__)

_EXE_NAME = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
# Socket I/O timeout handed to ffmpeg's RTSP demuxer (microseconds). The supervisor's own stall
# watchdog normally fires first; this only stops an orphaned ffmpeg from hanging forever.
_RTSP_SOCKET_TIMEOUT_US = 5_000_000


def find_ffmpeg() -> str:
    """Return the path of the ffmpeg executable.

    Search order: env ``TAPO_FFMPEG``; ``ffmpeg.exe`` beside ``sys.executable``; ``ffmpeg.exe`` in
    ``sys._MEIPASS`` (PyInstaller); ``ffmpeg`` on PATH. Raises FileNotFoundError when none exists.
    A ``TAPO_FFMPEG`` that points to a missing file is logged and skipped.
    """
    env = os.environ.get("TAPO_FFMPEG")
    if env:
        if os.path.isfile(env):
            return env
        logger.warning("TAPO_FFMPEG=%s does not exist; searching elsewhere", env)
    candidates = [os.path.dirname(os.path.abspath(sys.executable))]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(str(meipass))
    for directory in candidates:
        path = os.path.join(directory, _EXE_NAME)
        if os.path.isfile(path):
            return path
    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path
    raise FileNotFoundError("ffmpeg executable not found (set TAPO_FFMPEG or put ffmpeg on PATH)")


def build_ffmpeg_args(
    url: str,
    width: int,
    height: int,
    *,
    hwaccel: str | None = "d3d11va",
    transport: str = "tcp",
    fps: int | None = None,
    audio_url: str | None = None,
    input_format: str | None = None,
) -> list[str]:
    """Return the ffmpeg argument list (WITHOUT the executable itself).

    Input options (before ``-i``): ``-rtsp_transport <transport> -fflags nobuffer -flags low_delay``
    and ``-hwaccel <hwaccel>`` unless ``hwaccel`` is None.
    Video output: ``-map 0:v:0 -vf scale=W:H:force_original_aspect_ratio=decrease,pad=W:H:(ow-iw)/2:(oh-ih)/2
    -pix_fmt rgb24 -f rawvideo pipe:1`` (``fps`` limits the output rate via ``-r`` or an fps filter).
    Audio output only when ``audio_url`` is given, after ``pipe:1``:
    ``-map 0:a:0? -ac 1 -ar 8000 -f s16le <audio_url>``.
    ``input_format`` (e.g. ``"mpegts"`` with ``url="pipe:0"``) forces the demuxer with ``-f`` and
    replaces the RTSP-only ``-rtsp_transport`` option.
    Never logs the password (use ``redact_url``).

    Implementation note (measured against the mock camera, FFmpeg 8.1): default stream probing
    delays the first frame by ~3 s; adding ``-probesize 32768 -analyzeduration 0`` as input options
    brings it to ~1.1 s, which the 3 s reconnect budget in the supervisor contract needs.
    """
    if width <= 0 or height <= 0:
        raise ValueError("width and height must be positive")
    args = ["-hide_banner", "-nostdin", "-loglevel", "error"]
    if input_format:
        args += ["-f", input_format]
    else:
        args += ["-rtsp_transport", transport]
    args += ["-fflags", "nobuffer", "-flags", "low_delay"]
    args += ["-probesize", "32768", "-analyzeduration", "0"]
    if url.lower().startswith(("rtsp://", "rtsps://")):
        args += ["-timeout", str(_RTSP_SOCKET_TIMEOUT_US)]
    if hwaccel:
        args += ["-hwaccel", hwaccel]
    args += ["-i", url, "-map", "0:v:0"]
    args += [
        "-vf",
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2",
    ]
    if fps:
        args += ["-r", str(int(fps))]
    args += ["-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"]
    if audio_url:
        args += ["-map", "0:a:0?", "-ac", "1", "-ar", "8000", "-f", "s16le", audio_url]
    logger.debug(
        "ffmpeg args for %s: %dx%d hwaccel=%s transport=%s fps=%s audio=%s",
        redact_url(url), width, height, hwaccel, transport, fps, bool(audio_url),
    )
    return args
