"""Lane-private helpers for media/vod integration tests (lane-tdd-media)."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

import psutil

from tapo_desktop.core.media_engine.supervisor import Frame, StreamState


@dataclass
class FrameMeta:
    width: int
    height: int
    size: int
    index: int
    timestamp: float


@dataclass
class Recorder:
    """Collects supervisor callbacks without retaining every frame buffer."""

    frames: list[FrameMeta] = field(default_factory=list)
    states: list[tuple[float, StreamState, str | None]] = field(default_factory=list)
    audio_chunks: list[tuple[float, int]] = field(default_factory=list)
    last_frame: Frame | None = None
    callback_errors: list[BaseException] = field(default_factory=list)

    def __post_init__(self):
        self._lock = threading.Lock()

    def on_frame(self, f: Frame) -> None:
        try:
            with self._lock:
                self.frames.append(FrameMeta(f.width, f.height, len(f.data), f.index, f.timestamp))
                self.last_frame = f
        except BaseException as exc:  # pragma: no cover
            self.callback_errors.append(exc)

    def on_state(self, state: StreamState, reason: str | None) -> None:
        with self._lock:
            self.states.append((time.monotonic(), state, reason))

    def on_audio(self, chunk: bytes) -> None:
        with self._lock:
            self.audio_chunks.append((time.monotonic(), len(chunk)))
            if len(chunk) % 2:
                self.callback_errors.append(AssertionError(f"odd audio chunk length {len(chunk)}"))

    # queries
    def frame_count(self) -> int:
        with self._lock:
            return len(self.frames)

    def frames_after(self, t: float) -> list[FrameMeta]:
        with self._lock:
            return [f for f in self.frames if f.timestamp > t]

    def audio_bytes_after(self, t: float) -> int:
        with self._lock:
            return sum(n for ts, n in self.audio_chunks if ts > t)

    def state_names(self) -> list[str]:
        with self._lock:
            return [s.name for _, s, _ in self.states]

    def first_state_time(self, state: StreamState, after: float = 0.0) -> float | None:
        with self._lock:
            for ts, s, _ in self.states:
                if s is state and ts >= after:
                    return ts
        return None

    def reasons(self) -> list[str]:
        with self._lock:
            return [r for _, _, r in self.states if r]

    def kwargs(self, audio: bool = False) -> dict:
        kw = {"on_frame": self.on_frame, "on_state": self.on_state}
        if audio:
            kw["on_audio"] = self.on_audio
        return kw


def wait_until(pred, timeout: float, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return bool(pred())


def ffmpeg_children() -> list[psutil.Process]:
    out = []
    for c in psutil.Process().children(recursive=True):
        try:
            if "ffmpeg" in c.name().lower():
                out.append(c)
        except psutil.NoSuchProcess:
            pass
    return out


def with_port(url: str, port: int) -> str:
    """Same RTSP URL (credentials, path) pointed at 127.0.0.1:<port> (e.g. a ChaosProxy)."""
    p = urlsplit(url)
    userinfo = p.netloc.rsplit("@", 1)[0] + "@" if "@" in p.netloc else ""
    return urlunsplit((p.scheme, f"{userinfo}127.0.0.1:{port}", p.path, p.query, p.fragment))


def url_password(url: str) -> str:
    return urlsplit(url).password or ""


def cloud_password(cam) -> str:
    return getattr(cam, "cloud_password", "cloudpass")
