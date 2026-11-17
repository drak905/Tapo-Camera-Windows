"""One ffmpeg decode child and the threads that drain it (private to the supervisor).

``FfmpegDecoder`` owns exactly one ``subprocess.Popen``:

* stdout (rgb24 rawvideo) is read by a reader thread in exact frame-size chunks with
  ``readinto`` into one reused buffer; each complete frame is handed to ``on_frame`` as ``bytes``;
* stderr is drained by a second thread into a short ring of lines (ffmpeg runs at ``-loglevel
  error`` so this is tiny); lines may contain the RTSP URL and must be scrubbed before use;
* when audio is wanted, ffmpeg connects to a loopback TCP listener owned by this object and a
  third thread forwards even-length s16le chunks to ``on_audio``.

The child is assigned to a Job Object immediately after it is spawned, so it can never outlive
the application. ``kill()`` + ``close()`` always reap the child and join every thread.
"""
from __future__ import annotations

import collections
import logging
import os
import socket
import subprocess
import threading
import time
from typing import Callable

from tapo_desktop.core.media_engine.job_object import JobObject

logger = logging.getLogger(__name__)

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_STDERR_LINES = 24
_AUDIO_CHUNK = 4096
_HW_INLINE_FAILURE = ("failed setup for format", "hwaccel initialisation returned error")
_NO_AUDIO_STREAM = "does not contain any stream"


class AudioListener:
    """Loopback TCP listener ffmpeg connects to for its s16le audio output."""

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self._sock.bind(("127.0.0.1", 0))
            self._sock.listen(1)
            self._sock.settimeout(0.2)
        except OSError:
            self._sock.close()
            raise

    @property
    def url(self) -> str:
        """ffmpeg output URL for this listener."""
        return f"tcp://127.0.0.1:{self._sock.getsockname()[1]}"

    def accept(self, closed: threading.Event) -> socket.socket | None:
        """Wait for ffmpeg's connection (loopback peers only) until ``closed`` is set."""
        while not closed.is_set():
            try:
                conn, peer = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return None
            if peer[0] == "127.0.0.1":
                return conn
            conn.close()
        return None

    def close(self) -> None:
        self._sock.close()


class FfmpegDecoder:
    """A single ffmpeg child decoding to rgb24 frames of ``width`` x ``height``."""

    def __init__(
        self,
        command: list[str],
        width: int,
        height: int,
        *,
        on_frame: Callable[[bytes, float], None],
        on_audio: Callable[[bytes], None] | None = None,
        audio: AudioListener | None = None,
        job: JobObject | None = None,
        wake: threading.Event | None = None,
        name: str = "ffmpeg",
        stdin_pipe: bool = False,
    ) -> None:
        self._command = command
        self._stdin_pipe = stdin_pipe
        self._frame_size = width * height * 3
        self._on_frame = on_frame
        self._on_audio = on_audio
        self._audio = audio
        self._job = job
        self._name = name
        self._proc: subprocess.Popen | None = None
        self._threads: list[threading.Thread] = []
        self._closed = threading.Event()
        self._stderr: collections.deque[str] = collections.deque(maxlen=_STDERR_LINES)
        self._eof = threading.Event()
        self.frames = 0
        self.started_at = 0.0
        self.last_activity = 0.0
        self.hw_failed_inline = False
        self._wake = wake  # set on EOF and on the first frame so the owner reacts at once

    # ------------------------------------------------------------------ lifetime
    def start(self) -> None:
        """Spawn ffmpeg, assign it to the job and start the drain threads. Raises OSError."""
        self.started_at = self.last_activity = time.monotonic()
        self._proc = subprocess.Popen(
            self._command,
            stdin=subprocess.PIPE if self._stdin_pipe else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            creationflags=_CREATE_NO_WINDOW,
        )
        self._assign_to_job()
        self._spawn(self._read_frames, "frames")
        self._spawn(self._read_stderr, "stderr")
        if self._audio is not None and self._on_audio is not None:
            self._spawn(self._read_audio, "audio")

    def _assign_to_job(self) -> None:
        if self._job is None or self._proc is None:
            return
        try:
            self._job.assign(self._proc)
        except ProcessLookupError:
            pass  # already exited: nothing left to contain, the reader will see EOF
        except OSError as exc:
            logger.warning("could not assign ffmpeg (pid %s) to the job object: %s", self._proc.pid, exc)

    def _spawn(self, target: Callable[[], None], role: str) -> None:
        thread = threading.Thread(target=target, name=f"{self._name}-{role}", daemon=True)
        self._threads.append(thread)
        thread.start()

    def kill(self) -> None:
        """Terminate the child (idempotent, never raises)."""
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    def close(self, timeout: float = 2.0) -> None:
        """Kill and reap the child, join the threads, close pipes and the audio listener."""
        deadline = time.monotonic() + timeout
        self._closed.set()
        self.kill()
        if self._audio is not None:
            self._audio.close()
        proc = self._proc
        if proc is not None:
            try:
                proc.wait(max(0.05, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                logger.warning("ffmpeg pid %s did not exit after kill", proc.pid)
        current = threading.current_thread()
        for thread in self._threads:
            if thread is not current:
                thread.join(max(0.05, deadline - time.monotonic()))
        if proc is not None:
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe is not None:
                    try:
                        pipe.close()
                    except OSError:
                        pass  # stdin: broken pipe after the kill

    def write(self, data: bytes) -> bool:
        """Write input bytes to ffmpeg's stdin (``stdin_pipe=True`` only), blocking while the pipe is full.

        The blocking write is the backpressure from the decoder to the network reader. Returns False
        once the child is gone or the pipe is closed (never raises).
        """
        proc = self._proc
        if proc is None or proc.stdin is None or self._closed.is_set():
            return False
        view = memoryview(data)
        try:
            while view:
                written = proc.stdin.write(view)
                if not written:
                    return False
                view = view[written:]
        except (OSError, ValueError):
            return False
        return True

    # ------------------------------------------------------------------ status
    @property
    def pid(self) -> int | None:
        proc = self._proc
        return proc.pid if proc is not None and proc.poll() is None else None

    @property
    def exited(self) -> bool:
        """True once stdout hit EOF or the process is gone."""
        proc = self._proc
        return self._eof.is_set() or proc is None or proc.poll() is not None

    @property
    def returncode(self) -> int | None:
        return None if self._proc is None else self._proc.poll()

    @property
    def played(self) -> bool:
        return self.frames > 0

    def owns_current_thread(self) -> bool:
        return threading.current_thread() in self._threads

    def stderr_lines(self) -> list[str]:
        return list(self._stderr)

    def lacks_audio_stream(self) -> bool:
        return any(_NO_AUDIO_STREAM in line.lower() for line in self._stderr)

    # ------------------------------------------------------------------ drain threads
    def _read_frames(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        readinto = self._proc.stdout.readinto
        size = self._frame_size
        buf = bytearray(size)
        view = memoryview(buf)
        try:
            while not self._closed.is_set():
                filled = 0
                while filled < size:
                    n = readinto(view[filled:])
                    if not n:
                        return
                    filled += n
                now = time.monotonic()
                self.frames += 1
                self.last_activity = now
                if self.frames == 1 and self._wake is not None:
                    self._wake.set()
                if self._closed.is_set():
                    return
                self._on_frame(bytes(buf), now)
        except (OSError, ValueError):
            return  # pipe closed underneath us during teardown
        finally:
            view.release()
            self._eof.set()
            if self._wake is not None:
                self._wake.set()

    def _read_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        read = self._proc.stderr.read
        pending = b""
        try:
            while True:
                chunk = read(4096)
                if not chunk:
                    break
                *lines, pending = (pending + chunk).split(b"\n")
                for raw in lines:
                    self._note_stderr(raw)
                pending = pending[-4096:]
        except (OSError, ValueError):
            pass
        if pending:
            self._note_stderr(pending)

    def _note_stderr(self, raw: bytes) -> None:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            return
        self._stderr.append(line)
        lowered = line.lower()
        if any(marker in lowered for marker in _HW_INLINE_FAILURE):
            self.hw_failed_inline = True

    def _read_audio(self) -> None:
        assert self._audio is not None and self._on_audio is not None
        conn = self._audio.accept(self._closed)
        if conn is None:
            return
        buf = bytearray(_AUDIO_CHUNK)
        carry = b""
        try:
            conn.settimeout(0.5)
            while not self._closed.is_set():
                try:
                    n = conn.recv_into(buf)
                except socket.timeout:
                    continue
                if not n:
                    return
                data = carry + buf[:n] if carry else bytes(buf[:n])
                even = len(data) & ~1
                carry = data[even:]
                if even and not self._closed.is_set():
                    self._on_audio(data[:even])
        except OSError:
            return
        finally:
            conn.close()
