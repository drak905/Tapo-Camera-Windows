"""ChaosProxy: TCP-level fault injection between a client and any mock surface.

* ``reset_all()``       RST every current connection (both legs, SO_LINGER 0)
* ``blackhole(s)``      for ``s`` seconds accept nothing and forward nothing (data is held, then
                        delivered once the period ends); new connects wait in the listen backlog
* ``set_refuse(True)``  close the listening socket so new connects get ECONNREFUSED; ``False``
                        re-binds the same port
* ``set_latency(ms)``   delay every forwarded chunk
* ``connection_count``  cumulative accepted connections; ``active_connections`` currently open
* ``bytes_forwarded``   total bytes forwarded in both directions
"""
from __future__ import annotations

import socket
import threading
import time

from ._net import ConnRegistry, quiet_close, rst_close


class _Pair:
    def __init__(self, client: socket.socket, upstream: socket.socket):
        self.client, self.upstream = client, upstream
        self.done = 0
        self.closed = False


class ChaosProxy:
    def __init__(self, target_host: str, target_port: int, listen_port: int = 0, listen_host: str = "127.0.0.1"):
        self.target = (target_host, target_port)
        self.listen_host = listen_host
        self._lock = threading.Lock()
        self._pairs: list[_Pair] = []
        self._registry = ConnRegistry()
        self._stopping = threading.Event()
        self._blackhole_until = 0.0
        self._refuse = False
        self._latency = 0.0
        self.connection_count = 0
        self.bytes_forwarded = 0
        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self.port = self._bind(listen_port)

    # -- lifecycle ------------------------------------------------------------------------
    def _bind(self, port: int) -> int:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if port:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.listen_host, port))
        s.listen(64)
        s.settimeout(0.05)
        self._listener = s
        return s.getsockname()[1]

    def start(self) -> "ChaosProxy":
        self._stopping.clear()
        self._accept_thread = threading.Thread(target=self._accept_loop, name="mock-chaos-accept", daemon=True)
        self._accept_thread.start()
        return self

    def stop(self) -> None:
        self._stopping.set()
        with self._lock:
            lst, self._listener = self._listener, None
        quiet_close(lst)
        if self._accept_thread is not None:
            self._accept_thread.join(3)
        self._close_pairs(rst=False)
        self._registry.join(3)

    def __enter__(self) -> "ChaosProxy":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- faults ---------------------------------------------------------------------------
    def reset_all(self) -> None:
        self._close_pairs(rst=True)

    def blackhole(self, seconds: float) -> None:
        with self._lock:
            self._blackhole_until = time.monotonic() + seconds

    def set_refuse(self, enabled: bool) -> None:
        with self._lock:
            if enabled == self._refuse:
                return
            self._refuse = enabled
            lst = None
            if enabled:
                lst, self._listener = self._listener, None
        if enabled:
            quiet_close(lst)
        else:
            self._bind(self.port)

    def set_latency(self, ms: int) -> None:
        self._latency = max(0, ms) / 1000.0

    @property
    def active_connections(self) -> int:
        with self._lock:
            return len(self._pairs)

    def _blackholed(self) -> bool:
        return time.monotonic() < self._blackhole_until

    # -- internals ------------------------------------------------------------------------
    def _close_pair(self, pair: _Pair, rst: bool) -> None:
        with self._lock:
            if pair.closed:
                return
            pair.closed = True
            if pair in self._pairs:
                self._pairs.remove(pair)
        closer = rst_close if rst else quiet_close
        closer(pair.client)
        closer(pair.upstream)

    def _close_pairs(self, rst: bool) -> None:
        with self._lock:
            pairs = list(self._pairs)
        for p in pairs:
            self._close_pair(p, rst)

    def _accept_loop(self) -> None:
        while not self._stopping.is_set():
            lst = self._listener
            if lst is None or self._blackholed():
                time.sleep(0.02)
                continue
            try:
                client, _ = lst.accept()
            except TimeoutError:
                continue
            except OSError:
                time.sleep(0.02)
                continue
            try:
                upstream = socket.create_connection(self.target, timeout=3)
            except OSError:
                rst_close(client)
                continue
            for s in (client, upstream):
                s.settimeout(0.1)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            pair = _Pair(client, upstream)
            with self._lock:
                self.connection_count += 1
                self._pairs.append(pair)
            self._registry.spawn("mock-chaos-pump", self._pump, pair, client, upstream)
            self._registry.spawn("mock-chaos-pump", self._pump, pair, upstream, client)

    def _pump(self, pair: _Pair, src: socket.socket, dst: socket.socket) -> None:
        clean = False
        try:
            while not self._stopping.is_set() and not pair.closed:
                if self._blackholed():
                    time.sleep(0.02)
                    continue
                try:
                    data = src.recv(65536)
                except TimeoutError:
                    continue
                if not data:
                    clean = True
                    break
                while self._blackholed() and not self._stopping.is_set():
                    time.sleep(0.02)
                if self._latency:
                    time.sleep(self._latency)
                dst.sendall(data)
                with self._lock:
                    self.bytes_forwarded += len(data)
        except OSError:
            clean = False
        if self._stopping.is_set() or pair.closed:
            self._close_pair(pair, rst=False)
            return
        if not clean:  # propagate the reset to the other leg
            self._close_pair(pair, rst=True)
            return
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        with self._lock:
            pair.done += 1
            both = pair.done >= 2
        if both:
            self._close_pair(pair, rst=False)
