"""Locating ffmpeg and building its decode command line.

Wave 2 stub: signatures and docstrings only (see docs/CONTRACT_MEDIA.md).
"""
from __future__ import annotations

__all__ = ["find_ffmpeg", "build_ffmpeg_args"]


def find_ffmpeg() -> str:
    """Return the path of the ffmpeg executable.

    Search order: env ``TAPO_FFMPEG``; ``ffmpeg.exe`` beside ``sys.executable``; ``ffmpeg.exe`` in
    ``sys._MEIPASS`` (PyInstaller); ``ffmpeg`` on PATH. Raises FileNotFoundError when none exists.
    """
    raise NotImplementedError


def build_ffmpeg_args(
    url: str,
    width: int,
    height: int,
    *,
    hwaccel: str | None = "d3d11va",
    transport: str = "tcp",
    fps: int | None = None,
    audio_url: str | None = None,
) -> list[str]:
    """Return the ffmpeg argument list (WITHOUT the executable itself).

    Input options (before ``-i``): ``-rtsp_transport <transport> -fflags nobuffer -flags low_delay``
    and ``-hwaccel <hwaccel>`` unless ``hwaccel`` is None.
    Video output: ``-map 0:v:0 -vf scale=W:H:force_original_aspect_ratio=decrease,pad=W:H:(ow-iw)/2:(oh-ih)/2
    -pix_fmt rgb24 -f rawvideo pipe:1`` (``fps`` limits the output rate via ``-r`` or an fps filter).
    Audio output only when ``audio_url`` is given, after ``pipe:1``:
    ``-map 0:a:0? -ac 1 -ar 8000 -f s16le <audio_url>``.
    Never logs the password (use ``redact_url``).

    Implementation note (measured against the mock camera, FFmpeg 8.1): default stream probing
    delays the first frame by ~3 s; adding ``-probesize 32768 -analyzeduration 0`` as input options
    brings it to ~1.1 s, which the 3 s reconnect budget in the supervisor contract needs.
    """
    raise NotImplementedError
