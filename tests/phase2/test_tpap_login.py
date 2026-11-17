"""TapoControlClient against a TPAP (encrypt_type "4") mock camera, end to end over real HTTPS.

[spec:tpap.selection], [spec:tpap.pake_register.request], [spec:tpap.pake_share.request],
[spec:tpap.credentials.userpw], [spec:tpap.ds.framing], [spec:tpap.ds.errors], [spec:tpap.relogin]
"""
from __future__ import annotations

import datetime as dt
import logging

import pytest

from tapo_desktop.core.control_client import tpap
from tapo_desktop.core.control_client.client import TapoControlClient
from tapo_desktop.core.control_client.transport import HttpsTransport, PinStore
from tapo_desktop.core.errors import TapoAuthError, TapoDeviceError, TapoLockedError
from tapo_desktop.models import DayNightMode
from tests.fixtures.mock_tapo_server import MockTapoCamera

pytestmark = pytest.mark.integration

PASSWORD = "cloudpass"
C200_TPAP = {"pake": [2], "tls": 1, "noc": 1, "port": 443}


@pytest.fixture(autouse=True)
def _fast_relogin(monkeypatch):
    monkeypatch.setattr(tpap, "RELOGIN_RETRY_DELAY_S", 0.05)


def _cam(**kw) -> MockTapoCamera:
    knobs = {k: kw.pop(k) for k in list(kw) if k.startswith("tpap_") and k != "tpap_credential"}
    cam = MockTapoCamera(protocol="tpap", model=kw.pop("model", "C200"), **kw)
    for k, v in knobs.items():
        setattr(cam, k, v)
    return cam.start()


@pytest.fixture
def tpap_camera():
    cam = _cam()
    try:
        yield cam
    finally:
        cam.stop()


@pytest.fixture
def c200_camera():
    cam = _cam(tpap_credential="sha256_extra")
    try:
        yield cam
    finally:
        cam.stop()


class RecordingTransport(HttpsTransport):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.json_bodies: list[str] = []
        self.binary: list[tuple[str, dict]] = []

    def post(self, path, body, headers):
        self.json_bodies.append(body)
        return super().post(path, body, headers)

    def post_bytes(self, path, body, headers):
        self.binary.append((path, {k.lower(): v for k, v in headers.items()}))
        return super().post_bytes(path, body, headers)


def _client(cam, password=PASSWORD, **kw) -> TapoControlClient:
    return TapoControlClient(cam.host, password, port=cam.https_port, pin_store=PinStore(), timeout=5.0, **kw)


# ---------------------------------------------------------------- login happy paths
def test_md5_camera_login_after_minus_40211(tpap_camera):
    c = _client(tpap_camera)
    try:
        info = c.get_device_info()
        assert info.model == "C200"
        assert c.protocol == "tpap" and c.is_secure and c.logged_in
        assert tpap_camera.v3_probe_count == 1          # v3 step 1 answered -40211 -> TPAP
        assert tpap_camera.tpap_share_attempts == 1     # md5 passcode accepted first time
        assert tpap_camera.login_count == 1
    finally:
        c.close()


def test_c200_sha256_extra_crypt_login_uses_one_attempt(c200_camera):
    c = _client(c200_camera)
    try:
        assert c.get_privacy() is False
        assert c.protocol == "tpap"
        assert c200_camera.tpap_share_attempts == 1     # extra_crypt seen -> sha256 tried first
    finally:
        c.close()


def test_tpap_hint_skips_the_v3_probe(tpap_camera):
    c = _client(tpap_camera, tpap=C200_TPAP)
    try:
        c.login()
        assert tpap_camera.v3_probe_count == 0
        assert c.protocol == "tpap"
        c2 = _client(tpap_camera, encrypt_type=["4"])
        c2.login()
        assert tpap_camera.v3_probe_count == 0
        c2.close()
    finally:
        c.close()


def test_register_request_matches_the_app(tpap_camera):
    t = RecordingTransport(tpap_camera.host, tpap_camera.https_port, pin_store=PinStore(), timeout=5.0)
    c = TapoControlClient(tpap_camera.host, PASSWORD, port=tpap_camera.https_port, transport=t, tpap=C200_TPAP)
    try:
        c.get_device_info()
    finally:
        c.close()
    import json
    reg = json.loads(t.json_bodies[0])
    share = json.loads(t.json_bodies[1])
    assert reg["method"] == "login" and share["method"] == "login"
    p = reg["params"]
    assert p["sub_method"] == "pake_register"
    assert p["username"] == tpap.md5_hex("admin")
    assert p["cipher_suites"] == [1] and p["encryption"] == ["aes_128_ccm"] and p["passcode_type"] == "userpw"
    assert len(__import__("base64").b64decode(p["user_random"])) == 32
    assert set(share["params"]) == {"sub_method", "user_share", "user_confirm"}
    assert share["params"]["sub_method"] == "pake_share"
    path, headers = t.binary[0]
    assert path.startswith("/stok=") and path.endswith("/ds")
    assert headers["content-type"] == "application/octet-stream"
    assert headers["accept"] == "application/octet-stream"


def test_order_follows_register_evidence_not_model(tpap_camera):
    # the camera is a "C200" but sends no extra_crypt: md5 (the app's first candidate) is tried first
    c = _client(tpap_camera, tpap=C200_TPAP)
    try:
        c.login()
        assert tpap_camera.state.model == "C200"
        assert tpap_camera.tpap_share_attempts == 1
    finally:
        c.close()


def test_literal_admin_username_fallback(tpap_camera):
    tpap_camera.tpap_username = "admin"                 # firmware wanting the literal user name
    c = _client(tpap_camera, tpap=C200_TPAP)
    try:
        c.login()
        assert tpap_camera.tpap_register_count == 2     # md5("admin") -> -40209, then "admin"
        assert tpap_camera.tpap_share_attempts == 1     # no password attempt was spent on it
    finally:
        c.close()


# ---------------------------------------------------------------- failures are bounded
def test_wrong_password_raises_auth_error_after_exactly_two_attempts(tpap_camera):
    c = _client(tpap_camera, password="wrong")
    try:
        with pytest.raises(TapoAuthError) as ei:
            c.login()
        assert not isinstance(ei.value, TapoLockedError)
        assert tpap_camera.tpap_share_attempts == 2
        assert not c.logged_in
        assert "wrong" not in str(ei.value)
        # a later request makes another bounded login, never a loop
        with pytest.raises(TapoAuthError):
            c.get_device_info()
        assert tpap_camera.tpap_share_attempts <= 4
    finally:
        c.close()


def test_locked_camera_stops_immediately(tpap_camera):
    tpap_camera.lock_login(60)
    c = _client(tpap_camera, tpap=C200_TPAP)
    try:
        with pytest.raises(TapoLockedError) as ei:
            c.login()
        assert "retry in" in str(ei.value)
        assert tpap_camera.tpap_share_attempts == 0
        assert tpap_camera.tpap_register_count == 1
    finally:
        c.close()


def test_lockout_after_first_failure_stops_the_second_attempt(tpap_camera):
    tpap_camera.max_failed_logins = 1
    c = _client(tpap_camera, password="wrong", tpap=C200_TPAP)
    try:
        with pytest.raises(TapoLockedError):
            c.login()
        assert tpap_camera.tpap_share_attempts == 1
    finally:
        c.close()


def test_dev_confirm_mismatch_is_auth_error_without_retry(tpap_camera):
    tpap_camera.tpap_bad_dev_confirm = True
    c = _client(tpap_camera, tpap=C200_TPAP)
    try:
        with pytest.raises(TapoAuthError):
            c.login()
        assert tpap_camera.tpap_share_attempts == 1
        assert not c.logged_in
    finally:
        c.close()


def test_empty_password_never_reaches_the_camera(tpap_camera):
    c = _client(tpap_camera, password="", tpap=C200_TPAP)
    try:
        with pytest.raises(TapoAuthError):
            c.login()
        assert tpap_camera.tpap_register_count == 0
    finally:
        c.close()


# ---------------------------------------------------------------- channel
def test_seq_starts_at_start_seq_and_increments(tpap_camera):
    tpap_camera.fixed_start_seq = 500
    c = _client(tpap_camera, tpap=C200_TPAP)
    try:
        c.get_device_info()
        c.get_privacy()
        c.get_led()
        assert tpap_camera.tpap_ds_seqs == [500, 501, 502]
        raw = list(tpap_camera.raw_request_log)
        assert raw and all(r.get("method") == "multipleRequest" for r in raw)
    finally:
        c.close()


def test_session_expiry_relogs_in_with_the_known_good_variant(c200_camera):
    c = _client(c200_camera)
    try:
        c.get_device_info()
        assert c200_camera.tpap_share_attempts == 1
        c200_camera.expire_sessions()
        assert c.get_privacy() is False                 # -40401 -> silent re-login + retry
        assert c200_camera.login_count == 2
        assert c200_camera.tpap_share_attempts == 2     # one attempt, the variant that worked
        assert c200_camera.v3_probe_count == 1          # TPAP remembered, no second v3 probe
    finally:
        c.close()


def test_token_expiry_every_n_requests(tpap_camera):
    tpap_camera.inject_token_expiry(3)
    c = _client(tpap_camera, tpap=C200_TPAP)
    try:
        for _ in range(7):
            c.get_led()
        assert tpap_camera.login_count >= 3
    finally:
        c.close()


def test_c200_refuses_first_share_after_session_end(c200_camera):
    c = _client(c200_camera)
    try:
        c.get_device_info()
        c200_camera.expire_sessions()
        c200_camera.tpap_refuse_shares = 1
        assert c.get_privacy() is False
        assert c200_camera.tpap_share_attempts == 3     # 1 initial + refused + accepted (same passcode)
    finally:
        c.close()


def test_reboot_ends_session_and_next_call_relogs(tpap_camera):
    c = _client(tpap_camera, tpap=C200_TPAP)
    try:
        c.reboot()
        assert not c.logged_in
        c.get_device_info()
        assert tpap_camera.login_count == 2
    finally:
        c.close()


def test_item_errors_still_surface_as_device_errors(tpap_camera):
    tpap_camera.inject_error("getLensMaskConfig", -40106)
    c = _client(tpap_camera, tpap=C200_TPAP)
    try:
        with pytest.raises(TapoDeviceError) as ei:
            c.get_privacy()
        assert ei.value.code == -40106
        assert c.logged_in                                # an item error does not end the session
    finally:
        c.close()


def test_full_control_api_over_tpap(tpap_camera):
    c = _client(tpap_camera, tpap=C200_TPAP)
    try:
        assert c.get_device_info().model == "C200"
        assert isinstance(c.get_components(), dict)
        c.set_privacy(True)
        assert c.get_privacy() is True and tpap_camera.state.privacy is True
        c.set_privacy(False)
        c.set_led(False)
        assert c.get_led() is False
        c.set_day_night(DayNightMode.ON)
        assert c.get_day_night() == DayNightMode.ON
        c.set_motion_detection(True, 60)
        assert c.get_motion_detection()[0] is True
        x0 = tpap_camera.state.ptz_x
        c.move(10, 0)
        assert tpap_camera.state.ptz_x != x0
        c.step(90)
        c.stop_move()
        c.add_preset("door")
        presets = c.get_presets()
        assert any(p.name == "door" for p in presets)
        c.goto_preset(presets[0].id)
        c.delete_preset(presets[0].id)
        assert isinstance(c.get_sd_status(), list)
        today = dt.datetime.now(dt.timezone.utc).date()
        days = c.days_with_recordings(today - dt.timedelta(days=3), today + dt.timedelta(days=1))
        assert days
        clips = c.recordings_for_day(days[-1], tz=dt.timezone.utc)
        assert clips
    finally:
        c.close()


def test_no_secrets_in_logs_or_repr(tpap_camera, caplog):
    caplog.set_level(logging.DEBUG)
    c = _client(tpap_camera, tpap=C200_TPAP)
    try:
        c.get_device_info()
        sess = c._session
        stok = sess.stok
        text = caplog.text + repr(c) + repr(sess) + repr(sess.tpap)
        for secret in (PASSWORD, tpap.md5_hex(PASSWORD), stok):
            assert secret not in text
    finally:
        c.close()


def test_default_mock_is_unchanged():
    cam = MockTapoCamera()
    assert cam.protocol == "secure"
    assert MockTapoCamera(secure=False).protocol == "legacy"
