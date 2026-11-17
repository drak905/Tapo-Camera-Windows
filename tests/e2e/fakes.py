"""Test doubles for the services/UI lane (owned by lane-tdd-ui).

* :class:`FakeCameraService` - in-memory :class:`CameraService`: records every call together with
  the calling thread ident, can be told to fail a method, streams synthetic rgb24 frames from a
  background thread, returns canned presets/recordings and simulates download progress.
* :class:`FakeServiceFactory` - ``service_factory`` for :class:`AppController`; remembers every
  service it created and pre-configures failures for services that do not exist yet.
* :class:`FakeVault` - duck-typed in-memory ``CredentialVault`` (set/get/delete/camera_ids).

Core types (``StreamState``/``Frame``) are taken from ``tapo_desktop.core.media_engine.supervisor``
when that module exists; otherwise structurally identical local stand-ins are used (the
controller only reads ``state.name`` and ``frame.width/height/data``).
"""
from __future__ import annotations

import enum
import pathlib
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from tapo_desktop.core.errors import TapoDeviceError, TapoError
from tapo_desktop.models import (
    CameraConfig,
    CredentialKind,
    Credentials,
    DayNightMode,
    DeviceInfo,
    MotionEvent,
    Preset,
    RecordingClip,
    StreamQuality,
)
from tapo_desktop.services.camera_service import CameraService

try:  # real types once lane-impl-media has landed
    from tapo_desktop.core.media_engine.supervisor import Frame, StreamState  # type: ignore
except Exception:  # pragma: no cover - fallback while the media lane is a stub

    class StreamState(enum.Enum):  # type: ignore[no-redef]
        IDLE = enum.auto()
        CONNECTING = enum.auto()
        PLAYING = enum.auto()
        RECONNECTING = enum.auto()
        STOPPED = enum.auto()
        FAILED = enum.auto()

    @dataclass(frozen=True)
    class Frame:  # type: ignore[no-redef]
        width: int
        height: int
        data: bytes
        index: int
        timestamp: float


DEFAULT_COLOR = (220, 30, 30)
CLOUD = Credentials("admin", "cloud-secret-1")
CAMERA = Credentials("camuser", "camera-secret-2")


def show_window(env, qtbot, *, tray_enabled: bool = False, size=(1100, 750)):
    """Create, register (env + qtbot) and show a MainWindow for ``env``."""
    from tapo_desktop.ui.main_window import MainWindow

    w = MainWindow(env.controller, tray_enabled=tray_enabled)
    qtbot.addWidget(w)
    env.windows.append(w)
    w.resize(*size)
    w.show()
    qtbot.waitExposed(w)
    return w


def default_status() -> dict:
    return {
        "privacy": False,
        "day_night": DayNightMode.AUTO,
        "led": False,
        "motion_detection": True,
        "alarm": False,
    }


def default_presets() -> list[Preset]:
    return [Preset(id="1", name="Door"), Preset(id="2", name="Window"), Preset(id="3", name="Garden")]


RECORDING_DAY = date(2024, 3, 9)


def default_recordings() -> dict[date, list[RecordingClip]]:
    base = datetime(2024, 3, 9, tzinfo=timezone.utc)
    return {
        RECORDING_DAY: [
            RecordingClip(base + timedelta(hours=1), base + timedelta(hours=1, minutes=5), 2),
            RecordingClip(base + timedelta(hours=8, minutes=30, seconds=15),
                          base + timedelta(hours=8, minutes=31), 1),
        ]
    }


def make_config(i: int, **overrides) -> CameraConfig:
    kwargs = dict(name=f"Cam {i}", host=f"192.0.2.{10 + i}", id=f"cam{i}")
    kwargs.update(overrides)
    return CameraConfig(**kwargs)


@dataclass
class ServiceCall:
    name: str
    args: tuple
    thread_id: int
    t: float = field(default_factory=time.monotonic)


class FakeCameraService(CameraService):
    def __init__(self, config: CameraConfig, *, presets: list[Preset] | None = None,
                 recordings: dict[date, list[RecordingClip]] | None = None,
                 status: dict | None = None, frame_color: tuple[int, int, int] = DEFAULT_COLOR,
                 frame_interval: float = 0.03, download_steps: int = 4, latency: float = 0.0):
        self.config = config
        self.calls: list[ServiceCall] = []
        self.status = dict(status or default_status())
        self.presets = list(default_presets() if presets is None else presets)
        self.recordings = default_recordings() if recordings is None else recordings
        self.frame_color = frame_color
        self.frame_interval = frame_interval
        self.download_steps = download_steps
        self.latency = latency
        self.closed = False
        self.frames_sent = 0
        self.on_frame = None
        self.on_state = None
        self.on_event = None
        self.stream_args: tuple | None = None  # (quality, width, height) of the running stream
        self._lock = threading.RLock()
        self._failures: dict[str, list] = {}  # method -> [exception, remaining (None = forever)]
        self._stream_stop: threading.Event | None = None
        self._stream_thread: threading.Thread | None = None

    # ----------------------------------------------------------------- test API
    def fail(self, method: str, exc: Exception | None = None, times: int | None = None) -> None:
        """Make ``method`` raise ``exc`` (default TapoDeviceError) ``times`` times (None = always)."""
        with self._lock:
            self._failures[method] = [exc or TapoDeviceError(-40210, method), times]

    def clear_failures(self) -> None:
        with self._lock:
            self._failures.clear()

    def calls_to(self, name: str) -> list[ServiceCall]:
        with self._lock:
            return [c for c in self.calls if c.name == name]

    def call_names(self) -> list[str]:
        with self._lock:
            return [c.name for c in self.calls]

    def emit_state(self, state_name: str, reason: str | None = None) -> None:
        """Invoke the current ``on_state`` callback from a worker thread (blocks until done)."""
        cb = self.on_state
        assert cb is not None, "start_stream was not called"
        t = threading.Thread(target=cb, args=(StreamState[state_name], reason), daemon=True)
        t.start()
        t.join(2)

    def emit_event(self, event: MotionEvent) -> None:
        """Invoke the current ``on_event`` callback from a worker thread (blocks until done)."""
        cb = self.on_event
        assert cb is not None, "start_events was not called"
        t = threading.Thread(target=cb, args=(event,), daemon=True)
        t.start()
        t.join(2)

    def kill_threads(self) -> None:
        self._halt_stream()

    # ----------------------------------------------------------------- internals
    def _enter(self, name: str, *args) -> None:
        with self._lock:
            self.calls.append(ServiceCall(name, args, threading.get_ident()))
            failure = self._failures.get(name)
            if failure is not None:
                exc, remaining = failure
                if remaining is not None:
                    failure[1] = remaining - 1
                    if failure[1] <= 0:
                        del self._failures[name]
            else:
                exc = None
        if self.latency:
            time.sleep(self.latency)
        if exc is not None:
            raise exc

    def _halt_stream(self) -> bool:
        stop, thread = self._stream_stop, self._stream_thread
        self._stream_stop = self._stream_thread = None
        if stop is None:
            return False
        stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(2)
        return True

    def _stream_loop(self, stop: threading.Event, width: int, height: int, on_frame, on_state) -> None:
        on_state(StreamState.CONNECTING, None)
        index = 0
        while not stop.is_set():
            data = bytes(self.frame_color) * (width * height)
            on_frame(Frame(width=width, height=height, data=data, index=index, timestamp=time.monotonic()))
            self.frames_sent += 1
            if index == 0:
                on_state(StreamState.PLAYING, None)
            index += 1
            stop.wait(self.frame_interval)

    # ----------------------------------------------------------------- CameraService
    def connect(self) -> DeviceInfo:
        self._enter("connect")
        return DeviceInfo(model="C210", alias=self.config.name, firmware="1.0.0")

    def start_stream(self, quality: StreamQuality, width: int, height: int, on_frame, on_state) -> None:
        self._enter("start_stream", quality, width, height)
        self._halt_stream()
        self.on_frame, self.on_state = on_frame, on_state
        self.stream_args = (quality, width, height)
        stop = threading.Event()
        thread = threading.Thread(target=self._stream_loop, args=(stop, width, height, on_frame, on_state),
                                  name=f"fake-stream-{self.config.id}", daemon=True)
        self._stream_stop, self._stream_thread = stop, thread
        thread.start()

    def stop_stream(self) -> None:
        self._enter("stop_stream")
        cb = self.on_state
        if self._halt_stream() and cb is not None:
            cb(StreamState.STOPPED, None)
        self.stream_args = None

    def ptz_move(self, dx: int, dy: int) -> None:
        self._enter("ptz_move", dx, dy)

    def ptz_stop(self) -> None:
        self._enter("ptz_stop")

    def get_presets(self) -> list[Preset]:
        self._enter("get_presets")
        return list(self.presets)

    def goto_preset(self, preset_id: str) -> None:
        self._enter("goto_preset", preset_id)

    def get_status(self) -> dict:
        self._enter("get_status")
        return dict(self.status)

    def set_privacy(self, on: bool) -> None:
        self._enter("set_privacy", on)
        self.status["privacy"] = on

    def set_day_night(self, mode: DayNightMode) -> None:
        self._enter("set_day_night", mode)
        self.status["day_night"] = mode

    def set_led(self, on: bool) -> None:
        self._enter("set_led", on)
        self.status["led"] = on

    def set_alarm(self, on: bool) -> None:
        self._enter("set_alarm", on)
        self.status["alarm"] = on

    def set_motion_detection(self, on: bool) -> None:
        self._enter("set_motion_detection", on)
        self.status["motion_detection"] = on

    def days_with_recordings(self, start: date, end: date) -> list[date]:
        self._enter("days_with_recordings", start, end)
        return sorted(d for d, clips in self.recordings.items() if clips and start <= d <= end)

    def recordings_for_day(self, day: date) -> list[RecordingClip]:
        self._enter("recordings_for_day", day)
        return list(self.recordings.get(day, []))

    def download_recording(self, clip: RecordingClip, out_path: pathlib.Path, progress=None) -> pathlib.Path:
        self._enter("download_recording", clip, pathlib.Path(out_path))
        steps = max(1, self.download_steps)
        for i in range(steps + 1):
            if progress is not None:
                progress(i / steps)
            time.sleep(0.01)
        out = pathlib.Path(out_path)
        out.write_bytes(b"\x00\x00\x00\x18ftypmp42fake")
        return out

    def start_events(self, on_event) -> None:
        self._enter("start_events")
        self.on_event = on_event

    def stop_events(self) -> None:
        self._enter("stop_events")

    def close(self) -> None:
        self._enter("close")
        self._halt_stream()
        self.closed = True


class FakeServiceFactory:
    """``service_factory`` for AppController. ``factory.services[id]`` = newest service for id."""

    def __init__(self, **service_kwargs):
        self.service_kwargs = service_kwargs
        self.services: dict[str, FakeCameraService] = {}
        self.created: list[FakeCameraService] = []
        self.factory_threads: list[int] = []
        self._pending: dict[str, list[tuple]] = defaultdict(list)
        self._lock = threading.Lock()

    def __call__(self, config: CameraConfig) -> FakeCameraService:
        svc = FakeCameraService(config, **self.service_kwargs)
        with self._lock:
            for args in self._pending.get(config.id, []):
                svc.fail(*args)
            self.services[config.id] = svc
            self.created.append(svc)
            self.factory_threads.append(threading.get_ident())
        return svc

    def fail(self, camera_id: str, method: str, exc: Exception | None = None, times: int | None = None) -> None:
        """Fail ``method`` on the current AND every future service for ``camera_id``."""
        with self._lock:
            self._pending[camera_id].append((method, exc, times))
            svc = self.services.get(camera_id)
        if svc is not None:
            svc.fail(method, exc, times)

    def created_for(self, camera_id: str) -> list[FakeCameraService]:
        with self._lock:
            return [s for s in self.created if s.config.id == camera_id]

    def shutdown(self) -> None:
        for svc in list(self.created):
            svc.kill_threads()


class FakeVault:
    """In-memory, duck-typed ``CredentialVault``."""

    def __init__(self):
        self._data: dict[tuple[str, CredentialKind], Credentials] = {}
        self._lock = threading.Lock()

    def set(self, camera_id: str, kind: CredentialKind, creds: Credentials) -> None:
        with self._lock:
            self._data[(camera_id, CredentialKind(kind))] = creds

    def get(self, camera_id: str, kind: CredentialKind) -> Credentials | None:
        with self._lock:
            return self._data.get((camera_id, CredentialKind(kind)))

    def delete(self, camera_id: str) -> None:
        with self._lock:
            for key in [k for k in self._data if k[0] == camera_id]:
                del self._data[key]

    def camera_ids(self) -> list[str]:
        with self._lock:
            return sorted({k[0] for k in self._data})


__all__ = [
    "CAMERA", "CLOUD", "DEFAULT_COLOR", "RECORDING_DAY", "FakeCameraService", "FakeServiceFactory", "FakeVault", "Frame",
    "ServiceCall", "StreamState", "TapoError", "default_presets", "default_recordings", "default_status",
    "make_config", "show_window",
]
