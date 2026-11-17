"""ffmpeg-based RTSP stream supervisor: decode, reconnect, stall detection, concurrency lease.

Threads per running stream: one worker (owns the state machine and the ffmpeg child) plus the
drain threads of the current :class:`~tapo_desktop.core.media_engine._decoder.FfmpegDecoder`
(frames, stderr and, when ``on_audio`` is set, audio). ``on_frame`` / ``on_audio`` run on the
drain threads, ``on_state`` on whichever thread made the transition; keep them short.
"""
from __future__ import annotations

import collections
import enum
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import unquote, urlsplit

from tapo_desktop.core.errors import StreamLimitError
from tapo_desktop.core.media_engine._decoder import AudioListener, FfmpegDecoder
from tapo_desktop.core.media_engine._netprobe import probe_address, tcp_reachable
from tapo_desktop.core.media_engine.ffmpeg import build_ffmpeg_args, find_ffmpeg
from tapo_desktop.core.media_engine.job_object import JobObject, default_job
from tapo_desktop.core.media_engine.limiter import StreamLease, StreamLimiter
from tapo_desktop.core.media_engine.profiles import redact_url

__all__ = ["StreamState", "Frame", "StreamSupervisor"]

logger = logging.getLogger(__name__)

_AUTO_HWACCEL = "d3d11va"
_SOFTWARE_NAMES = {"", "none", "software", "sw", "cpu"}
_HW_FAILURE_MARKERS = ("hwaccel", "hardware", "d3d11", "dxva", "device creation", "hw_frames")
_RECENT_PLAYING_S = 30.0  # while the stream played this recently, retries stay quick
_RECENT_BACKOFF_CAP_S = 0.5
_PROBE_TIMEOUT_S = 3.0
_POLL_S = 0.1
_FPS_WINDOW = 32


class StreamState(enum.Enum):
    IDLE = "idle"
    CONNECTING = "connecting"
    PLAYING = "playing"
    RECONNECTING = "reconnecting"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True)
class Frame:
    """One decoded video frame, rgb24, ``len(data) == width * height * 3``."""

    width: int
    height: int
    data: bytes
    index: int
    timestamp: float  # time.monotonic() when the frame was fully read


@dataclass(frozen=True)
class _Target:
    url: str
    width: int
    height: int


@dataclass
class _Outcome:
    """How one ffmpeg attempt ended."""

    played: bool = False
    reason: str = ""
    switched: bool = False
    hw_unusable: bool = False
    no_audio_stream: bool = False
    fatal: bool = False  # retrying cannot help (e.g. wrong password): go to FAILED at once


class _Scrubber:
    """Removes the URL password (raw and percent-encoded) from ffmpeg output and reasons."""

    def __init__(self, url: str):
        self._pairs: list[tuple[str, str]] = [(url, redact_url(url))]
        try:
            password = urlsplit(url).password
        except ValueError:
            password = None
        if password:
            self._pairs += [(password, "***"), (unquote(password), "***")]

    def __call__(self, text: str) -> str:
        for secret, replacement in self._pairs:
            if secret:
                text = text.replace(secret, replacement)
        return text


class StreamSupervisor:
    """Owns one ffmpeg child that decodes ``url`` to rgb24 frames of exactly ``width`` x ``height``.

    Behaviour (binding, see docs/CONTRACT_MEDIA.md):
    * ``start()`` is non-blocking; state goes IDLE -> CONNECTING -> PLAYING (first frame).
    * ffmpeg exit or no frame for ``stall_timeout`` s -> RECONNECTING, retry with exponential
      backoff (``backoff_initial`` .. ``backoff_max``), reset after a successful frame; while the
      stream was recently playing the backoff stays small so the first frame after the camera is
      reachable again arrives within 3 s. ``max_retries`` consecutive failures -> FAILED.
    * Holds a ``limiter`` lease (key ``limiter_key`` or the URL host) while running; if acquire
      fails -> FAILED with a reason mentioning the limit, without spawning ffmpeg.
    * ``hwaccel="auto"``: try d3d11va, fall back to software decoding when the hardware path fails
      before the first frame. Any other explicit hwaccel value that fails also falls back to software.
      ``stats()["decoder"]`` reports the decoder actually in use: ``"d3d11va"``/``<hwaccel>`` or ``"software"``.
    * Every ffmpeg child is assigned to ``job`` (``default_job()`` when None).
    * ``on_state(state, reason)`` is called on every transition; reasons/logs never contain the password.
    * ``on_audio`` receives s16le 8 kHz mono chunks (even length) when given.
    """

    def __init__(
        self,
        url: str,
        width: int = 640,
        height: int = 360,
        *,
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
    ):
        if width <= 0 or height <= 0:
            raise ValueError("width and height must be positive")
        if backoff_initial <= 0 or backoff_max < backoff_initial:
            raise ValueError("need 0 < backoff_initial <= backoff_max")
        if stall_timeout <= 0:
            raise ValueError("stall_timeout must be positive")
        self._target = _Target(url, width, height)
        self._pending: _Target | None = None
        self._on_frame = on_frame
        self._on_state = on_state
        self._on_audio = on_audio
        self._ffmpeg_path = ffmpeg_path
        self._hwaccel = self._resolve_hwaccel(hwaccel)
        self._limiter = limiter
        self._limiter_key = limiter_key
        self._job = job
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max
        self._stall_timeout = stall_timeout
        self._max_retries = max_retries

        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._state = StreamState.IDLE
        self._thread: threading.Thread | None = None
        self._lease: StreamLease | None = None
        self._decoder: FfmpegDecoder | None = None
        self._hw_disabled = self._hwaccel is None
        self._audio_disabled = False
        self._decoder_name = self._hwaccel or "software"
        self._frames = 0
        self._reconnects = 0
        self._last_frame_at: float | None = None
        self._frame_times: collections.deque[float] = collections.deque(maxlen=_FPS_WINDOW)
        self._callback_failures = 0

    def __repr__(self) -> str:
        return f"StreamSupervisor({redact_url(self._target.url)!r}, state={self._state.name})"

    @staticmethod
    def _resolve_hwaccel(hwaccel: str | None) -> str | None:
        if hwaccel is None or hwaccel.strip().lower() in _SOFTWARE_NAMES:
            return None
        return _AUTO_HWACCEL if hwaccel.strip().lower() == "auto" else hwaccel.strip()

    # ================================================================== public API
    def start(self) -> None:
        """Start the worker thread (non-blocking)."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping.clear()
            self._wake.clear()
            failure = self._acquire_lease()
            if failure is None:
                self._thread = threading.Thread(
                    target=self._run, name=f"stream-{self._host_label()}", daemon=True
                )
        if failure is not None:
            logger.warning("stream %s not started: %s", redact_url(self._target.url), failure)
            self._set_state(StreamState.FAILED, failure)
            return
        self._set_state(StreamState.CONNECTING, None)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        """Kill ffmpeg, join threads, release the lease; state -> STOPPED. Idempotent."""
        deadline = time.monotonic() + timeout
        with self._lock:
            self._stopping.set()
            thread = self._thread
            decoder = self._decoder
        self._wake.set()
        if decoder is not None:
            decoder.kill()
        self._interrupt()
        if thread is not None and thread is not threading.current_thread() and not (
            decoder is not None and decoder.owns_current_thread()
        ):
            thread.join(max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                logger.warning("stream worker for %s did not finish within %.1f s",
                               redact_url(self._target.url), timeout)
        self._release_lease()
        with self._lock:
            if self._thread is thread and (thread is None or not thread.is_alive()):
                self._thread = None
            was_idle = self._state is StreamState.IDLE
        if not was_idle:
            self._set_state(StreamState.STOPPED, None, force=True)

    def switch(self, url: str, width: int, height: int) -> None:
        """Switch to another URL / output size (e.g. stream2 grid -> stream1 fullscreen) keeping the lease."""
        if width <= 0 or height <= 0:
            raise ValueError("width and height must be positive")
        target = _Target(url, width, height)
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            if running:
                self._pending = target
            else:
                self._target = target
        if running:
            logger.info("switching stream to %s at %dx%d", redact_url(url), width, height)
            self._wake.set()

    @property
    def state(self) -> StreamState:
        return self._state

    @property
    def pid(self) -> int | None:
        """PID of the current ffmpeg child, None when none is running."""
        decoder = self._decoder
        return decoder.pid if decoder is not None else None

    def stats(self) -> dict:
        """``{"frames": int, "reconnects": int, "fps": float, "last_frame_age": float | None, "decoder": str}``."""
        with self._lock:
            times = list(self._frame_times)
            last = self._last_frame_at
            frames, reconnects, decoder = self._frames, self._reconnects, self._decoder_name
        age = None if last is None else time.monotonic() - last
        fps = 0.0
        if len(times) >= 2 and times[-1] > times[0] and age is not None and age < self._stall_timeout:
            fps = (len(times) - 1) / (times[-1] - times[0])
        return {"frames": frames, "reconnects": reconnects, "fps": round(fps, 2),
                "last_frame_age": age, "decoder": decoder}

    # ================================================================== lease / state
    def _host_label(self) -> str:
        try:
            return urlsplit(self._target.url).hostname or "stream"
        except ValueError:
            return "stream"

    def _acquire_lease(self) -> str | None:
        """Take the limiter slot (caller holds the lock). Returns a failure reason or None."""
        if self._limiter is None or self._lease is not None:
            return None
        key = self._limiter_key or self._host_label()
        try:
            self._lease = self._limiter.acquire(key)
        except StreamLimitError as exc:
            return f"stream limit reached: {exc}"
        return None

    def _release_lease(self) -> None:
        with self._lock:
            lease, self._lease = self._lease, None
        if lease is not None:
            lease.release()

    def _set_state(self, state: StreamState, reason: str | None, *, force: bool = False) -> None:
        with self._lock:
            if state is self._state:
                return
            if self._stopping.is_set() and not force and state is not StreamState.STOPPED:
                return
            self._state = state
        logger.debug("stream %s -> %s%s", self._host_label(), state.name, f" ({reason})" if reason else "")
        if self._on_state is not None:
            try:
                self._on_state(state, reason)
            except Exception:
                logger.exception("on_state callback failed")

    # ================================================================== hooks for subclasses
    _FEEDS_STDIN = False  # True: the subclass writes the input stream to ffmpeg's stdin

    def _decoder_command(self, exe: str, target: _Target, hwaccel: str | None, audio_url: str | None) -> list[str]:
        """Full ffmpeg command line for one attempt (RTSP URL input here)."""
        return [exe, *build_ffmpeg_args(target.url, target.width, target.height, hwaccel=hwaccel,
                                        audio_url=audio_url)]

    def _interrupt(self) -> None:
        """Called by stop() after killing ffmpeg: unblock any other blocking I/O of the worker."""

    # ================================================================== worker
    def _run(self) -> None:
        try:
            exe = self._ffmpeg_path or find_ffmpeg()
        except FileNotFoundError as exc:
            self._fail(str(exc))
            return
        try:
            self._supervise(exe)
        except Exception:
            logger.exception("stream worker for %s crashed", self._host_label())
            self._fail("internal error in stream worker")

    def _supervise(self, exe: str) -> None:
        backoff = self._backoff_initial
        failures = 0
        while not self._stopping.is_set():
            outcome = self._attempt(exe, self._take_target())
            if self._stopping.is_set():
                return
            if outcome.switched:
                backoff, failures = self._backoff_initial, 0
                self._set_state(StreamState.CONNECTING, "switching stream")
                continue
            if outcome.fatal:
                self._fail(outcome.reason)
                return
            if self._adapt(outcome):
                continue
            if outcome.played:
                backoff, failures = self._backoff_initial, 0
            else:
                failures += 1
            if self._max_retries is not None and failures > self._max_retries:
                self._fail(f"giving up after {failures} failed attempt(s): {outcome.reason}")
                return
            delay = self._retry_delay(backoff)
            if not outcome.played:
                backoff = min(backoff * 2, self._backoff_max)
            with self._lock:
                self._reconnects += 1
            logger.info("stream %s lost (%s); retrying in %.2f s", self._host_label(), outcome.reason, delay)
            self._set_state(StreamState.RECONNECTING, outcome.reason)
            self._sleep(delay)

    def _adapt(self, outcome: _Outcome) -> bool:
        """Apply configuration fallbacks; True means retry at once without counting a failure."""
        if outcome.hw_unusable and not self._hw_disabled:
            logger.warning("hardware decoding (%s) failed on %s; falling back to software (%s)",
                           self._hwaccel, self._host_label(), outcome.reason)
            with self._lock:
                self._hw_disabled = True
                self._decoder_name = "software"
            return True
        if outcome.no_audio_stream and not self._audio_disabled:
            logger.info("stream %s has no audio track; continuing video-only", self._host_label())
            self._audio_disabled = True
            return True
        return False

    def _retry_delay(self, backoff: float) -> float:
        last = self._last_frame_at
        if last is not None and time.monotonic() - last < _RECENT_PLAYING_S:
            return min(backoff, _RECENT_BACKOFF_CAP_S)
        return backoff

    def _sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self._stopping.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._wake.wait(remaining)
            self._wake.clear()
            if self._pending is not None:
                return

    def _take_target(self) -> _Target:
        with self._lock:
            if self._pending is not None:
                self._target, self._pending = self._pending, None
            return self._target

    def _fail(self, reason: str) -> None:
        scrubbed = _Scrubber(self._target.url)(reason)
        logger.warning("stream %s failed: %s", self._host_label(), scrubbed)
        self._set_state(StreamState.FAILED, scrubbed)
        self._release_lease()

    # ================================================================== one ffmpeg attempt
    def _attempt(self, exe: str, target: _Target) -> _Outcome:
        scrub = _Scrubber(target.url)
        address = probe_address(target.url)
        if address is not None:
            problem = tcp_reachable(*address, min(_PROBE_TIMEOUT_S, self._stall_timeout), self._stopping)
            if problem is not None:
                return _Outcome(reason=problem)
            if self._pending is not None:
                return _Outcome(switched=True)
        decoder = self._spawn_decoder(exe, target)
        if decoder is None:
            return _Outcome(reason="cannot start ffmpeg")
        try:
            outcome = self._watch(decoder)
        finally:
            decoder.close()
            with self._lock:
                if self._decoder is decoder:
                    self._decoder = None
        return self._classify(decoder, outcome, scrub)

    def _spawn_decoder(self, exe: str, target: _Target) -> FfmpegDecoder | None:
        hwaccel = None if self._hw_disabled else self._hwaccel
        audio = None
        if self._on_audio is not None and not self._audio_disabled:
            try:
                audio = AudioListener()
            except OSError as exc:
                logger.warning("audio disabled for this attempt: cannot open loopback listener (%s)", exc)
        command = self._decoder_command(exe, target, hwaccel, audio.url if audio else None)
        decoder = FfmpegDecoder(
            command, target.width, target.height, stdin_pipe=self._FEEDS_STDIN,
            on_frame=self._frame_sink(target, hwaccel), on_audio=self._deliver_audio, audio=audio,
            job=self._job_or_default(), wake=self._wake, name=f"ffmpeg-{self._host_label()}",
        )
        try:
            decoder.start()
        except OSError as exc:
            logger.error("cannot start ffmpeg %s: %s", exe, exc)
            decoder.close()
            return None
        with self._lock:
            self._decoder = decoder
            stopping = self._stopping.is_set()
        if stopping:
            decoder.kill()
        logger.debug("spawned ffmpeg pid %s for %s (%s)", decoder.pid, redact_url(target.url),
                     hwaccel or "software")
        return decoder

    def _job_or_default(self) -> JobObject | None:
        if self._job is not None:
            return self._job
        try:
            return default_job()
        except OSError as exc:
            logger.warning("no job object available, ffmpeg may outlive a crash: %s", exc)
            return None

    def _watch(self, decoder: FfmpegDecoder) -> _Outcome:
        """Poll until stop, switch, ffmpeg exit or a stall."""
        while True:
            self._wake.wait(_POLL_S)
            self._wake.clear()
            if self._stopping.is_set():
                return _Outcome(played=decoder.played, reason="stopped")
            if self._pending is not None:
                return _Outcome(played=decoder.played, switched=True)
            if decoder.exited:
                return _Outcome(played=decoder.played, reason=f"ffmpeg exited (code {decoder.returncode})")
            idle = time.monotonic() - decoder.last_activity
            if idle > self._stall_timeout:
                what = "no frame" if decoder.played else "no first frame"
                return _Outcome(played=decoder.played, reason=f"{what} for {idle:.1f} s (stalled)")

    def _classify(self, decoder: FfmpegDecoder, outcome: _Outcome, scrub: _Scrubber) -> _Outcome:
        lines = [scrub(line) for line in decoder.stderr_lines()]
        if lines and not outcome.switched:
            outcome.reason = f"{outcome.reason}: {lines[-1]}"
            logger.debug("ffmpeg stderr tail: %s", " | ".join(lines[-5:]))
        hw_attempted = not self._hw_disabled and self._hwaccel is not None
        if hw_attempted and not decoder.played and not outcome.switched:
            text = " ".join(lines).lower()
            outcome.hw_unusable = any(m in text for m in (*_HW_FAILURE_MARKERS, self._hwaccel.lower()))
        if not decoder.played and not outcome.switched and decoder.lacks_audio_stream():
            outcome.no_audio_stream = True
        return outcome

    # ================================================================== callbacks from drain threads
    def _frame_sink(self, target: _Target, hwaccel: str | None) -> Callable[[bytes, float], None]:
        decoder_name = hwaccel or "software"
        first = True

        def deliver(data: bytes, timestamp: float) -> None:
            nonlocal first
            if self._stopping.is_set():
                return
            with self._lock:
                index = self._frames
                self._frames += 1
                self._last_frame_at = timestamp
                self._frame_times.append(timestamp)
                decoder = self._decoder
                if first:
                    inline_sw = decoder is not None and decoder.hw_failed_inline
                    self._decoder_name = "software" if inline_sw else decoder_name
            if first:
                first = False
                self._set_state(StreamState.PLAYING, None)
            self._invoke(self._on_frame, Frame(target.width, target.height, data, index, timestamp))

        return deliver

    def _deliver_audio(self, chunk: bytes) -> None:
        if self._on_audio is not None and not self._stopping.is_set():
            self._invoke(self._on_audio, chunk)

    def _invoke(self, callback: Callable, arg: object) -> None:
        try:
            callback(arg)
        except Exception:
            self._callback_failures += 1
            if self._callback_failures <= 3:
                logger.exception("stream callback %r failed", getattr(callback, "__name__", callback))
