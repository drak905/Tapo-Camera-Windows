"""Tier 1: TOFU PinStore and HttpsTransport error mapping (no camera needed)."""
from __future__ import annotations

import socket
import time

import pytest

from tapo_desktop.core.control_client.transport import HttpsTransport, PinStore
from tapo_desktop.core.errors import TapoConnectionError

pytestmark = pytest.mark.unit
FP1 = "a" * 64
FP2 = "0123456789abcdef" * 4


def test_pinstore_memory_get_set_forget():
    s = PinStore()
    assert s.get("10.0.0.1", 443) is None
    s.set("10.0.0.1", 443, FP1)
    assert s.get("10.0.0.1", 443) == FP1
    s.set("10.0.0.1", 443, FP2)
    assert s.get("10.0.0.1", 443) == FP2
    s.forget("10.0.0.1", 443)
    assert s.get("10.0.0.1", 443) is None
    s.forget("10.0.0.1", 443)  # idempotent


def test_pinstore_keyed_by_host_and_port():
    s = PinStore()
    s.set("10.0.0.1", 443, FP1)
    assert s.get("10.0.0.1", 444) is None
    assert s.get("10.0.0.2", 443) is None


def test_pinstore_persists(tmp_path):
    path = tmp_path / "pins.json"
    s = PinStore(path)
    s.set("10.0.0.1", 443, FP1)
    s.set("cam.local", 8443, FP2)
    s2 = PinStore(path)
    assert s2.get("10.0.0.1", 443) == FP1
    assert s2.get("cam.local", 8443) == FP2
    s2.forget("10.0.0.1", 443)
    assert PinStore(path).get("10.0.0.1", 443) is None
    assert PinStore(path).get("cam.local", 8443) == FP2


def test_pinstore_missing_or_corrupt_file(tmp_path):
    assert PinStore(tmp_path / "nope.json").get("h", 1) is None
    bad = tmp_path / "bad.json"
    bad.write_text("{{{not json", encoding="utf-8")
    s = PinStore(bad)
    assert s.get("h", 1) is None
    s.set("h", 1, FP1)
    assert PinStore(bad).get("h", 1) == FP1


def test_pinstore_normalizes_fingerprint_to_lowercase():
    s = PinStore()
    s.set("h", 1, FP2.upper())
    assert s.get("h", 1) == FP2


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_transport_connection_refused_maps_to_tapo_connection_error():
    t = HttpsTransport("127.0.0.1", _free_port(), pin_store=PinStore(), timeout=0.5)
    t0 = time.monotonic()
    with pytest.raises(TapoConnectionError):
        t.post("/", "{}", {"Content-Type": "application/json"})
    assert time.monotonic() - t0 < 5.0
    t.close()
    t.close()  # idempotent


def test_transport_plain_tcp_server_maps_to_tapo_connection_error():
    """A server that is not TLS (closes after garbage) -> TapoConnectionError, not ssl.SSLError."""
    import threading

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def serve():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                c, _ = srv.accept()
            except OSError:
                continue
            try:
                c.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            finally:
                c.close()

    th = threading.Thread(target=serve, daemon=True)
    th.start()
    try:
        t = HttpsTransport("127.0.0.1", port, pin_store=PinStore(), timeout=2.0)
        with pytest.raises(TapoConnectionError):
            t.post("/", "{}", {})
        assert t.fingerprint is None
    finally:
        stop.set()
        srv.close()
        th.join(2)


def test_transport_timeout_maps_to_tapo_connection_error():
    """Server accepts TCP but never answers the TLS handshake -> timeout -> TapoConnectionError."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port = srv.getsockname()[1]
    try:
        t = HttpsTransport("127.0.0.1", port, pin_store=PinStore(), timeout=0.5)
        t0 = time.monotonic()
        with pytest.raises(TapoConnectionError):
            t.post("/", "{}", {})
        assert time.monotonic() - t0 < 0.5 * 2 + 1.0
    finally:
        srv.close()
