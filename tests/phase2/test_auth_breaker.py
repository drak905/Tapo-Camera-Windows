"""A wrong password must not be retried automatically (camera lockout protection)."""
from __future__ import annotations

import time

import pytest

from tapo_desktop.core.control_client.client import TapoControlClient
from tapo_desktop.core.errors import TapoAuthError

pytestmark = pytest.mark.integration


def test_wrong_password_fails_fast_without_new_login_attempts(mock_camera):
    c = TapoControlClient(mock_camera.host, "wrong-password", port=mock_camera.https_port, timeout=5,
                          auth_retry_after=30)
    try:
        with pytest.raises(TapoAuthError):
            c.get_device_info()
        hits = len(mock_camera.request_log), mock_camera.login_count
        t0 = time.monotonic()
        for _ in range(5):
            with pytest.raises(TapoAuthError):
                c.get_device_info()
        assert time.monotonic() - t0 < 0.5  # no network round trips
        assert (len(mock_camera.request_log), mock_camera.login_count) == hits
    finally:
        c.close()


def test_breaker_expires_and_reset_allows_retry(mock_camera):
    c = TapoControlClient(mock_camera.host, "cloudpass", port=mock_camera.https_port, timeout=5,
                          auth_retry_after=0.3)
    try:
        c._password = "wrong"  # simulate a stale password, then the user fixes it
        with pytest.raises(TapoAuthError):
            c.login()
        with pytest.raises(TapoAuthError):
            c.login()  # still blocked
        c._password = "cloudpass"
        c.reset_auth_backoff()
        c.login()
        assert c.get_device_info().model
    finally:
        c.close()
