"""Small daemon-thread worker pool with per-key serial "strands" (private to the services layer).

Why not ``concurrent.futures.ThreadPoolExecutor``: its threads are joined at interpreter exit, so a
camera call stuck in a network timeout would delay shutdown. These workers are daemon threads that
exit after ``idle_timeout`` seconds without work and can be joined explicitly with ``shutdown``.

A :class:`Strand` runs its jobs one at a time in submission order on the shared pool, which gives
per-camera ordering (``ptz_move`` before ``ptz_stop``) without one thread per camera.
"""
from __future__ import annotations

import collections
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future

log = logging.getLogger(__name__)

Job = Callable[[], object]


class WorkerPool:
    """Runs callables on up to ``max_workers`` daemon threads and counts outstanding work."""

    def __init__(self, max_workers: int = 12, *, idle_timeout: float = 5.0, name: str = "tapo-worker"):
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        self._max_workers = max_workers
        self._idle_timeout = idle_timeout
        self._name = name
        self._cond = threading.Condition()
        self._queue: collections.deque[tuple[Job, Future]] = collections.deque()
        self._threads: set[threading.Thread] = set()
        self._idle = 0
        self._outstanding = 0  # queued + running
        self._counter = 0
        self._closed = False

    # ------------------------------------------------------------------ public API
    def submit(self, fn: Job) -> Future:
        """Queue ``fn``; returns a Future with its result/exception."""
        future: Future = Future()
        with self._cond:
            if self._closed:
                raise RuntimeError("worker pool is shut down")
            self._queue.append((fn, future))
            self._outstanding += 1
            if self._idle > 0:
                self._cond.notify()
            elif len(self._threads) < self._max_workers:
                self._spawn_locked()
        return future

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Block until no job is queued or running; False on timeout."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while self._outstanding:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._cond.wait(remaining)
            return True

    def shutdown(self, timeout: float = 3.0) -> None:
        """Stop accepting work, let queued jobs finish and join the threads (bounded by ``timeout``)."""
        with self._cond:
            self._closed = True
            self._cond.notify_all()
            threads = list(self._threads)
        deadline = time.monotonic() + timeout
        for t in threads:
            if t is not threading.current_thread():
                t.join(max(0.0, deadline - time.monotonic()))

    # ------------------------------------------------------------------ internals
    def _spawn_locked(self) -> None:
        self._counter += 1
        t = threading.Thread(target=self._run, name=f"{self._name}-{self._counter}", daemon=True)
        self._threads.add(t)
        t.start()

    def _next_job(self) -> tuple[Job, Future] | None:
        with self._cond:
            while not self._queue:
                if self._closed:
                    return None
                self._idle += 1
                notified = self._cond.wait(self._idle_timeout)
                self._idle -= 1
                if not notified and not self._queue:
                    return None  # idle expiry
            return self._queue.popleft()

    def _run(self) -> None:
        try:
            while True:
                item = self._next_job()
                if item is None:
                    return
                fn, future = item
                try:
                    if future.set_running_or_notify_cancel():
                        try:
                            future.set_result(fn())
                        except BaseException as exc:  # noqa: BLE001 - delivered through the Future
                            future.set_exception(exc)
                finally:
                    with self._cond:
                        self._outstanding -= 1
                        self._cond.notify_all()
        finally:
            with self._cond:
                self._threads.discard(threading.current_thread())
                # A job may have been queued between our last check and exit: hand it on.
                if (self._queue and not self._closed and self._idle == 0
                        and len(self._threads) < self._max_workers):
                    self._spawn_locked()


class Strand:
    """Serial executor on top of a :class:`WorkerPool`: jobs run one at a time, in order."""

    def __init__(self, pool: WorkerPool, name: str = ""):
        self._pool = pool
        self.name = name
        self._lock = threading.Lock()
        self._jobs: collections.deque[tuple[Job, Future]] = collections.deque()
        self._active = False

    def submit(self, fn: Job) -> Future:
        future: Future = Future()
        with self._lock:
            self._jobs.append((fn, future))
            start = not self._active
            self._active = True
        if start:
            self._pool.submit(self._drain)
        return future

    def _drain(self) -> None:
        while True:
            with self._lock:
                if not self._jobs:
                    self._active = False
                    return
                fn, future = self._jobs.popleft()
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(fn())
            except BaseException as exc:  # noqa: BLE001 - delivered through the Future
                future.set_exception(exc)
