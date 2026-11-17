"""TPAP / v3 login with a local access token (LAT) against the mock camera over real HTTPS.

[spec:lat.tpap.passcode_candidates]: with a LAT the client offers SHA256_HEX_UPPER(LAT) FIRST, then
the best password variant; still at most ``tpap.MAX_ATTEMPTS`` (2) pake_share per login and the auth
circuit breaker is unchanged. [spec:lat.legacy_secure.login]: the v3 secure login accepts the raw LAT
as the password when the device_confirm proves the camera uses it.
"""
from __future__ import annotations

import logging

import pytest

from tapo_desktop.core.control_client import tpap
from tapo_desktop.core.control_client.client import TapoControlClient
from tapo_desktop.core.control_client.transport import PinStore
from tapo_desktop.core.errors import TapoAuthError
from tests.fixtures.mock_tapo_server import MockTapoCamera

pytestmark = pytest.mark.integration

PASSWORD = "cloudpass"
LAT = "8c1f2d0b-LAT-6a1e0f9c3b2d4e5f"


@pytest.fixture(autouse=True)
def _fast_relogin(monkeypatch):
    monkeypatch.setattr(tpap, "RELOGIN_RETRY_DELAY_S", 0.05)


@pytest.fixture
def cams():
    started: list[MockTapoCamera] = []

    def make(**kw) -> MockTapoCamera:
        kw.setdefault("protocol", "tpap")
        cam = MockTapoCamera(cloud_password=PASSWORD, model="C200", **kw).start()
        started.append(cam)
        return cam

    yield make
    for c in started:
        c.stop()


def _client(cam, password=PASSWORD, **kw) -> TapoControlClient:
    kw.setdefault("encrypt_type", ["4"] if cam.protocol == "tpap" else None)
    return TapoControlClient(cam.host, password, port=cam.https_port, pin_store=PinStore(), timeout=5.0, **kw)


def test_lat_camera_accepts_lat_with_exactly_one_share(cams):
    cam = cams(tpap_credential="lat", local_token=LAT)
    c = _client(cam, local_token=LAT)
    try:
        assert c.get_device_info().model == "C200"
        assert c.protocol == "tpap"
        assert cam.tpap_share_attempts == 1
        assert c.uses_local_token is True
    finally:
        c.close()


def test_lat_is_tried_first_even_when_the_password_would_work(cams):
    cam = cams(tpap_credential="md5")                  # camera still on the TP-Link password
    c = _client(cam, local_token=LAT)
    try:
        c.login()
        assert cam.tpap_share_attempts == 2            # LAT refused, then md5(password) accepted
        assert cam.login_count == 1
    finally:
        c.close()


def test_wrong_lat_falls_back_to_the_best_password_variant(cams):
    cam = cams(tpap_credential="sha256_extra")         # C200 style: extra_crypt -> sha256 is "best"
    c = _client(cam, local_token="not-the-camera-token")
    try:
        c.login()
        assert cam.tpap_share_attempts == 2
        assert c.protocol == "tpap"
    finally:
        c.close()


def test_wrong_lat_and_wrong_password_stop_after_two_shares_then_breaker(cams):
    cam = cams(tpap_credential="lat", local_token=LAT)
    c = _client(cam, local_token="stale-token")
    try:
        with pytest.raises(TapoAuthError) as err:
            c.login()
        assert cam.tpap_share_attempts == 2            # LAT + one password variant, never more
        assert "local access token" in str(err.value)
        assert "stale-token" not in str(err.value) and PASSWORD not in str(err.value)
        with pytest.raises(TapoAuthError):
            c.get_device_info()                        # breaker: fails fast, no new attempts
        assert cam.tpap_share_attempts == 2
        assert cam.tpap_register_count == 2
    finally:
        c.close()


def test_no_lat_behaviour_is_unchanged(cams):
    cam = cams(tpap_credential="md5")
    c = _client(cam)
    try:
        c.login()
        assert cam.tpap_share_attempts == 1
        assert c.uses_local_token is False
    finally:
        c.close()


def test_lat_camera_without_lat_reports_the_password_failure(cams):
    cam = cams(tpap_credential="lat", local_token=LAT)
    c = _client(cam)
    try:
        with pytest.raises(TapoAuthError) as err:
            c.login()
        assert "rejected the TP-Link ID password" in str(err.value)
        assert cam.tpap_share_attempts == 2            # md5 + sha256 as before
    finally:
        c.close()


def test_relogin_after_session_expiry_reuses_the_lat_in_one_share(cams):
    cam = cams(tpap_credential="lat", local_token=LAT)
    c = _client(cam, local_token=LAT)
    try:
        c.get_device_info()
        cam.expire_sessions()
        c.get_device_info()
        assert cam.tpap_share_attempts == 2            # one share per login
        assert cam.login_count == 2
    finally:
        c.close()


def test_lat_only_without_password_still_logs_in(cams):
    cam = cams(tpap_credential="lat", local_token=LAT)
    c = _client(cam, password="", local_token=LAT)
    try:
        c.login()
        assert cam.tpap_share_attempts == 1
    finally:
        c.close()


def test_v3_secure_camera_in_lat_mode(cams):
    cam = cams(protocol="secure", tpap_credential="lat", local_token=LAT)
    c = _client(cam, local_token=LAT)
    try:
        info = c.get_device_info()
        assert info.model == "C200" and c.protocol == "secure"
        c.get_privacy()                                # tapo_tag must be computed with the LAT
    finally:
        c.close()
    plain = _client(cam)
    try:
        with pytest.raises(TapoAuthError):
            plain.login()
    finally:
        plain.close()


def test_v3_secure_camera_on_password_with_a_lat_given(cams):
    cam = cams(protocol="secure")
    c = _client(cam, local_token=LAT)
    try:
        assert c.get_device_info().model == "C200"
    finally:
        c.close()


def test_lat_never_in_repr_or_logs(cams, caplog):
    caplog.set_level(logging.DEBUG)
    cam = cams(tpap_credential="lat", local_token=LAT)
    c = _client(cam, local_token=LAT)
    try:
        c.get_device_info()
        auth = c._tpap_auth
        text = caplog.text + repr(c) + repr(auth)
        assert LAT not in text and tpap.sha256_hex_upper(LAT) not in text
    finally:
        c.close()


def test_authenticator_candidate_order_unit():
    """The order of passcodes offered by TpapAuthenticator (no network: a scripted camera)."""
    seen: list[str] = []

    class Refuser:
        def __call__(self, params):
            if params["sub_method"] == "pake_register":
                import base64
                import os
                share = tpap.encode_point(tpap._mul(5, tpap._G))
                return {"error_code": 0, "result": {"dev_salt": base64.b64encode(os.urandom(16)).decode(),
                                                    "dev_random": base64.b64encode(os.urandom(32)).decode(),
                                                    "dev_share": base64.b64encode(share).decode(),
                                                    "iterations": 10}}
            return {"error_code": -40401}

    orig = tpap.derive_w0_w1

    def spy(cred, salt, it):
        seen.append(cred)
        return orig(cred, salt, it)

    auth = tpap.TpapAuthenticator(Refuser(), PASSWORD, local_token=LAT, sleep=lambda s: None)
    import unittest.mock as um
    with um.patch.object(tpap, "derive_w0_w1", spy):
        with pytest.raises(TapoAuthError):
            auth.login()
    assert seen == [tpap.sha256_hex_upper(LAT), tpap.md5_hex(PASSWORD)]
