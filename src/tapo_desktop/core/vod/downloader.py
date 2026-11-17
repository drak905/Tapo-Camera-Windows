"""SD-card clip download over the :8800 media connection  [spec:vod.download.*, vod.stream.*].

Wave 2 stub: signatures and docstrings only (see docs/CONTRACT_MEDIA.md).
"""
from __future__ import annotations

import pathlib
import threading
from typing import Callable

from tapo_desktop.core.vod.session import MediaSession
from tapo_desktop.models import RecordingClip

__all__ = ["RecordingDownloader", "build_download_request", "build_stop_download_request", "build_remux_args"]


def build_download_request(
    clip: RecordingClip,
    *,
    player_id: str | None = None,
    client_id: int = 1,
    channel: int = 0,
) -> dict:
    """[spec:vod.download.session] ``{"type":"request","params":{"method":"get","download":{...}}}``.

    download = {client_id, channels:[channel], media_type:0, start_time:str(epoch s), end_time:str(epoch s),
    download_type:"normal", player_id (uuid4 hex when None)} plus ``event_type:[clip.event_type]`` when
    ``clip.event_type`` is non-zero. No ``seq`` (MediaSession.send_json adds it).
    """
    raise NotImplementedError


def build_stop_download_request() -> dict:
    """[spec:vod.download.session] ``{"type":"request","params":{"method":"do","stop":"download"}}`` (no seq)."""
    raise NotImplementedError


def build_remux_args(ts_path: pathlib.Path, mp4_path: pathlib.Path) -> list[str]:
    """ffmpeg arguments (WITHOUT the executable) remuxing MPEG-TS to MP4: ``-i <ts> ... -c:v copy -c:a aac ... <mp4>``.

    The output path is the last argument; overwriting is allowed (``-y``).
    """
    raise NotImplementedError


class RecordingDownloader:
    """Downloads one recording clip to MP4."""

    def __init__(
        self,
        host: str,
        password: str,
        *,
        port: int = 8800,
        ffmpeg_path: str | None = None,
        session_factory: Callable[..., MediaSession] | None = None,
    ):
        """``session_factory(host, password, port=port)`` creates the MediaSession (default: MediaSession)."""
        raise NotImplementedError

    def download(
        self,
        clip: RecordingClip,
        out_path: pathlib.Path,
        *,
        progress: Callable[[float], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> pathlib.Path:
        """Download ``clip`` to ``out_path`` (.mp4) and return it.

        Flow: connect; send_json(build_download_request) ; the response must have error_code 0 and a
        session_id (-52407 -> StreamLimitError, other non-zero -> TapoDeviceError); write video/mp2t
        bodies in order to a temp .ts; every part whose X-Data-Sequence % 25 == 0 is acked with
        ``send_json({"type":"notification","params":{"event_type":"stream_sequence"}},
        headers={"X-Data-Received": <seq>, "X-Session-Id": <sid>})``; stop at notification
        stream_status/finished; then remux with ffmpeg (build_remux_args).
        ``progress`` gets non-decreasing values in [0, 1], the last one exactly 1.0 on success.
        Failure (no media before finished, disk_status offline, connection closed early, ffmpeg
        error) -> TapoError. ``cancel`` set -> sends build_stop_download_request(), stops; either
        way the session is closed and no partial/temp file is left behind.
        """
        raise NotImplementedError
