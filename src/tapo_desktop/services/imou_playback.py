"""iMou memory-card (SD) playback of one camera: play from a time, pause, jump, stop. Click only, 1x only.

How it works (the vendor documents no pause, seek or speed for these streams):

* a *request* is one ``createVideoDownloadStreamUrl`` call (``businessType=localRecord``) for a time range, at most
  :data:`MAX_PLAY_SPAN_S` long, inside one run of recordings. The range strings are the vendor's own (a position inside a
  run is the run's begin text plus whole seconds, see :mod:`tapo_desktop.core.imou.clock`);
* the end of a request chains the next one (rest of the run, or the next run when the player passed its clips);
  the end of the last run is ``STOPPED "end of recordings"``;
* **pause** stops the stream (no flow is spent), **resume** and **jump** ask for a new URL; the calls of one camera
  are at least :data:`SEEK_DEBOUNCE_S` apart and the last jump wins;
* no HLS fallback, no live address: a refused playback address is a plain error;
* one stream per camera: the slot of the camera moves from ``live`` to ``playback`` (the tile shows a fixed text; when
  the playback ends it gets one click text, live view does not come back by itself); the same cap and the same daily minutes as live view;
* the recording stream is paced by the supervisor (``-re``, fixed 10 fps); the position is counted from frames.
Nothing is logged but class names and vendor codes: never a URL, token, secret or file name.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from tapo_desktop.core.imou import texts
from tapo_desktop.core.imou.clock import ImouClock, clock_for
from tapo_desktop.core.imou.errors import PREFIX, ImouError, ImouOfflineError, clean
from tapo_desktop.core.imou.records import MAX_GAP_S
from tapo_desktop.core.imou.stream import PLAYBACK_FPS
from tapo_desktop.core.media_engine.supervisor import StreamState

log = logging.getLogger(__name__)

SEEK_DEBOUNCE_S = 1.5
#: shortcut: the vendor gives no maximum range; one hour per request until the hardware check says more is fine.
MAX_PLAY_SPAN_S = 3600.0
_POSITION_EVERY = 2  # frames: five reports a second at 10 fps


def merge_segments(segments: Any) -> list[tuple[float, float]]:
    """Sorted ``(start, end)`` runs from the player's clips; touching ones (gap up to 2 s) are joined."""
    runs: list[list[float]] = []
    for item in sorted((float(s), float(e)) for s, e in (segments or []) if e >= s):
        if runs and item[0] - runs[-1][1] <= MAX_GAP_S:
            runs[-1][1] = max(runs[-1][1], item[1])
        else:
            runs.append(list(item))
    return [(s, e) for s, e in runs]


class ImouPlayback:
    """State of the playback of one camera. ``service`` is the :class:`ImouCameraService` that owns it."""

    def __init__(self, service: Any):
        self._svc = service
        self._lock = threading.RLock()
        self.active = False            # this camera's slot is held by playback
        self._gen = 0
        self._sup: Any = None
        self._timer: threading.Timer | None = None
        self._seek_timer: threading.Timer | None = None
        self._started_at = 0.0
        self._last_url_at = 0.0        # time.monotonic() of the last URL call
        self._paused = False
        self._cbs: tuple | None = None  # on_frame, on_state, on_position
        self._size = (640, 360)
        self._runs: list[tuple[float, float]] = []
        self._pos = 0.0
        self._req_end = 0.0            # epoch where the current request ends
        self._run_end = 0.0            # epoch where its run ends

    # ================================================================== public (called by the service)
    def start(self, start_utc: float, end_utc: float, width: int, height: int, on_frame, on_state, on_position,
              segments: Any) -> None:
        svc = self._svc
        rt = svc.runtime_or_raise()
        cid = svc.config.id
        if not svc.is_online(rt):
            raise ImouOfflineError(texts.OFFLINE_IN_LIST)
        rt.guard.check()
        limits = rt.limits()
        runs = merge_segments(segments) or [(float(start_utc), max(float(end_utc), float(start_utc) + 1.0))]
        pos = self._snap(runs, float(start_utc))
        if pos is None:
            raise ImouError(texts.NO_RUN_AT)
        if rt.meter.remaining(cid, limits.daily_seconds) <= 0:
            raise ImouError(texts.budget_used(limits.daily_seconds))
        with self._lock:
            self._cancel_seek()
            self._stop_stream_locked()  # a running playback of this camera is replaced (its slot stays)
            svc.claim_slot_for_playback(rt, limits.max_live)
            self._gen += 1
            gen = self._gen
            self.active = True
            self._paused = False
            self._cbs = (on_frame, on_state, on_position)
            self._size = (int(width), int(height))
            self._runs = runs
        on_state(StreamState.CONNECTING, texts.DELAY_TEXT)
        try:
            self._launch(gen, pos)
        except BaseException:
            with self._lock:
                if gen == self._gen:
                    self._gen += 1
                    self._finish_locked(release=True)
            self._svc.playback_ended()
            raise

    def pause(self) -> None:
        with self._lock:
            if not self.active or self._paused:
                return
            self._gen += 1
            self._cancel_seek()
            self._paused = True
            self._stop_stream_locked()

    def resume(self) -> None:
        with self._lock:
            if not self.active or not self._paused:
                return
            self._paused = False
            self._gen += 1
            self._schedule(self._gen, self._pos)

    def seek(self, utc: float) -> None:
        with self._lock:
            if not self.active:
                return
            self._gen += 1
            self._paused = False
            self._stop_stream_locked()
            self._schedule(self._gen, float(utc))

    def stop(self) -> None:
        with self._lock:
            if not self.active and self._sup is None:
                return
            self._gen += 1
            self._finish_locked(release=True)
        self._svc.playback_ended()

    # ================================================================== requests
    def _snap(self, runs: list[tuple[float, float]], pos: float) -> float | None:
        """``pos`` when it is inside a run, the start of the next run when it is in a gap, ``None`` after the last."""
        for start, end in runs:
            if pos < start:
                return start
            if pos <= end:
                return pos
        return None

    def _plan(self, pos: float):
        """``(snapped position, begin text, end text, request end, run end)`` for the request that starts at ``pos``."""
        svc = self._svc
        snapped = self._snap(self._runs, pos)
        if snapped is None:
            raise ImouError(texts.NO_RUN_AT)
        run_end = next(e for s, e in self._runs if snapped <= e)
        req_end = min(run_end, snapped + MAX_PLAY_SPAN_S)
        req_end = max(req_end, snapped + 1.0)
        clock: ImouClock = clock_for(svc.config)
        base = svc.find_run(snapped)
        if base is not None:
            begin = clock.shift(base.begin, snapped - base.start_utc)
            end = base.end if abs(req_end - base.end_utc) < 0.5 else clock.shift(begin, req_end - snapped)
        else:
            begin, end = clock.from_utc(snapped), clock.from_utc(req_end)
        return snapped, begin, end, req_end, run_end

    def _launch(self, gen: int, pos: float) -> None:
        """Ask for the address of the request at ``pos`` (no lock held during the call) and start the stream."""
        svc = self._svc
        rt = svc.runtime_or_raise()
        limits = rt.limits()
        with self._lock:
            if gen != self._gen:
                return
        snapped, begin, end, req_end, run_end = self._plan(pos)
        remaining = rt.meter.remaining(svc.config.id, limits.daily_seconds)
        if remaining <= 0:
            raise ImouError(texts.budget_used(limits.daily_seconds))
        rt.guard.check()
        url = rt.client.create_playback_url(svc.serial, svc.channel, begin, end, svc.stream_type)
        self._last_url_at = time.monotonic()
        with self._lock:
            if gen != self._gen:
                return
            svc.take_over_live_for_playback(rt)
            self._pos, self._req_end, self._run_end = snapped, req_end, run_end
            width, height = self._size
            self._sup = rt.supervisor_factory(
                url=url, kind="rtsp", width=width, height=height, hwaccel="none",
                on_frame=lambda frame, g=gen, base=snapped: self._frame(g, base, frame),
                on_state=lambda state, reason, g=gen: self._sup_state(g, state, reason),
                stall_timeout=limits.stall_timeout, playback=True, on_clip_end=lambda g=gen: self._clip_end(g))
            self._started_at = rt.clock()
            self._arm_timer(limits, remaining, gen)
            sup = self._sup
        sup.start()

    def _schedule(self, gen: int, pos: float) -> None:
        """Start the request at ``pos`` after the debounce gap since the last URL call (last jump wins). Lock held."""
        self._cancel_seek()
        delay = max(0.0, self._last_url_at + SEEK_DEBOUNCE_S - time.monotonic())
        timer = threading.Timer(delay, self._scheduled, args=(gen, pos))
        timer.daemon = True
        self._seek_timer = timer
        timer.start()

    def _scheduled(self, gen: int, pos: float) -> None:
        try:
            self._launch(gen, pos)
        except Exception as exc:
            self._end(gen, str(exc) if isinstance(exc, ImouError) else PREFIX + "playback could not start")

    def _cancel_seek(self) -> None:
        timer, self._seek_timer = self._seek_timer, None
        if timer is not None:
            timer.cancel()

    # ================================================================== callbacks of the supervisor
    def _frame(self, gen: int, base: float, frame: Any) -> None:
        cbs = self._cbs
        if gen != self._gen or cbs is None:
            return
        pos = base + frame.index / PLAYBACK_FPS
        self._pos = pos
        cbs[0](frame)
        if frame.index % _POSITION_EVERY == 0:
            cbs[2](pos)

    def _sup_state(self, gen: int, state: StreamState, reason: str | None) -> None:
        cbs = self._cbs
        if gen != self._gen or cbs is None:
            return
        if state is StreamState.FAILED:
            self._end(gen, PREFIX + (clean(reason or "") or "the stream ended"))
        elif state is StreamState.CONNECTING:
            cbs[1](state, texts.DELAY_TEXT)
        elif state is StreamState.PLAYING:
            cbs[1](state, None)

    def _clip_end(self, gen: int) -> None:
        """The stream reached the end of its range (a clean end of file)."""
        with self._lock:
            if gen != self._gen or not self.active:
                return
            next_pos: float | None = None
            if self._req_end < self._run_end - 0.5:
                next_pos = self._req_end
            else:
                later = [s for s, _e in self._runs if s > self._run_end + 0.5]
                next_pos = later[0] if later else None
            if next_pos is None:
                self._gen += 1
                cbs = self._cbs
                self._finish_locked(release=True)
                done = True
            else:
                self._gen += 1
                new_gen = self._gen
                self._stop_stream_locked()
                done = False
        if done:
            if cbs is not None:
                cbs[1](StreamState.STOPPED, texts.END_OF_RECORDINGS)
            self._svc.playback_ended()
            return
        try:
            self._launch(new_gen, next_pos)
        except Exception as exc:
            self._end(new_gen, str(exc) if isinstance(exc, ImouError) else PREFIX + "playback could not continue")

    def _end(self, gen: int, text: str) -> None:
        """The run ended by itself (failure, run limit, daily minutes): clean up and tell the player."""
        with self._lock:
            if gen != self._gen:
                return
            self._gen += 1
            cbs = self._cbs
            self._finish_locked(release=True)
        if cbs is not None:
            cbs[1](StreamState.FAILED, text)
        self._svc.playback_ended()

    # ================================================================== timers and cleanup
    def _arm_timer(self, limits: Any, remaining: float, gen: int) -> None:
        budget_first = remaining <= limits.idle_seconds
        delay = remaining if budget_first else limits.idle_seconds
        text = texts.budget_used(limits.daily_seconds) if budget_first else texts.run_limit(limits.idle_seconds)
        self._cancel_timer()
        timer = threading.Timer(delay, self._end, args=(gen, text))
        timer.daemon = True
        self._timer = timer
        timer.start()

    def _cancel_timer(self) -> None:
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()

    def _stop_stream_locked(self) -> None:
        """Stop the stream and count its minutes; the slot and the session stay (pause, jump, chain)."""
        self._cancel_timer()
        sup, self._sup = self._sup, None
        if sup is not None:
            try:
                sup.stop()
            except Exception as exc:
                log.warning("imou: stopping a playback stream failed (%s)", type(exc).__name__)
        rt = self._svc.runtime()
        if self._started_at and rt is not None:
            rt.meter.add(self._svc.config.id, max(0.0, rt.clock() - self._started_at))
        self._started_at = 0.0

    def _stop_locked(self, *, release: bool) -> None:
        self._cancel_seek()
        self._stop_stream_locked()
        if release:
            self._release_slot()

    def _finish_locked(self, *, release: bool) -> None:
        self._stop_locked(release=release)
        self.active = False
        self._paused = False

    def _release_slot(self) -> None:
        rt = self._svc.runtime()
        if rt is not None:
            rt.release(self._svc.config.id, "playback")
