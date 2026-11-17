"""Camera side of one continuous SD playback session (Phase 5, additive; used by the mock :8800 server
and the mock relay only when their continuous-playback knob is on).

:class:`PlaybackFeed` plays the recordings of a day from ``start_time`` to ``end_time`` like a real
camera: the recordings overlapping the range are streamed one after the other (gaps between them are
skipped), every unit is one video access unit of the cached clip looped by :class:`.ts_loop.TsLoop`
with real 90 kHz PTS (also sent as ``X-Data-PTS``). ``pts_mode``:

* ``"reset"`` (default) - every recording starts a new PTS sequence (the PTS jump back = a new file);
* ``"continuous"`` - PTS continue across recordings (the gap between them is not visible in the PTS).

Pacing: ``speed`` = stream seconds per wall second (``None`` / 0 = unpaced, as fast as the caller's ack
window allows - real cameras send faster than real time when acked). In-session controls
[spec:vod.playback.controls]: :meth:`pause` (``do pause``), :meth:`resume` (``do play "null"``),
:meth:`seek` (``do play {start_time}``); ``stop`` is the session's stop event. :attr:`finished` is True
after the last unit of the range was produced (the caller then sends ``stream_status``/``finished``).
"""
from __future__ import annotations

import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from . import media_assets, ts_loop

__all__ = ["FeedUnit", "PlaybackFeed", "recordings_of"]


@dataclass(frozen=True)
class FeedUnit:
    data: bytes
    pts: int  # 90 kHz video PTS of the unit (also X-Data-PTS)
    keyframe: bool
    utc: float  # recording time of the unit (epoch seconds)


def recordings_of(clips: Sequence) -> list[tuple[float, float]]:
    """``[(start, end)]`` epoch seconds of RecordingClip-like objects, sorted."""
    return sorted((c.start.timestamp(), c.end.timestamp()) for c in clips)


class PlaybackFeed:
    def __init__(self, recordings: Sequence[tuple[float, float]], start_time: float, end_time: float, *,
                 stop: threading.Event, speed: float | None = 1.0, pts_mode: str = "reset",
                 clip: bytes | None = None):
        if pts_mode not in ("reset", "continuous"):
            raise ValueError("pts_mode must be 'reset' or 'continuous'")
        self.recordings = sorted((float(s), float(e)) for s, e in recordings if e > s)
        self.start_time = float(start_time)
        self.end_time = float(end_time)
        self.stop = stop
        self.speed = speed
        self.pts_mode = pts_mode
        self._clip = clip
        self._cond = threading.Condition()
        self._paused = False
        self._seek: float | None = None
        self.finished = False
        self.pauses = 0
        self.resumes = 0
        self.seeks: list[float] = []
        self.position: float | None = None  # utc of the last produced unit

    # ------------------------------------------------------------------ controls
    def pause(self) -> None:
        with self._cond:
            self._paused = True
            self.pauses += 1
            self._cond.notify_all()

    def resume(self) -> None:
        with self._cond:
            self._paused = False
            self.resumes += 1
            self._cond.notify_all()

    def seek(self, utc: float) -> None:
        with self._cond:
            self._seek = float(utc)
            self._paused = False
            self.seeks.append(float(utc))
            self._cond.notify_all()

    def wake(self) -> None:
        """Wake a waiting :meth:`units` (call after setting ``stop``)."""
        with self._cond:
            self._cond.notify_all()

    @property
    def paused(self) -> bool:
        return self._paused

    # ------------------------------------------------------------------ stream
    def _next_recording(self, t: float) -> tuple[float, float] | None:
        for start, end in self.recordings:
            if end > t + 1e-6 and start < self.end_time:
                return start, end
        return None

    def _wait(self, due: float | None) -> str:
        """Block until ``due`` (monotonic; None = no pacing). Returns "go", "stop", "seek" or "resumed"."""
        resumed = False
        with self._cond:
            while True:
                if self.stop.is_set():
                    return "stop"
                if self._seek is not None:
                    return "seek"
                if self._paused:
                    resumed = True
                    self._cond.wait(0.05)
                    continue
                if resumed:
                    return "resumed"
                left = 0.0 if due is None else due - time.monotonic()
                if left <= 0:
                    return "go"
                self._cond.wait(min(left, 0.05))

    def units(self) -> Iterator[FeedUnit]:
        template = ts_loop.template_for(self._clip if self._clip is not None else media_assets.load_ts())
        t = self.start_time
        loop = ts_loop.TsLoop(template)
        speed = float(self.speed) if self.speed else 0.0
        while True:
            rec = self._next_recording(t)
            if rec is None:
                self.finished = True
                return
            seg_start, seg_end = rec
            t0 = max(t, seg_start)
            if self.pts_mode == "reset":
                loop = ts_loop.TsLoop(template)
            base_wall, base_utc = time.monotonic(), t0
            e0: float | None = None
            restart = False
            for unit in loop:
                if e0 is None:
                    e0 = unit.elapsed
                utc = t0 + (unit.elapsed - e0)
                if utc >= seg_end or utc >= self.end_time:
                    break
                due = base_wall + (utc - base_utc) / speed if speed > 0 else None
                what = self._wait(due)
                if what == "stop":
                    return
                if what == "seek":
                    with self._cond:
                        t, self._seek = float(self._seek or t), None
                    restart = True
                    break
                if what == "resumed":
                    base_wall, base_utc = time.monotonic(), utc
                self.position = utc
                yield FeedUnit(unit.data, unit.pts, unit.keyframe, utc)
            if restart:
                continue
            if self.end_time <= seg_end:
                self.finished = True
                return
            t = seg_end
