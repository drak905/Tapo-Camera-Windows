"""Remote SD-card recordings through the TP-Link cloud relay, like the Tapo app away from home
[spec:remote.relay.playback_download.*, remote.client_recipe.playback].

A relay slot is requested with the same CIPC ``/v2/relay/request`` as live view, but with
``streamType`` 3 + ``subType "cover"`` (download) or 1 (SD playback), ``channelId "0"``, ``customParams "{}"``
and no ``resolution`` / ``preConnection``. Inside the tunnel the client talks the LAN :8800 protocol
(no inner ``POST /stream``, plaintext TS): first a ``get download`` / ``get playback`` JSON part, then
video/mp2t parts, acked every 25 parts, heartbeat every 15 s, ended by ``stream_status``/``finished``.

:class:`RemoteRecordingDownloader` has the :class:`~tapo_desktop.core.vod.downloader.RecordingDownloader`
contract (``download(clip, out_path, progress=, cancel=)`` -> MP4, same progress / cancel / cleanup rules,
same ffmpeg remux). The download slot (streamType 3) is NOT verified on real cameras, so it is tried
first and, when the slot, the tunnel or the camera's first answer fails, the download falls back to the
playback stream (streamType 1, proven on a real C510W with tools/diag_remote_playback.py: 392 parts /
2.3 MB for a 66 s clip in 15 s). Playback is recorded until ``finished``, until the media PTS reach the
clip's end (then a stop request is sent) or until the relay closes the tunnel after the clip.
Errors that a second path cannot fix (camera offline, not bound, stream limit) are raised at once.
"""
from __future__ import annotations

import logging
import pathlib
import threading
import time
from collections.abc import Callable
from typing import Any, BinaryIO

from tapo_desktop.core.cloud.relay import (
    ERR_ILLEGAL_REQUEST,
    HEARTBEAT_INTERVAL_S,
    STREAM_DOWNLOAD,
    STREAM_PLAYBACK,
    RelayError,
    RelayMediaSession,
)
from tapo_desktop.core.errors import (
    StreamLimitError,
    TapoConnectionError,
    TapoDeviceError,
    TapoError,
    TapoProtocolError,
)
from tapo_desktop.core.vod.downloader import (
    DownloadCancelled,
    RecordingDownloader,
    _handle_notification,
    _parse_json,
    _Progress,
    build_download_request,
    build_stop_download_request,
)
from tapo_desktop.core.vod.session import MediaSession
from tapo_desktop.models import RecordingClip

__all__ = ["PLAYBACK_EVENT_TYPES", "RelayPlaybackSession", "RemoteRecordingDownloader", "VOD_TIMEOUT_S",
           "build_playback_request", "build_stop_playback_request"]

log = logging.getLogger(__name__)

#: Data timeout of the app's stream engine for relay connections (18 s) plus a little slack.
VOD_TIMEOUT_S = 20.0
#: ``event_type`` of the verified playback request (1 timing, 2 motion).
PLAYBACK_EVENT_TYPES = (1, 2)
_STREAM_LIMIT_CODE = -52407
_PTS_CLOCK = 90_000
_PTS_WRAP = 1 << 33


def build_playback_request(clip: RecordingClip, *, client_id: int = 1, channel: int = 0,
                           event_types: tuple[int, ...] = PLAYBACK_EVENT_TYPES) -> dict:
    """``{"type":"request","params":{"method":"get","playback":{...}}}`` exactly as proven on a real camera:
    client_id, channels [channel], scale "1/1", start_time / end_time (epoch s strings), event_type.
    No ``seq`` (MediaSession.send_json adds it)."""
    start, end = int(clip.start.timestamp()), int(clip.end.timestamp())
    if start <= 0:
        raise ValueError("playback start time must be positive")
    return {"type": "request", "params": {"method": "get", "playback": {
        "client_id": int(client_id), "channels": [int(channel)], "scale": "1/1", "start_time": str(start),
        "end_time": str(max(start, end)), "event_type": [int(e) for e in event_types]}}}


def build_stop_playback_request() -> dict:
    """``{"type":"request","params":{"method":"do","stop":"null"}}`` (no seq)."""
    return {"type": "request", "params": {"method": "do", "stop": "null"}}


class RelayPlaybackSession(RelayMediaSession):
    """A :class:`RelayMediaSession` on an SD playback (streamType 1) or download (streamType 3,
    ``subType "cover"``) slot; default I/O timeout :data:`VOD_TIMEOUT_S`."""

    def __init__(self, relay: Any, device_id: str, *, download: bool = False, device_type: str = "SMART.IPCAMERA",
                 model: str = "", cloud_type: int = 2, timeout: float = VOD_TIMEOUT_S,
                 heartbeat_interval: float = HEARTBEAT_INTERVAL_S):
        super().__init__(relay, device_id, device_type=device_type, model=model, cloud_type=cloud_type,
                         timeout=timeout, heartbeat_interval=heartbeat_interval,
                         stream_type=STREAM_DOWNLOAD if download else STREAM_PLAYBACK,
                         sub_type="cover" if download else None)
        self.download = bool(download)

    def __repr__(self) -> str:
        kind = "download" if self.download else "playback"
        return f"RelayPlaybackSession(device={self._device_id!r}, kind={kind}, connected={self._sock is not None})"


class _NotStarted(Exception):
    """Internal: the path failed before the camera accepted the request (another path may work)."""

    def __init__(self, error: TapoError):
        super().__init__(type(error).__name__)
        self.error = error


def _no_fallback(error: TapoError) -> bool:
    """True for refusals another stream type cannot fix (camera offline / not bound / limits ...)."""
    if isinstance(error, StreamLimitError):
        return True
    return isinstance(error, RelayError) and error.fatal and error.code != ERR_ILLEGAL_REQUEST


def _vod_session(msg: dict, what: str) -> str | None:
    """Check the camera's answer to ``get download`` / ``get playback``; return its session_id (or None
    when it names none: the media parts' X-Session-Id is used then)."""
    result = msg.get("result") if isinstance(msg.get("result"), dict) else None
    if result is None:
        result = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    code = result.get("error_code", msg.get("error_code", 0))
    try:
        code = int(code or 0)
    except (TypeError, ValueError):
        raise TapoProtocolError(f"camera sent a malformed {what} answer") from None
    if code == _STREAM_LIMIT_CODE:
        raise StreamLimitError(f"camera refused the {what}: too many media sessions ({code})")
    if code:
        raise TapoDeviceError(code, what)
    sid = result.get("session_id")
    return str(sid) if sid else None


class RemoteRecordingDownloader(RecordingDownloader):
    """Downloads one recording of a remote camera through the relay to MP4 (see the module docstring).

    ``relay`` is a :class:`~tapo_desktop.core.cloud.relay.RelayClient`; ``device_id`` the cloud deviceId;
    ``device_type`` / ``model`` / ``cloud_type`` go into the slot request. ``session_factory(download: bool)
    -> MediaSession`` replaces :class:`RelayPlaybackSession` (tests). ``last_mode`` is ``"download"`` or
    ``"playback"`` after a successful download (which path delivered it).
    """

    def __init__(self, relay: Any, device_id: str, *, device_type: str = "SMART.IPCAMERA", model: str = "",
                 cloud_type: int = 2, ffmpeg_path: str | None = None, timeout: float = VOD_TIMEOUT_S,
                 heartbeat_interval: float = HEARTBEAT_INTERVAL_S,
                 session_factory: Callable[[bool], MediaSession] | None = None):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        super().__init__(device_id, "", port=443, ffmpeg_path=ffmpeg_path)
        self._relay = relay
        self._device_id = device_id
        self._device_type = device_type
        self._model = model
        self._cloud_type = int(cloud_type)
        self._timeout = float(timeout)
        self._heartbeat = float(heartbeat_interval)
        self._vod_factory = session_factory or self._relay_session
        self.last_mode: str | None = None

    def __repr__(self) -> str:
        return f"RemoteRecordingDownloader(device={self._device_id!r})"

    def download(self, clip: RecordingClip, out_path: pathlib.Path, *,
                 progress: Callable[[float], None] | None = None,
                 cancel: threading.Event | None = None) -> pathlib.Path:
        """Download ``clip`` to ``out_path`` (.mp4) through the relay; same contract as
        :meth:`RecordingDownloader.download` (progress in [0, 1] ending at 1.0, DownloadCancelled on
        cancel, no partial/temp file left behind)."""
        self.last_mode = None
        return super().download(clip, out_path, progress=progress, cancel=cancel)

    def _relay_session(self, download: bool) -> MediaSession:
        return RelayPlaybackSession(self._relay, self._device_id, download=download, device_type=self._device_type,
                                    model=self._model, cloud_type=self._cloud_type, timeout=self._timeout,
                                    heartbeat_interval=self._heartbeat)

    # ------------------------------------------------------------------ network phase (overrides)
    def _fetch(self, clip: RecordingClip, ts_path: pathlib.Path, tracker: _Progress,
               cancel: threading.Event | None) -> None:
        try:
            self._fetch_mode(True, clip, ts_path, tracker, cancel)
            self.last_mode = "download"
            return
        except _NotStarted as failed:
            if _no_fallback(failed.error):
                raise failed.error from None
            log.info("remote download of %s: download stream unavailable (%s: %s); using the playback stream",
                     self._device_id, type(failed.error).__name__, failed.error)
        try:
            self._fetch_mode(False, clip, ts_path, tracker, cancel)
        except _NotStarted as failed:
            raise failed.error from None
        self.last_mode = "playback"

    def _fetch_mode(self, download: bool, clip: RecordingClip, ts_path: pathlib.Path, tracker: _Progress,
                    cancel: threading.Event | None) -> None:
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled("download cancelled before it started")
        session = self._vod_factory(download)
        try:
            try:
                session.connect()
                request = build_download_request(clip) if download else build_playback_request(clip)
                request_seq = session.send_json(request)
            except TapoError as exc:
                raise _NotStarted(exc) from None
            tracker.report(0.0)
            with open(ts_path, "wb") as sink:
                media_bytes = self._receive_vod(session, request_seq, sink, tracker, cancel, download, clip)
            if not media_bytes:
                raise TapoProtocolError("camera finished the download without sending any media")
        finally:
            session.close()

    def _stop(self, session: MediaSession, download: bool) -> None:
        try:
            session.send_json(build_stop_download_request() if download else build_stop_playback_request())
        except TapoError as exc:
            log.debug("could not send the stop request: %s", exc)

    def _receive_vod(self, session: MediaSession, request_seq: int, sink: BinaryIO, tracker: _Progress,
                     cancel: threading.Event | None, download: bool, clip: RecordingClip) -> int:
        """Consume parts until finished (or, for playback, the clip end). Returns the media bytes written.
        Failures before the camera accepted the request raise :class:`_NotStarted`."""
        what = "download" if download else "playback"
        duration = max(0.0, clip.duration_seconds)
        deadline = time.monotonic() + max(120.0, 3.0 * duration + 60.0)
        span = duration * _PTS_CLOCK
        first_pts: int | None = None
        session_id: str | None = None
        started = False
        media_bytes = 0
        try:
            for part in session.parts():
                if cancel is not None and cancel.is_set():
                    self._stop(session, download)
                    raise DownloadCancelled("download cancelled")
                if time.monotonic() > deadline:
                    self._stop(session, download)
                    raise TapoError("The remote recording took too long to arrive.")
                content_type = part.headers.get("Content-Type", "")
                if content_type.startswith("application/json"):
                    msg = _parse_json(part.body)
                    if msg.get("type") == "response" and not started and msg.get("seq", request_seq) == request_seq:
                        session_id = _vod_session(msg, what)
                        started = True
                    elif msg.get("type") == "notification" and _handle_notification(msg):
                        return media_bytes
                elif content_type.endswith("/mp2t"):
                    started = True
                    sink.write(part.body)
                    media_bytes += len(part.body)
                    self._ack_if_due(session, part, session_id)
                    tracker.on_media(part)
                    raw = part.headers.get("X-Data-PTS", "")
                    if not download and span and raw.isdigit():
                        pts = int(raw)
                        first_pts = pts if first_pts is None else first_pts
                        if (pts - first_pts) % _PTS_WRAP >= span:
                            self._stop(session, download)  # the playback stream runs on past the clip
                            return media_bytes
        except DownloadCancelled:
            raise
        except TapoError as exc:
            if not started:
                raise _NotStarted(exc) from None
            raise
        if not started:
            raise _NotStarted(TapoConnectionError(f"the relay closed the {what} stream before it started"))
        if not download and media_bytes:
            return media_bytes  # the relay ended the playback tunnel after the clip
        raise TapoConnectionError("media connection closed before the download finished")
