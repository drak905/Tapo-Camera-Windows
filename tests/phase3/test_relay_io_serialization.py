"""One TLS object must never be read and written at the same time (OpenSSL is not safe for it and
CPython's ssl module adds no lock): the relay tunnel serialises recv() and sendall()."""
from __future__ import annotations

import socket
import threading
import time

import pytest

from tapo_desktop.core.cloud import relay

pytestmark = pytest.mark.unit


class _OverlapDetector:
    """Fake TLS socket: recv() blocks a little per call; flags any overlap with sendall()."""

    def __init__(self):
        self._busy = threading.Lock()
        self.overlaps = 0
        self.sent = 0
        self.timeout = None
        self.closed = False

    def _enter(self):
        if not self._busy.acquire(blocking=False):
            self.overlaps += 1
            self._busy.acquire()

    def settimeout(self, t):
        self.timeout = t

    def gettimeout(self):
        return self.timeout

    def recv(self, n):
        self._enter()
        try:
            time.sleep(0.01)
            if self.timeout is not None and self.timeout < 0.05:
                raise socket.timeout("slice")
            return b"x"
        finally:
            self._busy.release()

    def sendall(self, data):
        self._enter()
        try:
            time.sleep(0.002)
            self.sent += 1
        finally:
            self._busy.release()

    def close(self):
        self.closed = True


def test_reads_and_writes_never_overlap():
    raw = _OverlapDetector()
    sock = relay.SerializedTLSSocket(raw)
    sock.settimeout(5)
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            sock.recv(1024)

    t = threading.Thread(target=reader)
    t.start()
    try:
        for _ in range(50):
            sock.sendall(b"heartbeat")
    finally:
        stop.set()
        t.join(2)
    assert raw.sent == 50
    assert raw.overlaps == 0


def test_recv_timeout_is_still_honoured():
    class Silent(_OverlapDetector):
        def recv(self, n):
            time.sleep(0.01)
            raise socket.timeout("slice")

    sock = relay.SerializedTLSSocket(Silent())
    sock.settimeout(0.3)
    t0 = time.monotonic()
    with pytest.raises(socket.timeout):
        sock.recv(10)
    assert 0.25 <= time.monotonic() - t0 < 1.5


def test_writer_is_not_starved_by_a_waiting_reader():
    class Slow(_OverlapDetector):
        def recv(self, n):
            time.sleep(0.05)
            raise socket.timeout("slice")

    sock = relay.SerializedTLSSocket(Slow())
    sock.settimeout(10)
    t = threading.Thread(target=lambda: pytest.raises(Exception, sock.recv, 10), daemon=True)
    t.start()
    time.sleep(0.1)
    t0 = time.monotonic()
    sock.sendall(b"hb")
    assert time.monotonic() - t0 < 0.5
    sock.close()
