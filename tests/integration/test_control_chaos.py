"""Tier 2 chaos: TapoControlClient through ChaosProxy (resets, blackholes, refusals)."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from tapo_desktop.core.errors import TapoConnectionError
from tests.integration._core_it_helpers import make_client

pytestmark = [pytest.mark.integration, pytest.mark.chaos, pytest.mark.timeout(45)]


@pytest.fixture
def proxied(mock_camera, chaos_proxy):
    proxy = chaos_proxy(mock_camera.host, mock_camera.https_port)
    return mock_camera, proxy


def test_keep_alive_reuses_connection(proxied):
    cam, proxy = proxied
    client = make_client(cam, host="127.0.0.1", port=proxy.port)
    try:
        client.login()
        base = proxy.connection_count
        for _ in range(10):
            client.get_led()
        assert proxy.connection_count - base <= 1, "transport must reuse the keep-alive connection"
    finally:
        client.close()


def test_reset_all_mid_session_recovers(proxied):
    cam, proxy = proxied
    client = make_client(cam, host="127.0.0.1", port=proxy.port)
    try:
        client.get_device_info()
        proxy.reset_all()
        assert client.get_device_info().model          # reconnect once on stale socket
        proxy.reset_all()
        client.set_led(False)
        assert cam.state.led is False
    finally:
        client.close()


def test_reset_all_repeatedly(proxied):
    cam, proxy = proxied
    client = make_client(cam, host="127.0.0.1", port=proxy.port)
    try:
        for _ in range(5):
            client.get_led()
            proxy.reset_all()
        assert client.get_led() in (True, False)
    finally:
        client.close()


def test_blackhole_times_out_then_recovers(proxied):
    cam, proxy = proxied
    timeout = 0.5
    client = make_client(cam, host="127.0.0.1", port=proxy.port, timeout=timeout)
    try:
        client.get_led()
        proxy.blackhole(2.0)
        t_bh = time.monotonic()
        t0 = time.monotonic()
        with pytest.raises(TapoConnectionError):
            client.get_led()
        elapsed = time.monotonic() - t0
        # at most one reconnect attempt => <= 2 x timeout, plus scheduling margin
        assert elapsed < 2 * timeout + 0.8, f"took {elapsed:.2f}s"
        remaining = 2.0 - (time.monotonic() - t_bh)
        if remaining > 0:
            time.sleep(remaining + 0.5)
        assert client.get_led() in (True, False)
        assert client.get_device_info().model
    finally:
        client.close()


def test_refused_connection_then_recovers(proxied):
    cam, proxy = proxied
    client = make_client(cam, host="127.0.0.1", port=proxy.port, timeout=1.0)
    try:
        client.get_led()
        proxy.set_refuse(True)
        proxy.reset_all()
        t0 = time.monotonic()
        with pytest.raises(TapoConnectionError):
            client.get_led()
        assert time.monotonic() - t0 < 4.0
        proxy.set_refuse(False)
        assert client.get_led() in (True, False)
    finally:
        client.close()


def test_latency_does_not_break_session(proxied):
    cam, proxy = proxied
    proxy.set_latency(50)
    client = make_client(cam, host="127.0.0.1", port=proxy.port, timeout=5.0)
    try:
        for _ in range(5):
            client.get_led()
        assert cam.login_count == 1
    finally:
        proxy.set_latency(0)
        client.close()


def test_concurrent_requests_with_reset(proxied):
    cam, proxy = proxied
    client = make_client(cam, host="127.0.0.1", port=proxy.port, timeout=5.0)
    errors: list[BaseException] = []

    def work(_):
        for _ in range(25):
            try:
                client.get_led()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

    try:
        client.login()
        with ThreadPoolExecutor(8) as ex:
            futs = [ex.submit(work, i) for i in range(8)]
            time.sleep(0.2)
            proxy.reset_all()
            for f in futs:
                f.result()
        assert errors == [], errors[:3]
    finally:
        client.close()
