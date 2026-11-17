"""Live view over the camera's local media port (:8800 ``POST /stream``), like the Tapo phone app.

On the LAN the phone app does not use RTSP: it opens the same Digest-authenticated media session
that is used for recordings ([spec:stream.local8800.*]) with user ``admin`` and the TP-Link ID
password, sends a preview request [spec:stream.control.preview] and receives the live MPEG-TS in
(AES-encrypted) multipart parts. :class:`LocalStreamSupervisor` does the same and feeds the
decrypted TS into an ffmpeg child (``-f mpegts -i pipe:0``) that outputs rgb24 frames exactly like
:class:`~tapo_desktop.core.media_engine.supervisor.StreamSupervisor`, whose state machine
(reconnect / backoff / stall watchdog / lease / hwaccel fallback / stats) it reuses.

Threads per running stream: the worker (state machine, handshake, watchdog), one pump thread per
attempt (reads parts, acks, writes to ffmpeg's stdin) and the decoder's drain threads. The pump's
blocking pipe write is the backpressure: when ffmpeg falls behind, the pump stops reading the
socket and TCP flow control slows the camera down. Nothing is queued in Python.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterator

from tapo_desktop.core.errors import StreamLimitError, TapoAuthError, TapoError
from tapo_desktop.core.media_engine._decoder import FfmpegDecoder
from tapo_desktop.core.media_engine._netprobe import tcp_reachable
from tapo_desktop.core.media_engine.ffmpeg import build_ffmpeg_args
from tapo_desktop.core.media_engine.job_object import JobObject
from tapo_desktop.core.media_engine.limiter import StreamLimiter
from tapo_desktop.core.media_engine.supervisor import (
    _POLL_S,
    _PROBE_TIMEOUT_S,
    Frame,
    StreamState,
    StreamSupervisor,
    _Outcome,
    _Scrubber,
    _Target,
)
from tapo_desktop.core.vod.session import MediaPart, MediaSession

__all__ = ["LocalStreamSupervisor", "build_preview_request", "build_stop_request", "PREVIEW_RESOLUTIONS"]

logger = logging.getLogger(__name__)

PREVIEW_RESOLUTIONS = ("HD", "VGA")  # HD = main stream, VGA = minor stream [spec:stream.control.preview]
_ACK_EVERY = 25  # [spec:vod.stream.flow_control_ack]
_STREAM_LIMIT_CODE = -52407  # [spec:stream.limits.concurrency] TOO_MANY_CLIENT
_LIMIT_CODES = {-52407, -52405, -52419, -52435}
_MIN_IO_TIMEOUT_S = 3.0
_PUMP_JOIN_S = 2.0
# device notifications after which the preview will not continue on this connection
_END_EVENTS = {"connection_closed", "stream_finish", "device_locked", "internal_error"}


def build_preview_request(resolution: str, channel: int = 0) -> dict:
    """Preview request without ``seq`` (``MediaSession.send_json`` adds it) [spec:stream.control.preview]."""
    if resolution not in PREVIEW_RESOLUTIONS:
        raise ValueError(f"resolution must be one of {PREVIEW_RESOLUTIONS}, not {resolution!r}")
    if channel < 0:
        raise ValueError("channel must be >= 0")
    return {"type": "request", "params": {"method": "get", "preview": {
        "channels": [int(channel)], "resolutions": [resolution], "audio": ["default"]}}}


def build_stop_request() -> dict:
    """``do stop`` request without ``seq`` [spec:stream.control.preview ``stop``]."""
    return {"type": "request", "params": {"method": "do", "stop": "null"}}


@dataclass(frozen=True)
class _LocalTarget(_Target):
    resolution: str = "VGA"


def _json_body(part: MediaPart) -> dict | None:
    if "json" not in part.headers.get("Content-Type", "").lower():
        return None
    try:
        msg = json.loads(part.body)
    except (ValueError, UnicodeDecodeError):
        return None
    return msg if isinstance(msg, dict) else None


class _Pump(threading.Thread):
    """Reads device parts after the preview was accepted: TS -> ffmpeg stdin, acks, end events."""

    def __init__(self, session: MediaSession, parts: Iterator[MediaPart], decoder: FfmpegDecoder,
                 wake: threading.Event, name: str):
        super().__init__(name=name, daemon=True)
        self._session = session
        self._parts = parts
        self._decoder = decoder
        self._wake = wake
        self._halt = threading.Event()
        self.reason: str | None = None  # why the pump ended (None while running)
        self.fatal = False
        self.media_parts = 0

    def halt(self) -> None:
        self._halt.set()

    def run(self) -> None:
        reason = "camera closed the media connection"
        try:
            for part in self._parts:
                if self._halt.is_set():
                    reason = "stopped"
                    break
                msg = _json_body(part)
                if msg is not None:
                    end = self._on_json(msg)
                    if end:
                        reason = end
                        break
                    continue
                if "mp2t" not in part.headers.get("Content-Type", "").lower() or not part.body:
                    continue
                if part.headers.get("Content-Type", "").lower().startswith("audio/"):
                    continue  # talkback / separate audio TS: not part of the video mux
                if not self._decoder.write(part.body):
                    reason = "decoder input closed"
                    break
                self.media_parts += 1
                self._ack_if_due(part)
        except TapoError as exc:
            reason = str(exc)
        except Exception:  # pragma: no cover - defensive: never let the pump die silently
            logger.exception("media pump crashed")
            reason = "internal error in media pump"
        finally:
            self.reason = reason
            self._wake.set()

    def _ack_if_due(self, part: MediaPart) -> None:
        raw = part.headers.get("X-Data-Sequence", "")
        if raw.isdigit() and int(raw) and int(raw) % _ACK_EVERY == 0 and self._session.session_id:
            self._session.send_ack(int(raw))

    def _on_json(self, msg: dict) -> str | None:
        """Handle a JSON part; return a reason when the stream ended."""
        kind = msg.get("type")
        if kind == "notification":
            params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
            event = params.get("event_type")
            if event in _END_EVENTS:
                detail = params.get("close_reason") or params.get("reason") or params.get("error_code")
                return f"camera ended the preview ({event}{': ' + str(detail) if detail else ''})"
            return None
        if kind == "error":
            code = msg.get("error_code")
            self.fatal = code in _LIMIT_CODES
            return f"{'stream limit reached: ' if self.fatal else ''}camera error {code}"
        return None


class LocalStreamSupervisor(StreamSupervisor):
    """Live view of one camera over :8800, same public surface as :class:`StreamSupervisor`.

    * ``password`` is the TP-Link ID (cloud) password; user ``admin`` [spec:stream.local8800.auth.digest].
      ``local_token`` (the camera's cloud-issued LAT) replaces it as the digest secret when given
      [spec:lat.media.digest].
    * ``resolution`` ``"HD"`` (main stream) or ``"VGA"`` (minor stream) is what the camera sends;
      frames are always scaled/padded to ``width`` x ``height``.
    * Wrong password -> FAILED, reason contains "auth" (no retry, no ffmpeg spawned).
      HTTP 503 / -52407 or a limiter refusal -> FAILED, reason contains "limit".
    * Any other failure, a camera-side close or a stall -> RECONNECTING with backoff, like
      StreamSupervisor (first frame after recovery normally < 3 s).
    * ``switch(resolution, width, height)`` restarts the preview on a new connection, keeping the lease.
    * ``limiter_key`` defaults to ``host``.
    * ``session_factory(resolution)`` replaces the direct :8800 :class:`MediaSession` of each attempt
      (same surface; used by :class:`~tapo_desktop.core.media_engine.relay_stream.RelayStreamSupervisor`);
      the TCP reachability probe is skipped then. A ``TapoError`` with ``fatal=True`` raised while opening
      the preview goes to FAILED without retrying.
    """

    _FEEDS_STDIN = True

    def __init__(
        self,
        host: str,
        password: str,
        width: int = 640,
        height: int = 360,
        *,
        resolution: str = "VGA",
        port: int = 8800,
        channel: int = 0,
        on_frame: Callable[[Frame], None],
        on_state: Callable[[StreamState, str | None], None] | None = None,
        on_audio: Callable[[bytes], None] | None = None,
        ffmpeg_path: str | None = None,
        hwaccel: str = "auto",
        limiter: StreamLimiter | None = None,
        limiter_key: str | None = None,
        job: JobObject | None = None,
        backoff_initial: float = 0.5,
        backoff_max: float = 10.0,
        stall_timeout: float = 5.0,
        max_retries: int | None = None,
        local_token: str | None = None,
        session_factory: Callable[[str], MediaSession] | None = None,
    ):
        if not host or not isinstance(host, str):
            raise ValueError("host must be a non-empty string")
        if not 0 < int(port) < 65536:
            raise ValueError("port must be 1..65535")
        build_preview_request(resolution, channel)  # validates resolution / channel
        self._host = host
        self._port = int(port)
        self._password = password or ""
        self._local_token = local_token or None
        self._session_factory = session_factory
        self._channel = int(channel)
        self._io_timeout = max(_MIN_IO_TIMEOUT_S, stall_timeout + 1.0)
        self._session: MediaSession | None = None
        self._pump: _Pump | None = None
        super().__init__(
            self._pseudo_url(resolution), width, height,
            on_frame=on_frame, on_state=on_state, on_audio=on_audio, ffmpeg_path=ffmpeg_path,
            hwaccel=hwaccel, limiter=limiter, limiter_key=limiter_key or host, job=job,
            backoff_initial=backoff_initial, backoff_max=backoff_max, stall_timeout=stall_timeout,
            max_retries=max_retries,
        )
        self._target = _LocalTarget(self._target.url, width, height, resolution)

    def _pseudo_url(self, resolution: str) -> str:
        """Password-free identifier used for logs and the thread name."""
        host = f"[{self._host}]" if ":" in self._host and not self._host.startswith("[") else self._host
        return f"tapo-local://{host}:{self._port}/{self._channel}/{resolution}"

    def __repr__(self) -> str:
        return (f"LocalStreamSupervisor('{self._host}:{self._port}', resolution={self._target.resolution}, "
                f"state={self.state.name})")

    # ================================================================== public API
    def switch(self, resolution: str, width: int, height: int) -> None:  # type: ignore[override]
        """Switch preview resolution / output size (grid VGA -> fullscreen HD) keeping the lease."""
        if width <= 0 or height <= 0:
            raise ValueError("width and height must be positive")
        build_preview_request(resolution, self._channel)
        target = _LocalTarget(self._pseudo_url(resolution), width, height, resolution)
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            if running:
                self._pending = target
            else:
                self._target = target
        if running:
            logger.info("switching %s:%d preview to %s at %dx%d", self._host, self._port, resolution, width, height)
            self._wake.set()

    # ================================================================== hooks
    def _host_label(self) -> str:
        return self._host

    def _decoder_command(self, exe: str, target: _Target, hwaccel: str | None, audio_url: str | None) -> list[str]:
        return [exe, *build_ffmpeg_args("pipe:0", target.width, target.height, hwaccel=hwaccel,
                                        audio_url=audio_url, input_format="mpegts")]

    def _interrupt(self) -> None:
        """stop(): politely end the preview, then close the socket to unblock the worker and pump."""
        with self._lock:
            session = self._session
        if session is None:
            return
        if session.session_id:
            try:
                session.send_json(build_stop_request())
            except TapoError:
                pass
        session.close()

    def _hide(self, text: str) -> str:
        text = _Scrubber(self._target.url)(text)
        for secret in (self._password, self._local_token or ""):
            if len(secret) >= 3:
                text = text.replace(secret, "***")
        return text

    def _fail(self, reason: str) -> None:
        super()._fail(self._hide(reason))

    # ================================================================== one attempt
    def _new_session(self, target: _LocalTarget) -> MediaSession:
        """The media session of one attempt: ``session_factory(resolution)`` when given (e.g. the cloud
        relay), else a direct :8800 connection."""
        if self._session_factory is not None:
            return self._session_factory(target.resolution)
        return MediaSession(self._host, self._password, port=self._port, timeout=self._io_timeout,
                            local_token=self._local_token)

    def _probe(self) -> str | None:
        """Cheap reachability check before the handshake (None = go ahead). Skipped for injected sessions."""
        if self._session_factory is not None:
            return None
        return tcp_reachable(self._host, self._port, min(_PROBE_TIMEOUT_S, self._io_timeout), self._stopping)

    def _attempt(self, exe: str, target: _Target) -> _Outcome:
        assert isinstance(target, _LocalTarget)
        session = self._new_session(target)
        with self._lock:
            self._session = session
        try:
            if self._stopping.is_set():
                return _Outcome(reason="stopped")
            opened = self._open_preview(session, target)
            if isinstance(opened, _Outcome):
                return opened
            parts, early = opened
            return self._stream(exe, target, session, parts, early)
        finally:
            session.close()
            with self._lock:
                if self._session is session:
                    self._session = None

    def _open_preview(self, session: MediaSession,
                      target: _LocalTarget) -> tuple[Iterator[MediaPart], list[MediaPart]] | _Outcome:
        """Handshake + preview request; returns (parts iterator, media parts that preceded the
        response) or an _Outcome when the attempt is over."""
        try:
            problem = self._probe()
            if problem is not None:
                return _Outcome(reason=problem)
            session.connect()
            if self._stopping.is_set():
                return _Outcome(reason="stopped")
            seq = session.send_json(build_preview_request(target.resolution, self._channel))
            parts = session.parts()
            early: list[MediaPart] = []
            for part in parts:
                msg = _json_body(part)
                if msg is None:
                    if len(early) < 8:
                        early.append(part)
                    continue
                if msg.get("type") == "response" and msg.get("seq") in (seq, None):
                    result = msg.get("result") if isinstance(msg.get("result"), dict) else {}
                    code = result.get("error_code", 0)
                    if code in _LIMIT_CODES:
                        return _Outcome(reason=f"stream limit reached: camera refused the preview ({code})",
                                        fatal=True)
                    if code:
                        return _Outcome(reason=f"camera refused the preview (error_code {code})")
                    return parts, early
                if msg.get("type") == "error":
                    return _Outcome(reason=f"camera error {msg.get('error_code')}")
            return _Outcome(reason="stopped" if self._stopping.is_set() else "camera closed the media connection")
        except TapoAuthError as exc:
            return _Outcome(reason=self._hide(f"auth failed: {exc}"), fatal=True)
        except StreamLimitError as exc:
            return _Outcome(reason=self._hide(f"stream limit reached: {exc}"), fatal=True)
        except TapoError as exc:
            reason = "stopped" if self._stopping.is_set() else self._hide(str(exc))
            # errors that know retrying cannot help (e.g. relay: camera offline) end the stream at once
            return _Outcome(reason=reason, fatal=bool(getattr(exc, "fatal", False)))

    def _stream(self, exe: str, target: _LocalTarget, session: MediaSession, parts: Iterator[MediaPart],
                early: list[MediaPart]) -> _Outcome:
        if self._stopping.is_set():
            return _Outcome(reason="stopped")
        if self._pending is not None:
            return _Outcome(switched=True)
        decoder = self._spawn_decoder(exe, target)
        if decoder is None:
            return _Outcome(reason="cannot start ffmpeg")
        pump = _Pump(session, self._chain(early, parts), decoder, self._wake, name=f"pump-{self._host}")
        with self._lock:
            self._pump = pump
        try:
            pump.start()
            outcome = self._watch_local(decoder, pump)
        finally:
            pump.halt()
            if session.session_id and (self._stopping.is_set() or self._pending is not None):
                try:
                    session.send_json(build_stop_request())
                except TapoError:
                    pass
            session.close()
            decoder.kill()
            pump.join(_PUMP_JOIN_S)
            if pump.is_alive():
                logger.warning("media pump for %s did not finish within %.1f s", self._host, _PUMP_JOIN_S)
            decoder.close()
            with self._lock:
                if self._decoder is decoder:
                    self._decoder = None
                if self._pump is pump:
                    self._pump = None
        outcome.reason = self._hide(outcome.reason)
        return self._classify(decoder, outcome, _Scrubber(target.url))

    @staticmethod
    def _chain(early: list[MediaPart], parts: Iterator[MediaPart]) -> Iterator[MediaPart]:
        yield from early
        yield from parts

    def _watch_local(self, decoder: FfmpegDecoder, pump: _Pump) -> _Outcome:
        """Poll until stop, switch, pump end, ffmpeg exit or a stall."""
        while True:
            self._wake.wait(_POLL_S)
            self._wake.clear()
            if self._stopping.is_set():
                return _Outcome(played=decoder.played, reason="stopped")
            if self._pending is not None:
                return _Outcome(played=decoder.played, switched=True)
            if pump.reason is not None:
                return _Outcome(played=decoder.played, reason=pump.reason, fatal=pump.fatal)
            if decoder.exited:
                return _Outcome(played=decoder.played, reason=f"ffmpeg exited (code {decoder.returncode})")
            idle = time.monotonic() - decoder.last_activity
            if idle > self._stall_timeout:
                what = "no frame" if decoder.played else "no first frame"
                return _Outcome(played=decoder.played, reason=f"{what} for {idle:.1f} s (stalled)")
