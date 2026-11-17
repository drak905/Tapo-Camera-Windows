"""Phase 5 test doubles (playback). tests/e2e/fakes.py is NOT edited: the playback-capable service is a
subclass of its FakeCameraService.

* :class:`FakePlaybackSupervisor` / :class:`PlaybackSupervisorFactory` - record how TapoCameraService
  builds (LAN) and RelayProvider builds (relay) playback supervisors and every control call.
* :class:`FakePlaybackService` - FakeCameraService + start/pause/resume/seek/speed/stop_playback that
  streams synthetic frames and a position advancing at the selected speed from a background thread.
* :class:`FakePlaybackFactory` - ``service_factory`` for AppController creating FakePlaybackService.
"""
from __future__ import annotations

import threading
import time

from tapo_desktop.core.media_engine.supervisor import Frame, StreamState
from tests.e2e.fakes import FakeCameraService, FakeServiceFactory

SPEEDS = {"1/1": 1.0, "2/1": 2.0, "4/1": 4.0, "8/1": 8.0}


class FakePlaybackSupervisor:
    def __init__(self, args: tuple, kwargs: dict):
        self.args, self.kwargs = args, kwargs
        self.state = StreamState.IDLE
        self.calls: list[tuple] = []
        self.pid = None
        self.paused = False

    def start(self) -> None:
        self.calls.append(("start",))
        self.state = StreamState.CONNECTING

    def stop(self) -> None:
        self.calls.append(("stop",))
        self.state = StreamState.STOPPED

    def pause(self) -> None:
        self.calls.append(("pause",))
        self.paused = True

    def resume(self) -> None:
        self.calls.append(("resume",))
        self.paused = False

    def seek(self, utc: float) -> None:
        self.calls.append(("seek", utc))

    def set_speed(self, scale: str) -> None:
        self.calls.append(("set_speed", scale))

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


class PlaybackSupervisorFactory:
    def __init__(self):
        self.created: list[FakePlaybackSupervisor] = []

    def __call__(self, *args, **kwargs) -> FakePlaybackSupervisor:
        sup = FakePlaybackSupervisor(args, kwargs)
        self.created.append(sup)
        return sup

    @property
    def last(self) -> FakePlaybackSupervisor:
        assert self.created, "no playback supervisor was created"
        return self.created[-1]


class FakePlaybackService(FakeCameraService):
    """FakeCameraService that can also play recordings (synthetic blue frames, moving position)."""

    def __init__(self, config, *, playback_error: Exception | None = None, playback_color=(20, 40, 200),
                 **kwargs):
        super().__init__(config, **kwargs)
        self.playback_error = playback_error
        self.playback_color = playback_color
        self.playback_args: tuple | None = None
        self.playback_kwargs: dict = {}
        self.position: float | None = None
        self.speed = 1.0
        self.paused = False
        self._pb_stop: threading.Event | None = None
        self._pb_thread: threading.Thread | None = None
        self._pb_lock = threading.Lock()

    def _pb_halt(self) -> bool:
        stop, thread = self._pb_stop, self._pb_thread
        self._pb_stop = self._pb_thread = None
        if stop is None:
            return False
        stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(2)
        return True

    def _pb_loop(self, stop, width, height, end, on_frame, on_state, on_position) -> None:
        on_state(StreamState.CONNECTING, None)
        index = 0
        last = time.monotonic()
        while not stop.is_set():
            now = time.monotonic()
            with self._pb_lock:
                if not self.paused:
                    self.position = (self.position or 0.0) + (now - last) * self.speed
                pos, paused = self.position, self.paused
            last = now
            if not paused:
                on_frame(Frame(width=width, height=height, data=bytes(self.playback_color) * (width * height),
                               index=index, timestamp=now))
                if index == 0:
                    on_state(StreamState.PLAYING, None)
                index += 1
                on_position(pos)
            if pos is not None and pos >= end:
                on_state(StreamState.STOPPED, "end of recordings")
                return
            stop.wait(0.03)

    def start_playback(self, start_utc, end_utc, width, height, on_frame, on_state, on_position, **kwargs) -> None:
        self._enter("start_playback", start_utc, end_utc, width, height)
        if self.playback_error is not None:
            raise self.playback_error
        self._pb_halt()
        self.playback_args = (start_utc, end_utc, width, height)
        self.playback_kwargs = dict(kwargs)
        with self._pb_lock:
            self.position, self.paused, self.speed = float(start_utc), False, 1.0
        stop = threading.Event()
        thread = threading.Thread(target=self._pb_loop, args=(stop, width, height, float(end_utc), on_frame,
                                                              on_state, on_position), daemon=True)
        self._pb_stop, self._pb_thread = stop, thread
        thread.start()

    def pause_playback(self) -> None:
        self._enter("pause_playback")
        with self._pb_lock:
            self.paused = True

    def resume_playback(self) -> None:
        self._enter("resume_playback")
        with self._pb_lock:
            self.paused = False

    def seek_playback(self, utc: float) -> None:
        self._enter("seek_playback", utc)
        with self._pb_lock:
            self.position = float(utc)
            self.paused = False

    def set_playback_speed(self, scale: str) -> None:
        self._enter("set_playback_speed", scale)
        if scale not in SPEEDS:
            raise ValueError(scale)
        with self._pb_lock:
            self.speed = SPEEDS[scale]

    def stop_playback(self) -> None:
        self._enter("stop_playback")
        self._pb_halt()
        self.playback_args = None

    def kill_threads(self) -> None:
        super().kill_threads()
        self._pb_halt()

    def close(self) -> None:
        self._pb_halt()
        super().close()


class FakePlaybackFactory(FakeServiceFactory):
    def __call__(self, config):  # type: ignore[override]
        svc = FakePlaybackService(config, **self.service_kwargs)
        with self._lock:
            for args in self._pending.get(config.id, []):
                svc.fail(*args)
            self.services[config.id] = svc
            self.created.append(svc)
            self.factory_threads.append(threading.get_ident())
        return svc
