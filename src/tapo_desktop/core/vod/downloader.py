"""SD-card clip download over the :8800 media connection  [spec:vod.download.*, vod.stream.*]."""
from __future__ import annotations

import json
import logging
import os
import pathlib
import subprocess
import threading
import uuid
from typing import BinaryIO, Callable

from tapo_desktop.core.errors import (
    StreamLimitError,
    TapoConnectionError,
    TapoDeviceError,
    TapoError,
    TapoProtocolError,
)
from tapo_desktop.core.media_engine.ffmpeg import find_ffmpeg
from tapo_desktop.core.media_engine.job_object import default_job
from tapo_desktop.core.vod.session import MediaPart, MediaSession
from tapo_desktop.models import RecordingClip

__all__ = [
    "RecordingDownloader",
    "DownloadCancelled",
    "build_download_request",
    "build_stop_download_request",
    "build_remux_args",
]

logger = logging.getLogger(__name__)

_STREAM_LIMIT_CODE = -52407
_ACK_EVERY = 25  # [spec:vod.stream.flow_control_ack]
_PTS_CLOCK = 90_000
_PTS_WRAP = 1 << 33
_REMUX_TIMEOUT_S = 300.0
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_PROGRESS_BEFORE_REMUX = 0.99


class DownloadCancelled(TapoError):
    """The download was cancelled through its ``cancel`` event; nothing was written."""


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
    download: dict = {
        "client_id": client_id,
        "channels": [channel],
        "media_type": 0,
        "start_time": str(int(clip.start.timestamp())),
        "end_time": str(int(clip.end.timestamp())),
    }
    if clip.event_type:
        download["event_type"] = [clip.event_type]
    download["download_type"] = "normal"
    download["player_id"] = player_id or uuid.uuid4().hex
    return {"type": "request", "params": {"method": "get", "download": download}}


def build_stop_download_request() -> dict:
    """[spec:vod.download.session] ``{"type":"request","params":{"method":"do","stop":"download"}}`` (no seq)."""
    return {"type": "request", "params": {"method": "do", "stop": "download"}}


def build_remux_args(ts_path: pathlib.Path, mp4_path: pathlib.Path) -> list[str]:
    """ffmpeg arguments (WITHOUT the executable) remuxing MPEG-TS to MP4: ``-i <ts> ... -c:v copy -c:a aac ... <mp4>``.

    The output path is the last argument; overwriting is allowed (``-y``).
    """
    return [
        "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-f", "mpegts", "-i", str(ts_path),
        "-c:v", "copy", "-c:a", "aac",
        "-movflags", "+faststart", "-f", "mp4", str(mp4_path),
    ]


class _Progress:
    """Turns X-Data-PTS headers into a non-decreasing fraction of the clip duration."""

    def __init__(self, callback: Callable[[float], None] | None, duration_s: float):
        self._callback = callback
        self._span = duration_s * _PTS_CLOCK if duration_s > 0 else 0.0
        self._first_pts: int | None = None
        self._value = -1.0

    def report(self, value: float) -> None:
        value = min(1.0, max(0.0, value))
        if self._callback is not None and value > self._value:
            self._value = value
            self._callback(value)

    def on_media(self, part: MediaPart) -> None:
        raw = part.headers.get("X-Data-PTS", "")
        if not raw.isdigit() or not self._span:
            self.report(0.0)
            return
        pts = int(raw)
        if self._first_pts is None:
            self._first_pts = pts
        elapsed = (pts - self._first_pts) % _PTS_WRAP
        self.report(min(_PROGRESS_BEFORE_REMUX, elapsed / self._span))


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
        local_token: str | None = None,
    ):
        """``session_factory(host, password, port=port[, local_token=...])`` creates the MediaSession
        (default: MediaSession); ``local_token`` (the camera's LAT, [spec:lat.media.digest]) is passed
        only when set, so existing factories keep working."""
        self._host = host
        self._password = password
        self._local_token = local_token or None
        self._port = port
        self._ffmpeg_path = ffmpeg_path
        self._session_factory = session_factory or MediaSession

    def __repr__(self) -> str:
        return f"RecordingDownloader(host={self._host!r}, port={self._port})"

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
        Cancelling raises :class:`DownloadCancelled` (a TapoError).
        """
        out_path = pathlib.Path(out_path)
        token = uuid.uuid4().hex[:12]
        ts_tmp = out_path.with_name(f".{out_path.stem}.{token}.part.ts")
        mp4_tmp = out_path.with_name(f".{out_path.stem}.{token}.part.mp4")
        tracker = _Progress(progress, clip.duration_seconds)
        try:
            self._fetch(clip, ts_tmp, tracker, cancel)
            self._remux(ts_tmp, mp4_tmp)
            os.replace(mp4_tmp, out_path)
        finally:
            for tmp in (ts_tmp, mp4_tmp):
                _remove_quietly(tmp)
        tracker.report(1.0)
        logger.info("downloaded clip %s..%s from %s", clip.start.isoformat(), clip.end.isoformat(), self._host)
        return out_path

    # ------------------------------------------------------------------ network phase
    def _fetch(self, clip: RecordingClip, ts_path: pathlib.Path, tracker: _Progress,
               cancel: threading.Event | None) -> None:
        extra = {"local_token": self._local_token} if self._local_token else {}
        session = self._session_factory(self._host, self._password, port=self._port, **extra)
        try:
            if cancel is not None and cancel.is_set():
                raise DownloadCancelled("download cancelled before it started")
            session.connect()
            request_seq = session.send_json(build_download_request(clip))
            tracker.report(0.0)
            with open(ts_path, "wb") as sink:
                media_bytes = self._receive(session, request_seq, sink, tracker, cancel)
            if not media_bytes:
                raise TapoProtocolError("camera finished the download without sending any media")
        finally:
            session.close()

    def _receive(self, session: MediaSession, request_seq: int, sink: BinaryIO, tracker: _Progress,
                 cancel: threading.Event | None) -> int:
        """Consume parts until stream_status/finished. Returns the number of media bytes written."""
        session_id: str | None = None
        media_bytes = 0
        for part in session.parts():
            if cancel is not None and cancel.is_set():
                self._send_stop(session)
                raise DownloadCancelled("download cancelled")
            content_type = part.headers.get("Content-Type", "")
            if content_type.startswith("application/json"):
                msg = _parse_json(part.body)
                if msg.get("type") == "response" and msg.get("seq", request_seq) == request_seq:
                    session_id = _check_download_response(msg)
                elif msg.get("type") == "notification" and _handle_notification(msg):
                    return media_bytes
            elif content_type.endswith("/mp2t"):
                sink.write(part.body)
                media_bytes += len(part.body)
                self._ack_if_due(session, part, session_id)
                tracker.on_media(part)
        raise TapoConnectionError("media connection closed before the download finished")

    @staticmethod
    def _ack_if_due(session: MediaSession, part: MediaPart, session_id: str | None) -> None:
        raw = part.headers.get("X-Data-Sequence", "")
        if not raw.isdigit() or int(raw) == 0 or int(raw) % _ACK_EVERY:
            return
        sid = session_id or part.headers.get("X-Session-Id", "")
        session.send_json(
            {"type": "notification", "params": {"event_type": "stream_sequence"}},
            headers={"X-Data-Received": raw, "X-Session-Id": sid},
        )

    @staticmethod
    def _send_stop(session: MediaSession) -> None:
        try:
            session.send_json(build_stop_download_request())
        except TapoError as exc:
            logger.debug("could not send stop-download request: %s", exc)

    # ------------------------------------------------------------------ remux phase
    def _remux(self, ts_path: pathlib.Path, mp4_path: pathlib.Path) -> None:
        try:
            exe = self._ffmpeg_path or find_ffmpeg()
        except FileNotFoundError as exc:
            raise TapoError(str(exc)) from None
        proc = subprocess.Popen(
            [exe, *build_remux_args(ts_path, mp4_path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=_CREATE_NO_WINDOW,
        )
        try:
            _assign_to_job(proc)
            _, stderr = proc.communicate(timeout=_REMUX_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise TapoError("ffmpeg remux timed out") from None
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        if proc.returncode != 0 or not mp4_path.is_file() or mp4_path.stat().st_size == 0:
            tail = stderr.decode("utf-8", "replace").strip().splitlines()[-1:] if stderr else []
            raise TapoError(f"ffmpeg could not convert the recording to MP4 (exit {proc.returncode})"
                            + (f": {tail[0]}" if tail else ""))


def _parse_json(body: bytes) -> dict:
    try:
        msg = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise TapoProtocolError("camera sent a malformed JSON part") from exc
    if not isinstance(msg, dict):
        raise TapoProtocolError("camera sent a JSON part that is not an object")
    return msg


def _check_download_response(msg: dict) -> str:
    """Validate the download response [spec:vod.download.response]; return its session_id."""
    result = msg.get("result") if isinstance(msg.get("result"), dict) else {}
    code = result.get("error_code", msg.get("error_code", 0))
    if code == _STREAM_LIMIT_CODE:
        raise StreamLimitError(f"camera refused the download: too many media sessions ({code})")
    if code:
        raise TapoDeviceError(int(code), "download")
    session_id = result.get("session_id")
    if not session_id:
        raise TapoProtocolError("download response has no session_id")
    return str(session_id)


def _handle_notification(msg: dict) -> bool:
    """[spec:vod.stream.notifications] True when the download finished; raises on fatal events."""
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    event = params.get("event_type")
    if event == "stream_status" and params.get("status") == "finished":
        return True
    if event == "disk_status" and params.get("status") == "offline":
        raise TapoError("the camera's SD card went offline during the download")
    if event == "stream_finish":
        raise TapoError(f"camera aborted the download ({params.get('reason', 'unknown reason')})")
    if event == "connection_closed":
        raise TapoConnectionError(f"camera closed the media stream ({params.get('close_reason', 'no reason')})")
    return False  # heartbeat / stream_sequence / unknown


def _assign_to_job(proc: subprocess.Popen) -> None:
    try:
        default_job().assign(proc)
    except OSError as exc:
        logger.warning("could not assign ffmpeg (pid %s) to the job object: %s", proc.pid, exc)


def _remove_quietly(path: pathlib.Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("could not remove temporary file %s: %s", path, exc)
