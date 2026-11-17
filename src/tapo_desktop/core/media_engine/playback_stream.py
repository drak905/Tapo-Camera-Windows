"""SD-card playback streaming, like the playback screen of the Tapo phone app
[spec:vod.playback.session, vod.playback.controls, vod.playback.enums, vod.stream.*].

:class:`PlaybackStreamSupervisor` plays the recordings of a camera over the LAN media port (:8800,
same Digest/AES session as live view); :class:`RelayPlaybackStreamSupervisor` does the same through the
TP-Link cloud relay (playback slot ``streamType`` 1, verified on a real remote C510W). Both reuse the
:class:`~tapo_desktop.core.media_engine.local_stream.LocalStreamSupervisor` machinery (handshake,
pump, acks every 25 parts, ffmpeg TS -> rgb24 frames, reconnect/backoff, stall watchdog, lease) and
add:

* **Range request.** One ``get playback`` from the current position to ``end_utc`` (normally the end
  of the day): the camera plays the following recordings one after the other, like the app. When the
  camera ends the range early (``stream_status``/``finished`` or the relay closing the tunnel) while
  ``segments`` (the day's clip list) has a later recording, a new request starts there; otherwise the
  state goes to STOPPED with reason :data:`END_OF_RECORDINGS`.
* **Pacing.** Cameras send playback faster than real time while acked. The pump splits every TS part
  at the video PES starts, reads their PTS (90 kHz; ``X-Data-PTS`` when a part is not plain TS
  packets) and writes each frame to ffmpeg when it is due, so frames come out at the recording's pace
  (times the speed). The blocked pump stops reading/acking, so the camera's ack window and TCP flow
  control keep the buffering bounded.
* **Position.** The PTS are mapped to recording time (UTC seconds): steps up to 3 s advance the
  position; a larger jump (a new recording file) or running past the end of a recording moves it to the
  start of the next recording in ``segments``. ``on_position(utc)`` fires about 5 times a second.
* **Controls.** ``pause()`` holds the next frame (no frames, no position change, stall watchdog off)
  and sends ``do pause``; ``resume()`` sends ``do play "null"``. If the camera closed the paused
  connection, the normal reconnect continues from the last position. ``seek(utc)`` starts a new
  playback request at ``utc`` (a fresh request is the verified path; the in-session ``do play
  {start_time}`` seek is in the spec but unverified on real firmware, and a fresh session cannot
  mix stale data from the old position); it also resumes a paused playback.
  ``set_speed(scale)`` accepts "1/1", "2/1", "4/1", "8/1" [spec:vod.playback.enums]. The request
  always asks for ``scale`` "1/1" (the only verified value) and the speed is applied by the client-side
  pacing; it is bounded by how fast the camera / relay delivers.
* **Reconnect** resumes from the last played position (new request at that second).
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from typing import Any

from tapo_desktop.core.errors import TapoError
from tapo_desktop.core.media_engine._decoder import FfmpegDecoder
from tapo_desktop.core.media_engine.job_object import JobObject
from tapo_desktop.core.media_engine.limiter import StreamLimiter
from tapo_desktop.core.media_engine.local_stream import LocalStreamSupervisor, _LocalTarget, _Pump, _json_body
from tapo_desktop.core.media_engine.supervisor import Frame, StreamState, _Outcome, _Target
from tapo_desktop.core.vod.session import MediaPart, MediaSession

__all__ = ["END_OF_RECORDINGS", "PLAYBACK_EVENT_TYPES", "PLAYBACK_SPEEDS", "PlaybackStreamSupervisor",
           "RelayPlaybackStreamSupervisor", "build_pause_request", "build_range_playback_request",
           "build_resume_request", "normalize_segments"]

logger = logging.getLogger(__name__)

#: STOPPED reason after the last recording of the range was played.
END_OF_RECORDINGS = "end of recordings"
#: Supported playback speeds [spec:vod.playback.enums scale] -> pace multiplier.
PLAYBACK_SPEEDS: dict[str, float] = {"1/1": 1.0, "2/1": 2.0, "4/1": 4.0, "8/1": 8.0}
#: ``event_type`` of the verified playback request (1 timing, 2 motion).
PLAYBACK_EVENT_TYPES = (1, 2)

_PTS_CLOCK = 90_000
_PTS_MASK = (1 << 33) - 1
_PTS_HALF = 1 << 32
_MAX_STEP_S = 3.0  # a larger PTS step = another recording file
_LATE_REBASE_S = 1.0  # this far behind schedule (slow link): restart the clock instead of racing
_POSITION_EVERY_S = 0.2
_EDGE_S = 1.5  # slack when matching a position to a recording of the clip list
_TS_PACKET = 188


def build_range_playback_request(start_utc: int, end_utc: int, *, client_id: int = 1, channel: int = 0,
                                 event_types: tuple[int, ...] = PLAYBACK_EVENT_TYPES) -> dict:
    """``get playback`` from ``start_utc`` to ``end_utc`` (epoch s, sent as strings), scale "1/1", like the
    verified request [spec:vod.playback.session]. No ``seq`` (MediaSession.send_json adds it)."""
    start, end = int(start_utc), int(end_utc)
    if start <= 0:
        raise ValueError("playback start time must be positive")
    if channel < 0:
        raise ValueError("channel must be >= 0")
    return {"type": "request", "params": {"method": "get", "playback": {
        "client_id": int(client_id), "channels": [int(channel)], "scale": "1/1", "start_time": str(start),
        "end_time": str(max(start, end)), "event_type": [int(e) for e in event_types]}}}


def build_pause_request() -> dict:
    """``do pause`` [spec:vod.playback.controls] (no seq)."""
    return {"type": "request", "params": {"method": "do", "pause": "null"}}


def build_resume_request() -> dict:
    """``do play "null"`` = resume [spec:vod.playback.controls] (no seq)."""
    return {"type": "request", "params": {"method": "do", "play": "null"}}


def normalize_segments(items: Sequence[Any] | None) -> list[tuple[float, float]]:
    """Sorted, merged ``[(start, end)]`` epoch seconds from ``(start, end)`` pairs or RecordingClip-like
    objects (``.start`` / ``.end`` aware datetimes); empty / inverted entries are dropped."""
    spans: list[tuple[float, float]] = []
    for item in items or ():
        if isinstance(item, (tuple, list)) and len(item) == 2:
            start, end = float(item[0]), float(item[1])
        else:
            start, end = item.start.timestamp(), item.end.timestamp()
        if end > start:
            spans.append((start, end))
    spans.sort()
    merged: list[tuple[float, float]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1] + 0.5:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _read_pts(b: bytes, i: int) -> int:
    return (((b[i] >> 1) & 7) << 30) | (b[i + 1] << 22) | ((b[i + 2] >> 1) << 15) | (b[i + 3] << 7) | (b[i + 4] >> 1)


class _VideoSplitter:
    """Splits a chunk of whole TS packets at the video PES starts: ``[(pts or None, bytes)]``.

    The first piece has ``pts`` None when the chunk does not start with a video PES (continuation of
    the previous frame). The video PID is the first PID carrying a PES with stream_id 0xE0..0xEF.
    """

    def __init__(self) -> None:
        self.video_pid: int | None = None

    def split(self, data: bytes) -> list[tuple[int | None, bytes]] | None:
        """None when ``data`` is not a whole number of aligned TS packets."""
        n = len(data)
        if n == 0 or n % _TS_PACKET or data[0] != 0x47:
            return None
        pieces: list[tuple[int | None, bytes]] = []
        start, pts = 0, None
        for off in range(0, n, _TS_PACKET):
            if data[off] != 0x47:
                return None
            b1 = data[off + 1]
            if not b1 & 0x40:  # no payload unit start
                continue
            pid = ((b1 & 0x1F) << 8) | data[off + 2]
            if self.video_pid is not None and pid != self.video_pid:
                continue
            afc = (data[off + 3] >> 4) & 3
            if not afc & 1:
                continue
            idx = off + 4 + (1 + data[off + 4] if afc & 2 else 0)
            if idx + 14 > off + _TS_PACKET or data[idx:idx + 3] != b"\x00\x00\x01":
                continue
            if not 0xE0 <= data[idx + 3] <= 0xEF or not data[idx + 7] & 0x80:
                continue
            value = _read_pts(data, idx + 9)
            self.video_pid = pid
            if off > start:
                pieces.append((pts, data[start:off]))
            start, pts = off, value
        pieces.append((pts, data[start:]))
        return pieces


class _Timeline:
    """Maps the media PTS of one playback request to recording time (UTC seconds)."""

    def __init__(self, segments: list[tuple[float, float]], start: float):
        self._segs = segments
        self.position = self.snap(start, segments)
        self._last_pts: int | None = None

    @staticmethod
    def snap(t: float, segments: list[tuple[float, float]]) -> float:
        """``t`` when it is inside a recording, else the start of the next one (``t`` when none)."""
        for start, end in segments:
            if t < end:
                return max(t, start)
        return t

    def _containing(self, t: float) -> int | None:
        for i, (start, end) in enumerate(self._segs):
            if start - _EDGE_S <= t <= end + _EDGE_S:
                return i
        return None

    def next_start(self, t: float) -> float | None:
        """Start of the recording after the one containing ``t`` (or after ``t``), None if none."""
        i = self._containing(t)
        if i is not None:
            return self._segs[i + 1][0] if i + 1 < len(self._segs) else None
        return next((s for s, _ in self._segs if s > t), None)

    def advance(self, pts: int) -> bool:
        """Account for a frame with ``pts``; True when the position jumped (pacing must restart)."""
        if self._last_pts is None:
            self._last_pts = pts
            return True
        delta = (pts - self._last_pts) & _PTS_MASK
        if delta >= _PTS_HALF:
            delta -= _PTS_MASK + 1
        self._last_pts = pts
        step = delta / _PTS_CLOCK
        if 0.0 <= step <= _MAX_STEP_S:
            new = self.position + step
            i = self._containing(self.position)
            if i is not None and new > self._segs[i][1] + _EDGE_S:  # PTS ran on into the next recording
                nxt = self._segs[i + 1][0] if i + 1 < len(self._segs) else None
                if nxt is not None:
                    self.position = nxt + max(0.0, new - self._segs[i][1] - _EDGE_S)
                    return True
            self.position = new
            return False
        nxt = self.next_start(self.position)  # a new recording file
        if nxt is not None:
            self.position = nxt
        elif not self._segs and 0.0 < step < 86400.0:
            self.position += step
        return True


class _Pacer:
    """Wall clock schedule of recording time: ``due(media)`` = when that media second should be shown."""

    def __init__(self) -> None:
        self._wall = 0.0
        self._media = 0.0
        self._speed = 1.0

    def rebase(self, media: float, speed: float) -> None:
        self._wall, self._media, self._speed = time.monotonic(), media, max(0.01, speed)

    def due(self, media: float) -> float:
        return self._wall + (media - self._media) / self._speed


class _PlaybackPump(_Pump):
    """The :class:`_Pump` of one playback attempt: TS -> ffmpeg paced by the PTS, acks, end of range."""

    def __init__(self, owner: "PlaybackStreamSupervisor", session: MediaSession, parts: Iterator[MediaPart],
                 decoder: FfmpegDecoder, wake: threading.Event, name: str):
        super().__init__(session, parts, decoder, wake, name)
        self._owner = owner
        self.finished = False  # the camera reported the end of the requested range
        self._splitter = _VideoSplitter()
        self._pacer = _Pacer()
        self._speed_gen: int | None = None

    def run(self) -> None:
        reason = "camera closed the media connection"
        try:
            for part in self._parts:
                if self._halt.is_set():
                    reason = "stopped"
                    break
                msg = _json_body(part)
                if msg is not None:
                    end = self._on_playback_json(msg)
                    if end:
                        reason = end
                        break
                    continue
                ctype = part.headers.get("Content-Type", "").lower()
                if "mp2t" not in ctype or ctype.startswith("audio/") or not part.body:
                    continue
                if not self._feed(part):
                    reason = "stopped" if self._halt.is_set() else "decoder input closed"
                    break
                self.media_parts += 1
                self._ack_if_due(part)
        except TapoError as exc:
            reason = str(exc)
        except Exception:  # pragma: no cover - defensive: never let the pump die silently
            logger.exception("playback pump crashed")
            reason = "internal error in playback pump"
        finally:
            self.reason = reason
            self._wake.set()

    def _on_playback_json(self, msg: dict) -> str | None:
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        if msg.get("type") == "notification":
            event = params.get("event_type")
            if event == "stream_status" and params.get("status") == "finished":
                self.finished = True
                return END_OF_RECORDINGS
            if event == "disk_status" and params.get("status") == "offline":
                self.fatal = True
                return "the camera's SD card is not available"
        elif msg.get("type") == "response":
            return None  # answers to do pause / play
        return self._on_json(msg)

    def _feed(self, part: MediaPart) -> bool:
        pieces = self._splitter.split(part.body)
        if pieces is None:  # not plain TS packets: pace the whole part by its X-Data-PTS
            raw = part.headers.get("X-Data-PTS", "")
            pieces = [(int(raw) if raw.isdigit() else None, part.body)]
        for pts, data in pieces:
            if pts is not None and not self._pace(pts):
                return False
            if not self._decoder.write(data):
                return False
        return True

    def _pace(self, pts: int) -> bool:
        """Wait until the frame starting at ``pts`` is due; False when halted."""
        owner = self._owner
        jumped = owner._timeline.advance(pts)
        position = owner._timeline.position
        gen, speed = owner._speed_state()
        if jumped or gen != self._speed_gen:
            self._pacer.rebase(position, speed)
            self._speed_gen = gen
        was_paused = False
        while True:
            if self._halt.is_set():
                return False
            if owner.paused:
                was_paused = True
                self._halt.wait(0.05)
                continue
            gen, speed = owner._speed_state()
            if was_paused or gen != self._speed_gen:
                self._pacer.rebase(position, speed)
                self._speed_gen, was_paused = gen, False
            left = self._pacer.due(position) - time.monotonic()
            if left <= 0:
                if left < -_LATE_REBASE_S:
                    self._pacer.rebase(position, speed)
                break
            self._halt.wait(min(left, 0.05))
        owner._note_position(position)
        return True


class PlaybackStreamSupervisor(LocalStreamSupervisor):
    """Plays the SD-card recordings of one camera over :8800 from ``start_utc`` to ``end_utc``.

    Same public surface and callbacks as :class:`LocalStreamSupervisor` (``start``/``stop``/``state``/
    ``pid``/``stats``; ``on_frame`` frames of exactly ``width`` x ``height``; ``on_state`` transitions)
    plus ``on_position(utc_seconds)``, :meth:`pause`, :meth:`resume`, :meth:`seek`, :meth:`set_speed`,
    :attr:`position`, :attr:`paused`, :attr:`speed`. See the module docstring for the behaviour.

    ``segments`` is the day's clip list (``(start, end)`` pairs or RecordingClips): it maps the stream to
    recording time across recordings and lets playback continue with the next recording. The end of the
    range -> STOPPED with reason :data:`END_OF_RECORDINGS`. A camera that refuses another media session
    (HTTP 503 / -52407) or the limiter -> FAILED, reason containing "limit"; any other refusal of the
    playback request -> FAILED with its error code. ``session_factory()`` replaces the direct :8800
    session of each attempt (the relay variant uses it).
    """

    _REQUEST_LABEL = "playback"

    def __init__(
        self,
        host: str,
        password: str,
        width: int = 640,
        height: int = 360,
        *,
        start_utc: float,
        end_utc: float,
        segments: Sequence[Any] | None = (),
        port: int = 8800,
        channel: int = 0,
        on_frame: Callable[[Frame], None],
        on_state: Callable[[StreamState, str | None], None] | None = None,
        on_position: Callable[[float], None] | None = None,
        speed: str = "1/1",
        ffmpeg_path: str | None = None,
        hwaccel: str = "auto",
        limiter: StreamLimiter | None = None,
        limiter_key: str | None = None,
        job: JobObject | None = None,
        backoff_initial: float = 0.5,
        backoff_max: float = 10.0,
        stall_timeout: float = 8.0,
        max_retries: int | None = None,
        local_token: str | None = None,
        session_factory: Callable[[], MediaSession] | None = None,
        client_id: int = 1,
        event_types: tuple[int, ...] = PLAYBACK_EVENT_TYPES,
    ):
        start, end = float(start_utc), float(end_utc)
        if not (math.isfinite(start) and start > 0):
            raise ValueError("start_utc must be a positive epoch time")
        if not (math.isfinite(end) and end > start):
            raise ValueError("end_utc must be after start_utc")
        if speed not in PLAYBACK_SPEEDS:
            raise ValueError(f"speed must be one of {list(PLAYBACK_SPEEDS)}")
        self._segments = normalize_segments(segments)
        self._end_utc = end
        self._on_position = on_position
        self._client_id = int(client_id)
        self._event_types = tuple(int(e) for e in event_types)
        self._pb_lock = threading.Lock()
        self._resume_at = _Timeline.snap(start, self._segments)
        self._position: float | None = None
        self._speed_scale = speed
        self._speed_gen = 0
        self._paused = threading.Event()
        self._finished = False
        self._end_refused = False
        self._timeline = _Timeline(self._segments, self._resume_at)
        self._request_start = max(1, int(self._resume_at))
        self._playback_pump: _PlaybackPump | None = None
        self._last_report = 0.0
        factory = None if session_factory is None else (lambda _resolution: session_factory())
        super().__init__(
            host, password, width, height, resolution="VGA", port=port, channel=channel, on_frame=on_frame,
            on_state=on_state, ffmpeg_path=ffmpeg_path, hwaccel=hwaccel, limiter=limiter,
            limiter_key=limiter_key, job=job, backoff_initial=backoff_initial, backoff_max=backoff_max,
            stall_timeout=stall_timeout, max_retries=max_retries, local_token=local_token,
            session_factory=factory,
        )

    def _pseudo_url(self, resolution: str) -> str:
        host = f"[{self._host}]" if ":" in self._host and not self._host.startswith("[") else self._host
        return f"tapo-playback://{host}:{self._port}/{self._channel}"

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._host!r}, state={self.state.name}, position={self._position})"

    # ================================================================== public API
    @property
    def position(self) -> float | None:
        """Recording time (UTC epoch seconds) of the last frame handed to the decoder, None before."""
        return self._position

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    @property
    def speed(self) -> str:
        return self._speed_scale

    @property
    def segments(self) -> list[tuple[float, float]]:
        return list(self._segments)

    def start(self) -> None:
        """Start (or, after the end of the recordings, restart) playback at the current position."""
        with self._pb_lock:
            self._finished = False
            at = self._resume_at
        self._report(at, force=True)
        super().start()

    def pause(self) -> None:
        """Freeze the picture and the position; tells the camera ``do pause`` (best effort)."""
        if self._paused.is_set():
            return
        self._paused.set()
        self._send_control(build_pause_request())

    def resume(self) -> None:
        """Continue after :meth:`pause` (``do play "null"``)."""
        if not self._paused.is_set():
            return
        decoder = self._decoder
        if decoder is not None:
            decoder.last_activity = time.monotonic()  # the pause is not a stall
        self._paused.clear()
        self._send_control(build_resume_request())

    def seek(self, utc_seconds: float) -> None:
        """Continue playback at ``utc_seconds`` (snapped to the next recording when it falls in a gap) with a
        new playback request; resumes a paused playback; restarts after the end of the recordings."""
        utc = float(utc_seconds)
        if not (math.isfinite(utc) and utc > 0):
            raise ValueError("seek time must be a positive epoch time")
        target = _Timeline.snap(utc, self._segments)
        with self._pb_lock:
            self._resume_at = target
            finished = self._finished
        self._paused.clear()
        self._report(target, force=True)
        with self._lock:
            running = self._thread is not None and self._thread.is_alive() and not self._stopping.is_set()
            if running:
                self._pending = self._target  # end this attempt; the next one requests the new time
        if running:
            logger.debug("playback %s: seeking", self._host_label())
            self._wake.set()
        elif finished:
            self.start()

    def set_speed(self, scale: str) -> None:
        """Playback speed "1/1", "2/1", "4/1" or "8/1" (client-side pacing, see the module docstring)."""
        if scale not in PLAYBACK_SPEEDS:
            raise ValueError(f"speed must be one of {list(PLAYBACK_SPEEDS)}, not {scale!r}")
        with self._pb_lock:
            if scale != self._speed_scale:
                self._speed_scale = scale
                self._speed_gen += 1

    # ================================================================== helpers for the pump
    def _speed_state(self) -> tuple[int, float]:
        with self._pb_lock:
            return self._speed_gen, PLAYBACK_SPEEDS[self._speed_scale]

    def _note_position(self, utc: float) -> None:
        with self._pb_lock:
            self._resume_at = utc
            self._position = utc
        self._report(utc)

    def _report(self, utc: float, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_report < _POSITION_EVERY_S:
            return
        self._last_report = now
        if self._on_position is not None:
            try:
                self._on_position(float(utc))
            except Exception:
                logger.exception("on_position callback failed")

    def _send_control(self, message: dict) -> None:
        with self._lock:
            session = self._session
        if session is None or not session.session_id:
            return
        try:
            session.send_json(message)
        except TapoError as exc:
            logger.debug("playback %s: control request not sent: %s", self._host_label(), exc)

    # ================================================================== LocalStreamSupervisor hooks
    def _stream_request(self, target: _LocalTarget) -> dict:
        return build_range_playback_request(self._request_start, int(math.ceil(self._end_utc)),
                                            client_id=self._client_id, channel=self._channel,
                                            event_types=self._event_types)

    def _refused(self, code: object) -> _Outcome:
        if self._segments and not any(end > self._request_start + _EDGE_S for _, end in self._segments):
            self._end_refused = True  # nothing left to play after this time
            return _Outcome(reason=END_OF_RECORDINGS)
        return _Outcome(reason=f"camera refused the playback (error_code {code})", fatal=True)

    def _new_pump(self, session: MediaSession, parts: Iterator[MediaPart], decoder: FfmpegDecoder) -> _Pump:
        pump = _PlaybackPump(self, session, parts, decoder, self._wake, name=f"playback-pump-{self._host}")
        self._playback_pump = pump
        return pump

    def _stall_suspended(self) -> bool:
        return self._paused.is_set()

    # ================================================================== worker
    def _supervise(self, exe: str) -> None:
        super()._supervise(exe)
        with self._pb_lock:
            finished = self._finished
        if finished:
            logger.info("playback %s: end of recordings", self._host_label())
            self._release_lease()
            self._set_state(StreamState.STOPPED, END_OF_RECORDINGS, force=True)

    def _finish(self) -> _Outcome:
        with self._pb_lock:
            self._finished = True
        self._stopping.set()
        return _Outcome(reason=END_OF_RECORDINGS)

    def _attempt(self, exe: str, target: _Target) -> _Outcome:
        with self._pb_lock:
            start = self._resume_at
        if start >= self._end_utc:
            return self._finish()
        self._request_start = max(1, int(math.floor(start)))
        self._timeline = _Timeline(self._segments, start)
        self._playback_pump = None
        self._end_refused = False
        outcome = super()._attempt(exe, target)
        if self._stopping.is_set() or outcome.switched:
            return outcome
        pump = self._playback_pump
        reached_end = self._end_refused or (pump is not None and pump.finished)
        position = self._timeline.position
        if not reached_end and pump is not None and pump.media_parts and not outcome.fatal:
            # the relay closes the tunnel after the last recording instead of sending "finished"
            reached_end = bool(self._segments) and position >= self._segments[-1][1] - _EDGE_S
        if not reached_end:
            return outcome
        nxt = self._timeline.next_start(position)
        if nxt is not None and nxt < self._end_utc and nxt > start:
            logger.debug("playback %s: continuing with the next recording", self._host_label())
            with self._pb_lock:
                self._resume_at = nxt
            return _Outcome(played=True, switched=True)
        return self._finish()


class RelayPlaybackStreamSupervisor(PlaybackStreamSupervisor):
    """Playback of a remote / shared camera through the TP-Link cloud relay (playback slot ``streamType``
    1, ``channelId`` "0", no resolution / preConnection, ``customParams`` "{}"; plaintext TS in the
    tunnel; heartbeat every 15 s) [spec:remote.client_recipe.playback]. Same behaviour as
    :class:`PlaybackStreamSupervisor`; every attempt (start, seek, reconnect) takes a new relay slot.
    ``device_id`` is the cloud deviceId (also the default limiter key).
    """

    def __init__(self, relay: Any, device_id: str, width: int = 640, height: int = 360, *,
                 device_type: str = "SMART.IPCAMERA", model: str = "", cloud_type: int = 2,
                 heartbeat_interval: float | None = None, limiter_key: str | None = None, **kwargs: Any):
        from tapo_desktop.core.cloud.relay import HEARTBEAT_INTERVAL_S

        kwargs.pop("session_factory", None)
        kwargs.pop("port", None)
        kwargs.pop("local_token", None)
        self._relay = relay
        self._device_type = device_type
        self._model = model
        self._cloud_type = int(cloud_type)
        self._heartbeat_interval = float(heartbeat_interval or HEARTBEAT_INTERVAL_S)
        super().__init__(device_id, "", width, height, limiter_key=limiter_key or device_id,
                         session_factory=self._relay_session, **kwargs)

    def _pseudo_url(self, resolution: str) -> str:
        return f"tapo-relay-playback://{self._host}/{self._channel}"

    def _relay_session(self) -> MediaSession:
        from tapo_desktop.core.cloud.relay_vod import RelayPlaybackSession

        return RelayPlaybackSession(self._relay, self._host, download=False, device_type=self._device_type,
                                    model=self._model, cloud_type=self._cloud_type, timeout=self._io_timeout,
                                    heartbeat_interval=self._heartbeat_interval)
