"""Small networking helpers shared by the mock servers: tracked threads/sockets, clean shutdown."""
from __future__ import annotations

import socket
import struct
import threading
from http.server import HTTPServer
from typing import Callable

SOCKET_TIMEOUT = 0.5


def rst_close(sock: socket.socket | None) -> None:
    """Close with SO_LINGER(1, 0) so the peer receives a TCP RST."""
    if sock is None:
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def quiet_close(sock: socket.socket | None) -> None:
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


class ConnRegistry:
    """Tracks live connection sockets and their threads for deterministic shutdown."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conns: dict[int, tuple[socket.socket, threading.Thread | None]] = {}
        self._threads: list[threading.Thread] = []

    def spawn(self, name: str, target: Callable, *args) -> threading.Thread:
        t = threading.Thread(target=target, args=args, name=name, daemon=True)
        with self._lock:
            self._threads = [x for x in self._threads if x.is_alive()]
            self._threads.append(t)
        t.start()
        return t

    def add(self, sock: socket.socket) -> None:
        with self._lock:
            self._conns[id(sock)] = (sock, threading.current_thread())

    def remove(self, sock: socket.socket) -> None:
        with self._lock:
            self._conns.pop(id(sock), None)

    def sockets(self) -> list[socket.socket]:
        with self._lock:
            return [s for s, _ in self._conns.values()]

    def close_all(self, rst: bool = False) -> None:
        for s in self.sockets():
            (rst_close if rst else quiet_close)(s)

    def join(self, timeout: float = 3.0) -> None:
        import time

        deadline = time.monotonic() + timeout
        with self._lock:
            threads = list(self._threads)
        for t in threads:
            if t is threading.current_thread():
                continue
            t.join(max(0.0, deadline - time.monotonic()))


class TrackedHTTPServer(HTTPServer):
    """HTTPServer with one tracked daemon thread per connection and a clean ``stop()``."""

    allow_reuse_address = False
    request_queue_size = 64

    def __init__(self, addr, handler, name: str, wrap: Callable[[socket.socket], socket.socket] | None = None):
        self.registry = ConnRegistry()
        self.thread_name = name
        self._wrap = wrap
        self._serve_thread: threading.Thread | None = None
        self.stopping = threading.Event()
        super().__init__(addr, handler)

    def get_request(self):
        sock, addr = self.socket.accept()
        if self._wrap is not None:
            sock = self._wrap(sock)
        return sock, addr

    def process_request(self, request, client_address):
        self.registry.spawn(f"{self.thread_name}-conn", self._process, request, client_address)

    def _process(self, request, client_address):
        self.registry.add(request)
        try:
            self.finish_request(request, client_address)
        except Exception:  # noqa: BLE001 - connection-level errors are expected in chaos tests
            pass
        finally:
            self.registry.remove(request)
            self.shutdown_request(request)

    def handle_error(self, request, client_address):  # silence tracebacks
        pass

    def start(self) -> None:
        self._serve_thread = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.05},
                                              name=f"{self.thread_name}-accept", daemon=True)
        self._serve_thread.start()

    def stop(self) -> None:
        self.stopping.set()
        if self._serve_thread is not None:
            self.shutdown()
            self._serve_thread.join(3)
        self.server_close()
        self.registry.close_all()
        self.registry.join(3)


class TcpServer:
    """Plain threaded TCP acceptor: ``handler(sock, addr)`` runs in its own tracked thread."""

    def __init__(self, host: str, port: int, handler: Callable[[socket.socket, tuple], None], name: str):
        self.name = name
        self.handler = handler
        self.registry = ConnRegistry()
        self.stopping = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind((host, port))
        self.sock.listen(64)
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]
        self._accept_thread: threading.Thread | None = None

    def start(self) -> None:
        self._accept_thread = threading.Thread(target=self._accept_loop, name=f"{self.name}-accept", daemon=True)
        self._accept_thread.start()

    def _accept_loop(self) -> None:
        while not self.stopping.is_set():
            try:
                conn, addr = self.sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            conn.settimeout(SOCKET_TIMEOUT)
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.registry.spawn(f"{self.name}-conn", self._run, conn, addr)

    def _run(self, conn: socket.socket, addr) -> None:
        self.registry.add(conn)
        try:
            self.handler(conn, addr)
        except Exception:  # noqa: BLE001
            pass
        finally:
            self.registry.remove(conn)
            quiet_close(conn)

    def stop(self) -> None:
        self.stopping.set()
        try:
            self.sock.close()
        except OSError:
            pass
        if self._accept_thread is not None:
            self._accept_thread.join(3)
        self.registry.close_all()
        self.registry.join(3)
